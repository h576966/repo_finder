"""Deterministic personal/curated implementation references."""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import git
import httpx

from . import catalog_repositories, snapshotter
from .catalog_core import _hash_id, _json_dump, _json_load, get_connection
from .constants import _now_iso
from .github_client import GitHubClient, get_client
from .models import (
    FindReuseReferencesResult,
    RateLimitError,
    ReuseContextResult,
    ReuseContextSnippet,
    ReuseReferenceCandidate,
)
from .path_safety import PathSafetyError, resolve_under_root, should_skip_path
from .pipeline import build_repository_card
from .target_profile import TargetProfileV1, build_target_profile, npm_unambiguous_major

SelectionKind = Literal["personal", "curated"]
REFERENCE_ANALYZER_VERSION = "implementation-reference-v1"
REFERENCE_SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".ex", ".exs", ".go", ".h", ".hpp",
    ".java", ".js", ".jsx", ".kt", ".kts", ".lua", ".mjs", ".mts", ".php",
    ".py", ".rb", ".rs", ".scala", ".scss", ".swift", ".svelte", ".ts", ".tsx",
    ".vue",
}
MAX_REFERENCE_FILES = 1_500
MAX_REFERENCE_FILE_BYTES = 240_000
MAX_TOTAL_INDEXED_BYTES = 30_000_000
MAX_SEARCH_TERMS_PER_FILE = 12_000
MAX_CONTEXT_SNIPPETS = 3
MAX_CONTEXT_LINES = 120
MAX_MANIFESTS = 4
MAX_MANIFEST_LINES = 40
MIN_ABSOLUTE_BM25 = 0.35
_GITHUB_REPO_RE = re.compile(r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_STOP_WORDS = {
    "and", "code", "for", "from", "implementation", "implement", "into", "that", "the",
    "this", "using", "with",
}


class ReuseReferenceError(ValueError):
    pass


async def add_reference_source(
    source: str | Path,
    *,
    selection_kind: SelectionKind = "personal",
    commit: str | None = None,
    github_client: GitHubClient | None = None,
) -> dict[str, Any]:
    """Snapshot and index one explicitly selected repository without executing it."""
    if selection_kind not in {"personal", "curated"}:
        raise ReuseReferenceError("selection_kind must be personal or curated.")
    source_text = str(source).strip()
    if not source_text:
        raise ReuseReferenceError("source is required.")
    remote_url: str | None
    default_branch: str | None

    github_match = _GITHUB_REPO_RE.fullmatch(source_text)
    if github_match:
        client = github_client or get_client()
        owner, name = github_match.group("owner"), github_match.group("repo")
        try:
            raw_metadata = await client.get_repo_metadata(owner, name)
            default_branch = str(raw_metadata.get("default_branch") or "main")
            commit_sha = _require_commit_sha(commit) if commit else await client.get_default_branch_commit(
                owner, name, default_branch
            )
        except (httpx.HTTPError, RateLimitError, OSError, ValueError) as exc:
            raise ReuseReferenceError(f"Could not inspect selected GitHub repository: {exc}") from exc
        repo_url = str(raw_metadata.get("html_url") or source_text).removesuffix(".git")
        origin = repo_url
        source_kind = "github"
        remote_url = repo_url
        facts = _github_repository_facts(raw_metadata)
    else:
        root = _validated_local_repository(source_text)
        owner = f"local-{hashlib.sha256(str(root).encode()).hexdigest()[:10]}"
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", root.name).strip("-.") or "repository"
        repo, commit_sha, default_branch = _local_revision(root, commit)
        repo.close()
        repo_url = str(root)
        origin = str(root)
        source_kind = "local"
        remote_url = None
        facts = {
            "private": None,
            "archived": None,
            "fork": None,
            "is_template": None,
            "description": None,
            "default_branch": default_branch,
        }
        raw_metadata = {
            "owner": {"login": owner},
            "name": name,
            "full_name": f"{owner}/{name}",
            "html_url": f"local:{root.as_posix()}",
            "default_branch": default_branch,
            "private": True,
            "archived": False,
            "fork": False,
            "is_template": False,
        }

    try:
        snapshot_root, actual_sha = snapshotter.clone_snapshot(
            repo_url=repo_url,
            owner=owner,
            repo=name,
            commit_sha=commit_sha,
            default_branch=default_branch,
            preserve_bytes=True,
        )
    except Exception as exc:
        raise ReuseReferenceError(str(exc)) from exc
    if actual_sha != commit_sha:
        raise ReuseReferenceError(f"Snapshot commit mismatch: expected {commit_sha}, got {actual_sha}.")

    source_profile = build_target_profile(snapshot_root)
    card = build_repository_card(snapshot_root)
    license_info = _observed_license(raw_metadata, snapshot_root)
    facts["license"] = license_info
    facts["selection_overrides_discovery_filters"] = True
    raw_metadata["language"] = raw_metadata.get("language") or next(iter(source_profile.languages), None)
    repo_id = catalog_repositories.upsert_repository(raw_metadata, selection_kind)
    snapshot_id = catalog_repositories.upsert_snapshot(
        repo_id,
        actual_sha,
        default_branch,
        snapshot_root,
        analyzer_version=REFERENCE_ANALYZER_VERSION,
    )
    catalog_repositories.upsert_repository_card(snapshot_id, card)

    references, index_facts = _index_snapshot(
        snapshot_root,
        snapshot_id=snapshot_id,
        repo_id=repo_id,
        manifest_paths=list(source_profile.manifest_paths),
        repository_text=" ".join(
            str(value)
            for value in (facts.get("description"), repo_id, *source_profile.framework_signals)
            if value
        ),
    )
    metadata = {
        "repository_facts": facts,
        "source_profile": source_profile.to_jsonable(),
        "license": license_info,
        "index": index_facts,
    }
    conn = get_connection()
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM implementation_references WHERE snapshot_id = ?", [snapshot_id])
        for reference in references:
            conn.execute(
                """
                INSERT INTO implementation_references (
                    reference_id, snapshot_id, repo_id, path, content_sha256,
                    search_text, manifest_paths, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                reference,
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO reference_sources (
                snapshot_id, repo_id, source_kind, selection_kind, origin,
                remote_url, metadata, added_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                snapshot_id, repo_id, source_kind, selection_kind, origin,
                remote_url, _json_dump(metadata), _now_iso(),
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {
        "status": "added",
        "repo_id": repo_id,
        "snapshot_id": snapshot_id,
        "commit_sha": actual_sha,
        "source_kind": source_kind,
        "selection_kind": selection_kind,
        "origin": origin,
        "remote_url": remote_url,
        "reference_count": len(references),
        "index": index_facts,
        "license": license_info,
        "executed_source_code": False,
    }


def find_reuse_references(
    task: str,
    *,
    project_path: str | Path | None = None,
    max_results: int = 3,
) -> FindReuseReferencesResult:
    if not task.strip():
        raise ReuseReferenceError("task is required.")
    if not 1 <= max_results <= 3:
        raise ReuseReferenceError("max_results must be between 1 and 3.")
    target = build_target_profile(project_path) if project_path is not None else None
    rows = _latest_reference_rows()
    query_terms = set(_terms(task))
    if not rows:
        return _abstention(task, "No personal or curated implementation references have been added.", target)
    if not query_terms:
        return _abstention(task, "The task has no searchable terms after normalization.", target)

    documents = [_json_load(row["search_text"], []) for row in rows]
    scores = _absolute_bm25_scores(query_terms, documents)
    accepted: list[tuple[float, ReuseReferenceCandidate]] = []
    required_matches = 1 if len(query_terms) == 1 else 2
    for row, document, raw_score in zip(rows, documents, scores, strict=True):
        matched = sorted(query_terms & set(document))
        coverage = len(matched) / len(query_terms)
        # This absolute gate is evaluated before personal priority or target fit.
        if len(matched) < required_matches or coverage < 0.25 or raw_score < MIN_ABSOLUTE_BM25:
            continue
        metadata = _json_load(row["metadata"], {})
        target_fit = _target_fit_facts(metadata.get("source_profile"), target)
        priority = 0.03 if row["selection_kind"] == "personal" else 0.0
        rank_score = raw_score + coverage * 0.2 + priority
        candidate = ReuseReferenceCandidate(
            candidate_id=str(row["reference_id"]),
            snapshot_id=str(row["snapshot_id"]),
            repo_id=str(row["repo_id"]),
            commit_sha=str(row["commit_sha"]),
            path=str(row["path"]),
            content_sha256=f"sha256:{row['content_sha256']}",
            relevance_score=round(raw_score, 4),
            matched_terms=matched,
            source_kind=str(row["source_kind"]),
            selection_kind=str(row["selection_kind"]),
            origin=str(row["origin"]),
            permalink=_permalink(row.get("remote_url"), str(row["commit_sha"]), str(row["path"])),
            manifest_paths=[str(value) for value in _json_load(row["manifest_paths"], [])],
            repository_facts=dict(metadata.get("repository_facts") or {}),
            target_fit=target_fit,
        )
        accepted.append((rank_score, candidate))
    if not accepted:
        return _abstention(
            task,
            "No reference had enough task-specific source evidence; relative top rank, target fit, "
            "and personal priority cannot satisfy the absolute relevance gate.",
            target,
        )
    accepted.sort(key=lambda item: (-item[0], item[1].candidate_id))
    return FindReuseReferencesResult(
        task=task.strip(),
        status="matches",
        results=[candidate for _score, candidate in accepted[:max_results]],
        target_profile_fingerprint=target.fingerprint if target else "",
    )


def get_reuse_context(
    candidate_id: str,
    *,
    task: str = "",
    project_path: str | Path | None = None,
) -> ReuseContextResult:
    row = _reference_row(candidate_id)
    if row is None:
        raise ReuseReferenceError(f"Unknown reuse reference: {candidate_id}")
    snapshot_root = Path(str(row["snapshot_path"])).resolve()
    _validate_snapshot(snapshot_root, str(row["commit_sha"]))
    try:
        source_path, safe_path = resolve_under_root(snapshot_root, str(row["path"]))
    except PathSafetyError as exc:
        raise ReuseReferenceError(str(exc)) from exc
    if not source_path.is_file():
        raise ReuseReferenceError(f"Pinned source is missing: {safe_path}")
    raw = source_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != row["content_sha256"]:
        raise ReuseReferenceError(
            f"Pinned source changed for {candidate_id}; expected sha256:{row['content_sha256']}, "
            f"got sha256:{digest}. Re-add the clean commit to create a new identity."
        )
    metadata = _json_load(row["metadata"], {})
    remote_url = row.get("remote_url")
    lines = raw.decode("utf-8", errors="replace").splitlines()
    snippets, snippet_warning = _snippets(
        lines,
        path=safe_path,
        task=task,
        remote_url=remote_url,
        commit_sha=str(row["commit_sha"]),
    )
    manifests, manifest_warnings = _manifest_context(
        snapshot_root,
        [str(value) for value in _json_load(row["manifest_paths"], [])],
        remote_url=remote_url,
        commit_sha=str(row["commit_sha"]),
    )
    target = build_target_profile(project_path) if project_path is not None else None
    index = dict(metadata.get("index") or {})
    warnings = [*manifest_warnings]
    if snippet_warning:
        warnings.append(snippet_warning)
    if index.get("limited"):
        warnings.append("The source index hit a configured file or byte limit; evidence may be incomplete.")
    missing = []
    license_info = dict(metadata.get("license") or {"status": "unknown"})
    if license_info.get("status") == "unknown":
        missing.append("License information is unknown.")
    return ReuseContextResult(
        candidate_id=candidate_id,
        snapshot_id=str(row["snapshot_id"]),
        repo_id=str(row["repo_id"]),
        commit_sha=str(row["commit_sha"]),
        source_kind=str(row["source_kind"]),
        selection_kind=str(row["selection_kind"]),
        origin=str(row["origin"]),
        path=safe_path,
        content_sha256=f"sha256:{digest}",
        snippets=snippets,
        manifests=manifests,
        repository_facts=dict(metadata.get("repository_facts") or {}),
        target_fit=_target_fit_facts(metadata.get("source_profile"), target),
        license=license_info,
        missing_evidence=missing,
        warnings=warnings,
        truncated=sum(snippet.end_line - snippet.start_line + 1 for snippet in snippets) < len(lines)
        or bool(index.get("limited")),
    )


async def search_github_fallback(
    task: str,
    *,
    max_results: int = 3,
    github_client: GitHubClient | None = None,
) -> dict[str, Any]:
    """Explicit bounded GitHub inspection. It never writes catalog state."""
    if not task.strip():
        raise ReuseReferenceError("task is required.")
    if not 1 <= max_results <= 3:
        raise ReuseReferenceError("max_results must be between 1 and 3.")
    query = " ".join(_terms(task)[:8])[:200]
    if not query:
        raise ReuseReferenceError("task has no searchable terms.")
    client = github_client or get_client()
    try:
        matches = await client.search_repos(query, per_page=max_results, page=1)
        inspected = []
        for match in matches[:max_results]:
            full_name = str(match.get("full_name") or "")
            if "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)
            branch = str(match.get("default_branch") or "main")
            commit_sha = await client.get_default_branch_commit(owner, repo, branch)
            root_contents = await client.get_repo_contents(owner, repo, ref=commit_sha)
            readme = await client.get_readme(owner, repo, ref=commit_sha)
            root_paths = []
            if isinstance(root_contents, list):
                root_paths = sorted(
                    str(item.get("path"))
                    for item in root_contents[:30]
                    if isinstance(item, dict) and item.get("path")
                )
            inspected.append(
                {
                    "repo_id": full_name,
                    "html_url": str(match.get("html_url") or f"https://github.com/{full_name}"),
                    "commit_sha": commit_sha,
                    "default_branch": branch,
                    "description": match.get("description"),
                    "language": match.get("language"),
                    "license": _github_license(match),
                    "private": bool(match.get("private", False)),
                    "archived": bool(match.get("archived", False)),
                    "fork": bool(match.get("fork", False)),
                    "is_template": bool(match.get("is_template", False)),
                    "root_paths": root_paths,
                    "readme_excerpt": (readme or "")[:1_200],
                    "permanent": False,
                    "add_command": (
                        f"source-scout reference-add --source "
                        f"{match.get('html_url') or f'https://github.com/{full_name}'} --commit {commit_sha}"
                    ),
                }
            )
    except RateLimitError as exc:
        return {"status": "network_error", "error_type": "rate_limit", "error": str(exc), "results": []}
    except (httpx.HTTPError, OSError) as exc:
        return {"status": "network_error", "error_type": "network", "error": str(exc), "results": []}
    return {
        "status": "matches" if inspected else "no_matches",
        "query": query,
        "results": inspected,
        "persisted": False,
        "note": "Add a selected pinned result separately with reference-add.",
    }


def reference_to_jsonable(value: Any) -> dict[str, Any]:
    return asdict(value) if hasattr(value, "__dataclass_fields__") else dict(value)


def _validated_local_repository(source: str) -> Path:
    path = Path(source).expanduser()
    if path.is_symlink():
        raise ReuseReferenceError("Local repository path must not be a symlink.")
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise ReuseReferenceError(f"Local repository does not exist: {source}") from exc
    if not root.is_dir():
        raise ReuseReferenceError("Local repository source must be a directory.")
    try:
        repo = git.Repo(str(root))
    except (git.InvalidGitRepositoryError, git.NoSuchPathError) as exc:
        raise ReuseReferenceError("Local source must be a Git repository.") from exc
    try:
        if Path(str(repo.working_tree_dir)).resolve() != root:
            raise ReuseReferenceError("Select the Git repository root, not a nested directory.")
    finally:
        repo.close()
    return root


def _local_revision(root: Path, revision: str | None) -> tuple[git.Repo, str, str | None]:
    repo = git.Repo(str(root))
    try:
        commit_sha = str(repo.commit(revision or "HEAD").hexsha)
    except (git.BadName, ValueError) as exc:
        repo.close()
        raise ReuseReferenceError(f"Could not resolve local commit: {revision or 'HEAD'}") from exc
    branch = None if repo.head.is_detached else repo.active_branch.name
    return repo, commit_sha, branch


def _require_commit_sha(value: str | None) -> str:
    commit = (value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReuseReferenceError("GitHub --commit must be a full 40-character hexadecimal SHA.")
    return commit


def _index_snapshot(
    root: Path,
    *,
    snapshot_id: str,
    repo_id: str,
    manifest_paths: list[str],
    repository_text: str,
) -> tuple[list[list[Any]], dict[str, Any]]:
    repo = git.Repo(str(root))
    try:
        unsafe_git_paths = {
            Path(index_path).as_posix()
            for (index_path, _stage), entry in repo.index.entries.items()
            if entry.mode not in {0o100644, 0o100755}
        }
    finally:
        repo.close()
    manifest_text = ""
    for rel_path in manifest_paths[:MAX_MANIFESTS]:
        try:
            path, _ = resolve_under_root(root, rel_path)
            manifest_text += " " + path.read_text(encoding="utf-8", errors="replace")[:8_000]
        except (OSError, PathSafetyError):
            continue
    references: list[list[Any]] = []
    scanned_bytes = 0
    skipped_large = 0
    limited = False
    candidates: list[Path] = []
    for current_raw, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_raw)
        dirnames[:] = sorted(
            name for name in dirnames if not should_skip_path(root, current / name)
        )
        for filename in sorted(filenames):
            path = current / filename
            if path.suffix.lower() in REFERENCE_SOURCE_SUFFIXES and not should_skip_path(root, path):
                candidates.append(path)
    for path in sorted(candidates, key=lambda item: item.relative_to(root).as_posix()):
        if len(references) >= MAX_REFERENCE_FILES or scanned_bytes >= MAX_TOTAL_INDEXED_BYTES:
            limited = True
            break
        rel_path = path.relative_to(root).as_posix()
        if rel_path in unsafe_git_paths:
            continue
        size = path.stat().st_size
        if size > MAX_REFERENCE_FILE_BYTES:
            skipped_large += 1
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        scanned_bytes += len(raw)
        digest = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", errors="replace")
        terms = _terms(f"{rel_path}\n{repository_text}\n{manifest_text}\n{text}")[:MAX_SEARCH_TERMS_PER_FILE]
        reference_id = _hash_id(REFERENCE_ANALYZER_VERSION, snapshot_id, rel_path, digest)
        references.append(
            [
                reference_id,
                snapshot_id,
                repo_id,
                rel_path,
                digest,
                _json_dump(terms),
                _json_dump(manifest_paths),
                _now_iso(),
            ]
        )
    return references, {
        "analyzer_version": REFERENCE_ANALYZER_VERSION,
        "reference_count": len(references),
        "scanned_bytes": scanned_bytes,
        "skipped_large_files": skipped_large,
        "candidate_file_count": len(candidates),
        "max_files": MAX_REFERENCE_FILES,
        "max_total_bytes": MAX_TOTAL_INDEXED_BYTES,
        "max_file_bytes": MAX_REFERENCE_FILE_BYTES,
        "limited": limited or skipped_large > 0,
    }


def _latest_reference_rows() -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY repo_id ORDER BY added_at DESC, snapshot_id DESC
            ) AS source_rank
            FROM reference_sources
        )
        SELECT i.*, s.commit_sha, s.snapshot_path, r.html_url, l.source_kind,
               l.selection_kind, l.origin, l.remote_url, l.metadata
        FROM implementation_references i
        JOIN latest l ON l.snapshot_id = i.snapshot_id AND l.source_rank = 1
        JOIN snapshots s ON s.snapshot_id = i.snapshot_id
        JOIN repositories r ON r.repo_id = i.repo_id
        ORDER BY i.reference_id
        """
    ).fetchall()
    columns = [str(item[0]) for item in conn.description]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _reference_row(candidate_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT i.*, s.commit_sha, s.snapshot_path, r.html_url, rs.source_kind,
               rs.selection_kind, rs.origin, rs.remote_url, rs.metadata
        FROM implementation_references i
        JOIN snapshots s ON s.snapshot_id = i.snapshot_id
        JOIN repositories r ON r.repo_id = i.repo_id
        JOIN reference_sources rs ON rs.snapshot_id = i.snapshot_id
        WHERE i.reference_id = ?
        """,
        [candidate_id],
    ).fetchone()
    if row is None:
        return None
    columns = [str(item[0]) for item in conn.description]
    return dict(zip(columns, row, strict=True))


def _absolute_bm25_scores(query_terms: set[str], documents: list[list[str]]) -> list[float]:
    if not documents:
        return []
    average_length = sum(len(document) for document in documents) / len(documents)
    if average_length <= 0:
        return [0.0] * len(documents)
    frequencies = {term: sum(term in document for document in documents) for term in query_terms}
    scores = []
    for document in documents:
        counts = Counter(document)
        score = 0.0
        for term in query_terms:
            count = counts.get(term, 0)
            if count <= 0:
                continue
            inverse = math.log(1 + (len(documents) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            denominator = count + 1.2 * (0.25 + 0.75 * len(document) / average_length)
            score += inverse * (count * 2.2 / denominator)
        scores.append(score)
    return scores


def _terms(value: str) -> list[str]:
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value).replace("_", " ").replace("-", " ")
    return [token.lower() for token in _TOKEN_RE.findall(expanded) if token.lower() not in _STOP_WORDS]


def _target_fit_facts(source_raw: Any, target: TargetProfileV1 | None) -> dict[str, Any]:
    if target is None:
        return {"status": "not_requested", "shared": [], "conflicts": [], "unknown": []}
    source = source_raw if isinstance(source_raw, dict) else {}
    shared: list[str] = []
    conflicts: list[str] = []
    unknown: list[str] = []
    source_languages = set(str(value) for value in source.get("languages", []))
    target_languages = set(target.languages)
    source_ecosystems = _ecosystems(source_languages)
    target_ecosystems = _ecosystems(target_languages)
    if source_ecosystems and target_ecosystems:
        common = sorted(source_ecosystems & target_ecosystems)
        if common:
            shared.append(f"Shared language ecosystem: {', '.join(common)}.")
        else:
            conflicts.append("Source and target use different known language ecosystems.")
    else:
        unknown.append("Language ecosystem compatibility is unknown.")
    source_frameworks = set(str(value) for value in source.get("framework_signals", []))
    common_frameworks = sorted(source_frameworks & set(target.framework_signals))
    if common_frameworks:
        shared.append(f"Shared frameworks: {', '.join(common_frameworks)}.")
    source_dependencies = _profile_dependencies(source)
    target_dependencies = {
        item.name: (item.ecosystem, item.constraint)
        for item in (*target.runtime_dependencies, *target.dev_dependencies)
    }
    common_dependencies = sorted(set(source_dependencies) & set(target_dependencies))
    if common_dependencies:
        shared.append(f"Shared dependencies: {', '.join(common_dependencies[:8])}.")
    for name in common_dependencies:
        source_ecosystem, source_constraint = source_dependencies[name]
        target_ecosystem, target_constraint = target_dependencies[name]
        if source_ecosystem != "npm" or target_ecosystem != "npm":
            continue
        source_major = npm_unambiguous_major(source_constraint)
        target_major = npm_unambiguous_major(target_constraint)
        if source_major is not None and target_major is not None and source_major != target_major:
            conflicts.append(f"Known npm major-version conflict: {name} {source_major}→{target_major}.")
        elif source_major is None or target_major is None:
            unknown.append(f"npm major-version compatibility is unknown for {name}.")
    if not shared and not conflicts:
        unknown.append("No deterministic compatibility signal was shared.")
    status = "conflict" if conflicts else ("compatible_signals" if shared else "unknown")
    return {"status": status, "shared": shared, "conflicts": conflicts, "unknown": sorted(set(unknown))}


def _profile_dependencies(profile: dict[str, Any]) -> dict[str, tuple[str, str]]:
    result = {}
    for section in ("runtime_dependencies", "dev_dependencies"):
        for item in profile.get(section, []):
            if isinstance(item, dict) and item.get("name"):
                result[str(item["name"])] = (str(item.get("ecosystem", "")), str(item.get("constraint", "")))
    return result


def _ecosystems(languages: set[str]) -> set[str]:
    result = set()
    if languages & {"javascript", "typescript"}:
        result.add("node")
    if "python" in languages:
        result.add("python")
    if languages & {"c", "cpp", "cs", "go", "java", "kotlin", "rust", "swift"}:
        result.add("compiled")
    return result


def _validate_snapshot(root: Path, commit_sha: str) -> None:
    if not root.is_dir():
        raise ReuseReferenceError(f"Pinned snapshot is missing: {root}")
    try:
        repo = git.Repo(str(root))
        try:
            actual_sha = str(repo.head.commit.hexsha)
            dirty = repo.is_dirty(untracked_files=True)
        finally:
            repo.close()
    except git.InvalidGitRepositoryError as exc:
        raise ReuseReferenceError(f"Pinned snapshot is not a Git repository: {root}") from exc
    if actual_sha != commit_sha:
        raise ReuseReferenceError(f"Pinned snapshot commit changed: expected {commit_sha}, got {actual_sha}.")
    if dirty:
        raise ReuseReferenceError("Pinned snapshot has changed files; refusing uncertain source context.")


def _snippets(
    lines: list[str], *, path: str, task: str, remote_url: Any, commit_sha: str
) -> tuple[list[ReuseContextSnippet], str | None]:
    query = set(_terms(task))
    hits = [index for index, line in enumerate(lines, start=1) if query & set(_terms(line))]
    warning = None
    if not hits:
        hits = [1] if lines else []
        if task.strip():
            warning = "No task terms matched individual source lines; returned the file opening."
    ranges: list[tuple[int, int]] = []
    for hit in hits:
        start, end = max(1, hit - 8), min(len(lines), hit + 12)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], min(len(lines), max(ranges[-1][1], end)))
        else:
            ranges.append((start, end))
        if len(ranges) >= MAX_CONTEXT_SNIPPETS:
            break
    snippets = []
    remaining = MAX_CONTEXT_LINES
    for start, end in ranges:
        end = min(end, start + remaining - 1)
        selected = lines[start - 1 : end]
        content = "\n".join(f"{number}|{line}" for number, line in enumerate(selected, start=start))
        snippets.append(
            ReuseContextSnippet(
                path=path,
                start_line=start,
                end_line=end,
                content=content,
                content_sha256=f"sha256:{hashlib.sha256(chr(10).join(selected).encode()).hexdigest()}",
                permalink=_permalink(remote_url, commit_sha, path, start, end),
            )
        )
        remaining -= len(selected)
        if remaining <= 0:
            break
    return snippets, warning


def _manifest_context(
    root: Path, manifest_paths: list[str], *, remote_url: Any, commit_sha: str
) -> tuple[list[dict[str, Any]], list[str]]:
    results = []
    warnings = []
    for rel_path in manifest_paths[:MAX_MANIFESTS]:
        try:
            path, safe_path = resolve_under_root(root, rel_path)
            raw = path.read_bytes()
            lines = raw.decode("utf-8", errors="replace").splitlines()
            selected = lines[:MAX_MANIFEST_LINES]
            results.append(
                {
                    "path": safe_path,
                    "start_line": 1,
                    "end_line": len(selected),
                    "content": "\n".join(f"{number}|{line}" for number, line in enumerate(selected, start=1)),
                    "content_sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
                    "permalink": _permalink(remote_url, commit_sha, safe_path, 1, len(selected)),
                    "truncated": len(selected) < len(lines),
                }
            )
        except (OSError, PathSafetyError) as exc:
            warnings.append(f"Manifest unavailable: {rel_path} ({exc})")
    if len(manifest_paths) > MAX_MANIFESTS:
        warnings.append(f"Manifest list limited to {MAX_MANIFESTS} files.")
    return results, warnings


def _permalink(
    remote_url: Any, commit_sha: str, path: str, start_line: int | None = None, end_line: int | None = None
) -> str | None:
    if not isinstance(remote_url, str) or not _GITHUB_REPO_RE.fullmatch(remote_url):
        return None
    anchor = ""
    if start_line is not None:
        anchor = f"#L{start_line}" + (f"-L{end_line}" if end_line and end_line != start_line else "")
    return f"{remote_url.rstrip('/').removesuffix('.git')}/blob/{commit_sha}/{quote(path, safe='/')}{anchor}"


def _observed_license(raw_metadata: dict[str, Any], root: Path) -> dict[str, Any]:
    spdx = _github_license(raw_metadata)
    files = []
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md"):
        if (root / name).is_file():
            files.append(name)
    if spdx:
        return {"status": "observed", "spdx": spdx, "files": files}
    if files:
        return {"status": "files_present_spdx_unknown", "spdx": None, "files": files}
    return {"status": "unknown", "spdx": None, "files": []}


def _github_license(raw_metadata: dict[str, Any]) -> str | None:
    value = raw_metadata.get("license")
    spdx = value.get("spdx_id") if isinstance(value, dict) else None
    return str(spdx) if spdx and str(spdx) not in {"NOASSERTION", "OTHER"} else None


def _github_repository_facts(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "private": bool(raw.get("private", False)),
        "archived": bool(raw.get("archived", False)),
        "fork": bool(raw.get("fork", False)),
        "is_template": bool(raw.get("is_template", False)),
        "description": raw.get("description"),
        "default_branch": raw.get("default_branch"),
        "stars": int(raw.get("stargazers_count", 0) or 0),
    }


def _abstention(
    task: str, reason: str, target: TargetProfileV1 | None
) -> FindReuseReferencesResult:
    return FindReuseReferencesResult(
        task=task.strip(),
        status="abstained",
        results=[],
        abstention_reason=reason,
        target_profile_fingerprint=target.fingerprint if target else "",
    )
