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
    FindImplementationReferencesResult,
    ImplementationReference,
    ImplementationReferenceContext,
    ImplementationReferenceSnippet,
    RateLimitError,
)
from .path_safety import PathSafetyError, resolve_under_root, should_skip_path
from .reference_bounds import MAX_EXCERPT_BYTES, MAX_TASK_CHARS, bounded_response, json_size, metadata_view
from .target_profile import TargetProfileV1, build_target_profile, npm_unambiguous_major
from .usage_journal import journaled

SelectionKind = Literal["personal", "curated"]
REFERENCE_ANALYZER_VERSION = "implementation-reference-v2"
REFERENCE_SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cjs",
    ".cmd",
    ".cpp",
    ".cs",
    ".css",
    ".cts",
    ".ex",
    ".exs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".lua",
    ".mjs",
    ".mts",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".scss",
    ".swift",
    ".svelte",
    ".ts",
    ".tsx",
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
MAX_CATALOG_READ_BYTES = 8_000_000
MAX_CATALOG_VALUE_CHARS = 1_000_000
MIN_ABSOLUTE_BM25 = 0.35
_GITHUB_REPO_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_STOP_WORDS = {
    "and",
    "code",
    "for",
    "from",
    "implementation",
    "implement",
    "into",
    "that",
    "the",
    "this",
    "using",
    "with",
}


class ImplementationReferenceError(ValueError):
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
        raise ImplementationReferenceError("selection_kind must be personal or curated.")
    source_text = str(source).strip()
    if not source_text or len(source_text) > 2_000:
        raise ImplementationReferenceError("source is required and must not exceed 2000 characters.")
    remote_url: str | None
    default_branch: str | None

    github_match = _GITHUB_REPO_RE.fullmatch(source_text)
    if github_match and github_client is None:
        owned_client = get_client()
        try:
            return await add_reference_source(
                source, selection_kind=selection_kind, commit=commit, github_client=owned_client
            )
        finally:
            await owned_client.close()
    if github_match:
        client = github_client or get_client()
        owner, name = github_match.group("owner"), github_match.group("repo")
        try:
            raw_metadata = await client.get_repo_metadata(owner, name)
            default_branch = str(raw_metadata.get("default_branch") or "main")
            commit_sha = (
                _require_commit_sha(commit)
                if commit
                else await client.get_default_branch_commit(owner, name, default_branch)
            )
        except (httpx.HTTPError, RateLimitError, OSError, ValueError) as exc:
            raise ImplementationReferenceError(
                f"Could not inspect selected GitHub repository: {exc}"
            ) from exc
        repo_url = f"https://github.com/{owner}/{name}"
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
        raise ImplementationReferenceError(str(exc)) from exc
    if actual_sha != commit_sha:
        raise ImplementationReferenceError(
            f"Snapshot commit mismatch: expected {commit_sha}, got {actual_sha}."
        )

    source_profile = build_target_profile(snapshot_root)
    license_info = _observed_license(raw_metadata, snapshot_root)
    facts["license"] = license_info
    facts["selection_overrides_discovery_filters"] = True
    raw_metadata["language"] = raw_metadata.get("language") or next(iter(source_profile.languages), None)
    repo_id = f"{owner}/{name}"
    snapshot_id = _hash_id(repo_id, actual_sha, REFERENCE_ANALYZER_VERSION)

    references, index_facts = _index_snapshot(
        snapshot_root,
        snapshot_id=snapshot_id,
        repo_id=repo_id,
        manifest_paths=list(source_profile.manifest_paths),
    )
    bounded_facts, facts_clipped = metadata_view(facts)
    bounded_profile, profile_clipped = metadata_view(source_profile.to_jsonable())
    metadata = {
        "repository_facts": {**bounded_facts, "presentation_truncated": facts_clipped},
        "source_profile": {**bounded_profile, "incomplete": profile_clipped},
        "license": license_info,
        "index": index_facts,
    }
    with get_connection() as conn:
        repo_id = catalog_repositories.upsert_repository(raw_metadata, selection_kind)
        snapshot_id = catalog_repositories.upsert_snapshot(
            repo_id,
            actual_sha,
            default_branch,
            snapshot_root,
            analyzer_version=REFERENCE_ANALYZER_VERSION,
        )
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
                snapshot_id,
                repo_id,
                source_kind,
                selection_kind,
                origin,
                remote_url,
                _json_dump(metadata),
                _now_iso(),
            ],
        )
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


@journaled
def find_implementation_references(
    task: str,
    *,
    target_project_path: str | Path | None = None,
    max_results: int = 3,
) -> FindImplementationReferencesResult:
    if not task.strip() or len(task) > MAX_TASK_CHARS:
        raise ImplementationReferenceError("task is required and must not exceed 2000 characters.")
    if not 1 <= max_results <= 3:
        raise ImplementationReferenceError("max_results must be between 1 and 3.")
    target = build_target_profile(target_project_path) if target_project_path is not None else None
    rows, retrieval_limited = _latest_reference_rows()
    query_terms = set(_terms(task))
    if not rows:
        return _abstention(
            task,
            "No current source-evidence index is available. Add a selected repository; "
            "re-add v1 sources to build the v2 index. Historical IDs remain readable.",
            target,
        )
    if not query_terms:
        return _abstention(task, "The task has no searchable terms after normalization.", target)

    documents = [_json_load(row["search_text"], []) for row in rows]
    scores = _absolute_bm25_scores(query_terms, documents)
    accepted: list[tuple[float, ImplementationReference]] = []
    required_matches = 1 if len(query_terms) == 1 else 2
    for row, document, raw_score in zip(rows, documents, scores, strict=True):
        matched = sorted(query_terms & set(document))
        coverage = len(matched) / len(query_terms)
        # This absolute gate is evaluated before personal priority or target fit.
        if len(matched) < required_matches or coverage < 0.25 or raw_score < MIN_ABSOLUTE_BM25:
            continue
        metadata = _json_load(row["metadata"], {})
        facts, facts_clipped = metadata_view(metadata.get("repository_facts") or {})
        target_fit = _target_fit_facts(metadata.get("source_profile"), target)
        priority = 0.03 if row["selection_kind"] == "personal" else 0.0
        rank_score = raw_score + coverage * 0.2 + priority
        candidate = ImplementationReference(
            reference_id=str(row["reference_id"]),
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
            manifest_paths=[str(value) for value in _json_load(row["manifest_paths"], [])][:MAX_MANIFESTS],
            repository_facts={**facts, "presentation_truncated": facts_clipped},
            target_fit=target_fit,
        )
        accepted.append((rank_score, candidate))
    if not accepted:
        return _abstention(
            task,
            "No reference had enough task-specific source evidence; relative top rank, target fit, "
            "and personal priority cannot satisfy the absolute relevance gate."
            + (
                " Catalog read limit reached; only part of the collection was searched."
                if retrieval_limited
                else ""
            ),
            target,
        )
    accepted.sort(key=lambda item: (-item[0], item[1].reference_id))
    return FindImplementationReferencesResult(
        task=task.strip(),
        status="matches",
        results=[candidate for _score, candidate in accepted[:max_results]],
        target_profile_fingerprint=target.fingerprint if target else "",
        warnings=["Catalog read limit reached; only part of the collection was searched."]
        if retrieval_limited
        else [],
    )


@journaled
def get_implementation_reference(
    reference_id: str,
    *,
    task: str = "",
    target_project_path: str | Path | None = None,
) -> ImplementationReferenceContext:
    if len(task) > MAX_TASK_CHARS or len(reference_id) > 128:
        raise ImplementationReferenceError("Reference id or task exceeds input bounds.")
    row = _reference_row(reference_id)
    if row is None:
        raise ImplementationReferenceError(f"Unknown implementation reference: {reference_id}")
    snapshot_root = Path(str(row["snapshot_path"])).resolve()
    legacy = row["analyzer_version"] != REFERENCE_ANALYZER_VERSION
    if not legacy:
        _validate_snapshot(snapshot_root, str(row["commit_sha"]))
    try:
        source_path, safe_path = resolve_under_root(snapshot_root, str(row["path"]))
    except PathSafetyError as exc:
        raise ImplementationReferenceError(str(exc)) from exc
    if not source_path.is_file():
        raise ImplementationReferenceError(f"Pinned source is missing: {safe_path}")
    with source_path.open("rb") as stream:
        materialized = stream.read(MAX_REFERENCE_FILE_BYTES + 1)
    if len(materialized) > MAX_REFERENCE_FILE_BYTES:
        raise ImplementationReferenceError("Pinned source exceeds the read budget.")
    if hashlib.sha256(materialized).hexdigest() != row["content_sha256"]:
        raise ImplementationReferenceError("Pinned source changed from its recorded identity.")
    raw = snapshotter.read_blob(snapshot_root, str(row["commit_sha"]), safe_path)
    digest = hashlib.sha256(raw).hexdigest()
    if not legacy and digest != row["content_sha256"]:
        raise ImplementationReferenceError(
            f"Pinned source changed for {reference_id}; expected sha256:{row['content_sha256']}, "
            f"got sha256:{digest}. Re-add the clean commit to create a new identity."
        )
    metadata = _json_load(row["metadata"], {})
    remote_url = row.get("remote_url")
    lines = _source_lines(raw)
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
    target = build_target_profile(target_project_path) if target_project_path is not None else None
    index = dict(metadata.get("index") or {})
    facts, facts_clipped = metadata_view(metadata.get("repository_facts") or {})
    warnings = [*manifest_warnings]
    if facts_clipped:
        warnings.append("Repository metadata presentation was truncated.")
    if snippet_warning:
        warnings.append(snippet_warning)
    missing = []
    if index.get("limited"):
        missing.append("The source index omitted entries at configured safety or size limits.")
    if snippet_warning:
        missing.append(snippet_warning)
    license_info = dict(metadata.get("license") or {"status": "unknown"})
    if license_info.get("status") == "unknown":
        missing.append("License information is unknown.")
    return ImplementationReferenceContext(
        reference_id=reference_id,
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
        repository_facts=facts,
        target_fit=_target_fit_facts(metadata.get("source_profile"), target),
        license=license_info,
        missing_evidence=missing,
        warnings=warnings,
        truncated=sum(snippet.end_line - snippet.start_line + 1 for snippet in snippets) < len(lines)
        or facts_clipped
        or any(item["truncated"] for item in manifests),
        historical_content_sha256=f"sha256:{row['content_sha256']}" if legacy else None,
    )


async def search_github_fallback(
    task: str,
    *,
    max_results: int = 3,
    github_client: GitHubClient | None = None,
) -> dict[str, Any]:
    """One explicit page of temporary repository leads, never implementation proof."""
    if not task.strip() or len(task) > MAX_TASK_CHARS or not 1 <= max_results <= 3:
        raise ImplementationReferenceError("Provide a task of 1..2000 characters and max_results 1..3.")
    if github_client is None:
        owned = get_client()
        try:
            return await search_github_fallback(task, max_results=max_results, github_client=owned)
        finally:
            await owned.close()
    query = " ".join(_terms(task)[:8])[:200]
    if not query:
        raise ImplementationReferenceError("task has no searchable terms.")
    leads = []
    errors = []
    try:
        matches = await github_client.search_repos(query, per_page=max_results, page=1)
    except (httpx.HTTPError, RateLimitError, OSError, ValueError) as exc:
        return _github_failure(exc)
    for match in matches[:max_results]:
        full_name = str(match.get("full_name") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
            errors.append("Malformed repository lead.")
            continue
        owner, name = full_name.split("/")
        try:
            commit = _require_commit_sha(
                await github_client.get_default_branch_commit(
                    owner, name, str(match.get("default_branch") or "main")
                )
            )
            contents = await github_client.get_repo_contents(owner, name, ref=commit)
            readme = await github_client.get_readme(owner, name, ref=commit)
            facts, clipped = metadata_view(_github_repository_facts(match))
            leads.append(
                {
                    "kind": "repository_lead",
                    "repo_id": full_name,
                    "commit_sha": commit,
                    "html_url": f"https://github.com/{full_name}",
                    "repository_facts": facts,
                    "readme_excerpt": (readme or "")[:1200],
                    "root_paths": [
                        str(item["path"])[:500]
                        for item in contents[:30]
                        if isinstance(item, dict) and item.get("path")
                    ]
                    if isinstance(contents, list)
                    else [],
                    "implementation_verified": False,
                    "permanent": False,
                    "truncated": clipped,
                    "add_command": "source-scout references add --source "
                    f"https://github.com/{full_name} --commit {commit}",
                }
            )
        except (httpx.HTTPError, RateLimitError, OSError, ValueError) as exc:
            errors.append(_github_failure(exc)["error"])
    return bounded_response(
        {
            "status": "partial" if errors else ("leads" if leads else "no_matches"),
            "query": query,
            "results": leads,
            "errors": errors,
            "persisted": False,
            "note": "Inspect lead source, or explicitly add a selected repository.",
        }
    )


def _github_failure(exc: Exception) -> dict[str, Any]:
    kind = (
        "rate_limit"
        if isinstance(exc, RateLimitError)
        else ("invalid_response" if isinstance(exc, ValueError) else "network")
    )
    return {
        "status": "network_error",
        "error_type": kind,
        "error": f"GitHub {kind}; inspection was not completed.",
        "results": [],
        "persisted": False,
    }


async def inspect_github_source(
    source: str,
    commit: str,
    *,
    paths: list[str],
    github_client: GitHubClient | None = None,
) -> dict[str, Any]:
    """Verify up to three caller-selected Git blobs, without fetching or catalog writes."""
    match = _GITHUB_REPO_RE.fullmatch(source)
    commit = _require_commit_sha(commit)
    if not match or not 1 <= len(paths) <= 3:
        raise ImplementationReferenceError("Select a GitHub repository, commit and 1..3 paths.")
    for path in paths:
        if (
            not path
            or len(path) > 500
            or path.startswith("/")
            or any(c in path for c in "\\:\r\n")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or should_skip_path(Path.cwd(), Path.cwd() / path)
        ):
            raise ImplementationReferenceError("Inspection paths must be safe literal relative source paths.")
    if github_client is None:
        owned = get_client()
        try:
            return await inspect_github_source(source, commit, paths=paths, github_client=owned)
        finally:
            await owned.close()
    owner, name = match.group("owner"), match.group("repo")
    results, missing = [], []
    try:
        tree = await github_client.get_commit_tree(owner, name, commit)
        entries = {item.get("path"): item for item in tree["tree"][:6000] if isinstance(item, dict)}
        if tree.get("truncated") or len(tree["tree"]) > 6000:
            missing.append("GitHub tree response was incomplete.")
        for path in paths:
            entry = entries.get(path)
            if not entry or entry.get("mode") not in {"100644", "100755"} or entry.get("type") != "blob":
                missing.append(f"Regular source blob unavailable: {path}")
                continue
            try:
                raw = await github_client.get_blob(owner, name, str(entry.get("sha", "")))
                if b"\0" in raw or raw.startswith(b"version https://git-lfs.github.com/spec/"):
                    missing.append(f"Binary or LFS content omitted: {path}")
                    continue
                snippets, warning = _snippets(
                    _source_lines(raw), path=path, task="", remote_url=source, commit_sha=commit
                )
                results.append(
                    {
                        "kind": "verified_implementation_source",
                        "path": path,
                        "commit_sha": commit,
                        "git_blob_sha": entry["sha"],
                        "content_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "hash_basis": "git-blob-bytes",
                        "snippets": [asdict(item) for item in snippets],
                        "truncated": sum(s.end_line - s.start_line + 1 for s in snippets)
                        < len(_source_lines(raw)),
                    }
                )
                if warning:
                    missing.append(warning)
            except (httpx.HTTPError, RateLimitError, OSError, ValueError) as exc:
                missing.append(f"Source unavailable: {path} ({_github_failure(exc)['error_type']})")
    except (httpx.HTTPError, RateLimitError, OSError, ValueError) as exc:
        return _github_failure(exc)
    return bounded_response(
        {
            "status": "partial" if missing else "inspected",
            "results": results,
            "missing_evidence": missing,
            "persisted": False,
            "truncated": any(item["truncated"] for item in results),
        }
    )


def reference_to_jsonable(value: Any) -> dict[str, Any]:
    return bounded_response(asdict(value) if hasattr(value, "__dataclass_fields__") else dict(value))


def _validated_local_repository(source: str) -> Path:
    path = Path(source).expanduser()
    if path.is_symlink():
        raise ImplementationReferenceError("Local repository path must not be a symlink.")
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise ImplementationReferenceError(f"Local repository does not exist: {source}") from exc
    if not root.is_dir():
        raise ImplementationReferenceError("Local repository source must be a directory.")
    try:
        repo = git.Repo(str(root))
    except (git.InvalidGitRepositoryError, git.NoSuchPathError) as exc:
        raise ImplementationReferenceError("Local source must be a Git repository.") from exc
    try:
        if Path(str(repo.working_tree_dir)).resolve() != root:
            raise ImplementationReferenceError("Select the Git repository root, not a nested directory.")
    finally:
        repo.close()
    return root


def _local_revision(root: Path, revision: str | None) -> tuple[git.Repo, str, str | None]:
    repo = git.Repo(str(root))
    try:
        commit_sha = str(repo.commit(revision or "HEAD").hexsha)
    except (git.BadName, ValueError) as exc:
        repo.close()
        raise ImplementationReferenceError(f"Could not resolve local commit: {revision or 'HEAD'}") from exc
    branch = None if repo.head.is_detached else repo.active_branch.name
    return repo, commit_sha, branch


def _require_commit_sha(value: str | None) -> str:
    commit = (value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ImplementationReferenceError("GitHub --commit must be a full 40-character hexadecimal SHA.")
    return commit


def _index_snapshot(
    root: Path,
    *,
    snapshot_id: str,
    repo_id: str,
    manifest_paths: list[str],
) -> tuple[list[list[Any]], dict[str, Any]]:
    with snapshotter.open_snapshot(root) as repo:
        commit_sha = str(repo.head.commit.hexsha)
        blobs, materialization = snapshotter._regular_blobs(repo, commit_sha)
        regular_paths = {entry.path for entry in blobs}
    references: list[list[Any]] = []
    scanned_bytes = 0
    skipped_large = 0
    limited = False
    candidates: list[Path] = []
    for current_raw, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_raw)
        dirnames[:] = sorted(name for name in dirnames if not should_skip_path(root, current / name))
        for filename in sorted(filenames):
            path = current / filename
            if path.suffix.lower() in REFERENCE_SOURCE_SUFFIXES and not should_skip_path(root, path):
                candidates.append(path)
    for path in sorted(candidates, key=lambda item: item.relative_to(root).as_posix()):
        if len(references) >= MAX_REFERENCE_FILES or scanned_bytes >= MAX_TOTAL_INDEXED_BYTES:
            limited = True
            break
        rel_path = path.relative_to(root).as_posix()
        if rel_path not in regular_paths:
            continue
        size = path.stat().st_size
        if size > MAX_REFERENCE_FILE_BYTES:
            skipped_large += 1
            continue
        raw = snapshotter.read_blob(root, commit_sha, rel_path)
        if b"\0" in raw or raw.startswith(b"version https://git-lfs.github.com/spec/"):
            continue
        scanned_bytes += len(raw)
        digest = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", errors="replace")
        # Metadata and manifests describe a repository; they cannot prove that
        # this file implements a task. The gate and BM25 use only source evidence.
        terms = _terms(f"{rel_path}\n{text}")[:MAX_SEARCH_TERMS_PER_FILE]
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
        "limited": limited or skipped_large > 0 or materialization["limited"],
        "materialization": materialization,
    }


def _latest_reference_rows() -> tuple[list[dict[str, Any]], bool]:
    with get_connection() as conn:
        cursor = conn.execute(
            """
            WITH latest AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY repo_id ORDER BY added_at DESC, snapshot_id DESC
                ) AS source_rank
                FROM reference_sources
            )
            SELECT i.reference_id, i.snapshot_id, i.repo_id, i.path, i.content_sha256,
                   substr(i.search_text, 1, 1000001) AS search_text,
                   substr(i.manifest_paths, 1, 1000001) AS manifest_paths,
                   s.commit_sha, s.snapshot_path, s.analyzer_version, l.source_kind,
                   l.selection_kind, l.origin, l.remote_url, substr(l.metadata, 1, 1000001) AS metadata
            FROM implementation_references i
            JOIN latest l ON l.snapshot_id = i.snapshot_id AND l.source_rank = 1
            JOIN snapshots s ON s.snapshot_id = i.snapshot_id
            JOIN repositories r ON r.repo_id = i.repo_id
            WHERE s.analyzer_version = 'implementation-reference-v2'
            ORDER BY i.reference_id
            LIMIT 2001
            """
        )
        columns = [str(item[0]) for item in conn.description]
        rows: list[dict[str, Any]] = []
        consumed = 0
        limited = False
        while row := cursor.fetchone():
            consumed += json_size(row)
            if consumed > MAX_CATALOG_READ_BYTES or len(rows) >= 2000:
                limited = True
                break
            item = dict(zip(columns, row, strict=True))
            if any(
                len(str(item[key])) > MAX_CATALOG_VALUE_CHARS
                for key in ("metadata", "search_text", "manifest_paths")
            ):
                limited = True
                continue
            rows.append(item)
        return rows, limited


def _reference_row(reference_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT i.reference_id, i.snapshot_id, i.repo_id, i.path, i.content_sha256,
                   substr(i.manifest_paths, 1, 1000001) AS manifest_paths,
                   s.commit_sha, s.snapshot_path, s.analyzer_version, rs.source_kind,
                   rs.selection_kind, rs.origin, rs.remote_url, substr(rs.metadata, 1, 1000001) AS metadata
            FROM implementation_references i
            JOIN snapshots s ON s.snapshot_id = i.snapshot_id
            JOIN repositories r ON r.repo_id = i.repo_id
            JOIN reference_sources rs ON rs.snapshot_id = i.snapshot_id
            WHERE i.reference_id = ?
            """,
            [reference_id],
        ).fetchone()
        if row is None:
            return None
        columns = [str(item[0]) for item in conn.description]
        result = dict(zip(columns, row, strict=True))
        if any(len(str(result[key])) > MAX_CATALOG_VALUE_CHARS for key in ("metadata", "manifest_paths")):
            raise ImplementationReferenceError(
                "Historical reference metadata exceeds read bounds; export its data explicitly."
            )
        return result


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
    return [
        token.lower()
        for token in _TOKEN_RE.findall(expanded)
        if len(token) <= 64 and token.lower() not in _STOP_WORDS
    ]


def _target_fit_facts(source_raw: Any, target: TargetProfileV1 | None) -> dict[str, Any]:
    if target is None:
        return {"status": "not_requested", "shared": [], "conflicts": [], "unknown": []}
    source = source_raw if isinstance(source_raw, dict) else {}
    if source.get("incomplete"):
        return {
            "status": "unknown",
            "shared": [],
            "conflicts": [],
            "unknown": ["Source profile was bounded; target fit is unknown."],
        }
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
    return result


def _validate_snapshot(root: Path, commit_sha: str) -> None:
    try:
        snapshotter.validate_snapshot(root, commit_sha)
    except snapshotter.SnapshotError as exc:
        raise ImplementationReferenceError(str(exc)) from exc


def _source_lines(raw: bytes) -> list[str]:
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def _snippets(
    lines: list[str], *, path: str, task: str, remote_url: Any, commit_sha: str
) -> tuple[list[ImplementationReferenceSnippet], str | None]:
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
        while selected and json_size("\n".join(selected)) > MAX_EXCERPT_BYTES:
            selected.pop()
        if not selected:
            warning = "A source line exceeded the presentation budget; no partial line was cited."
            continue
        end = start + len(selected) - 1
        content = "\n".join(f"{number}|{line}" for number, line in enumerate(selected, start=start))
        snippets.append(
            ImplementationReferenceSnippet(
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
            raw = snapshotter.read_blob(root, commit_sha, safe_path)
            lines = _source_lines(raw)
            selected = lines[:MAX_MANIFEST_LINES]
            while selected and json_size("\n".join(selected)) > MAX_EXCERPT_BYTES:
                selected.pop()
            results.append(
                {
                    "path": safe_path,
                    "start_line": 1 if selected else None,
                    "end_line": len(selected),
                    "content": "\n".join(f"{number}|{line}" for number, line in enumerate(selected, start=1)),
                    "content_sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
                    "excerpt_sha256": f"sha256:{hashlib.sha256(chr(10).join(selected).encode()).hexdigest()}",
                    "normalization": "utf8-replace-crlf-to-lf-no-final-newline",
                    "hash_basis": "git-blob-bytes",
                    "permalink": _permalink(
                        remote_url, commit_sha, safe_path, 1 if selected else None, len(selected)
                    ),
                    "truncated": len(selected) < len(lines),
                }
            )
        except (OSError, PathSafetyError, snapshotter.SnapshotError) as exc:
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
    observation = (
        {"source": "current_github_repository_metadata", "observed_at": _now_iso(), "spdx": spdx}
        if raw_metadata.get("license")
        else None
    )
    pinned = {"files": files, "spdx": None, "source": "pinned_commit"}
    if spdx:
        return {
            "status": "metadata_observed",
            "spdx": None,
            "files": files,
            "current_repository_observation": observation,
            "pinned_commit": pinned,
        }
    if files:
        return {"status": "files_present_spdx_unknown", "spdx": None, "files": files}
    return {"status": "unknown", "spdx": None, "files": []}


def _github_license(raw_metadata: dict[str, Any]) -> str | None:
    value = raw_metadata.get("license")
    spdx = value.get("spdx_id") if isinstance(value, dict) else None
    return str(spdx) if spdx and str(spdx) not in {"NOASSERTION", "OTHER"} else None


def _github_repository_facts(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation_source": "current_github_repository_metadata",
        "observed_at": _now_iso(),
        "private": bool(raw.get("private", False)),
        "archived": bool(raw.get("archived", False)),
        "fork": bool(raw.get("fork", False)),
        "is_template": bool(raw.get("is_template", False)),
        "description": raw.get("description"),
        "default_branch": raw.get("default_branch"),
        "stars": int(raw.get("stargazers_count", 0) or 0),
    }


def _abstention(task: str, reason: str, target: TargetProfileV1 | None) -> FindImplementationReferencesResult:
    return FindImplementationReferencesResult(
        task=task.strip(),
        status="abstained",
        results=[],
        abstention_reason=reason,
        target_profile_fingerprint=target.fingerprint if target else "",
    )
