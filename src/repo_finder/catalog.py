import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import duckdb

from .constants import _now_iso
from .models import ReusableCandidate

ANALYZER_VERSION = "deterministic-ui-v1"
DEFAULT_DB_NAME = "cache.duckdb"

ALLOWED_REUSE_OUTCOMES = {
    "returned",
    "opened_bundle",
    "selected",
    "integrated_successfully",
    "rejected_irrelevant",
    "rejected_too_coupled",
    "rejected_low_quality",
}

_connection: duckdb.DuckDBPyConnection | None = None
_connection_path: str | None = None


def repo_finder_home() -> Path:
    configured = os.environ.get("REPO_FINDER_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / ".repo_finder").resolve()


def ensure_home() -> Path:
    home = repo_finder_home()
    (home / "repos").mkdir(parents=True, exist_ok=True)
    (home / "bundles").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    return home


def catalog_db_path() -> Path:
    return ensure_home() / DEFAULT_DB_NAME


def safe_repo_dir(owner: str, repo: str) -> str:
    return f"{owner}__{repo}".replace("/", "__")


def snapshot_path(owner: str, repo: str, commit_sha: str) -> Path:
    return ensure_home() / "repos" / safe_repo_dir(owner, repo) / commit_sha


def bundle_path(candidate_id: str) -> Path:
    return ensure_home() / "bundles" / candidate_id


def reset_connection() -> None:
    global _connection, _connection_path
    if _connection is not None:
        _connection.close()
    _connection = None
    _connection_path = None


def get_connection() -> duckdb.DuckDBPyConnection:
    global _connection, _connection_path
    path = str(catalog_db_path())
    if _connection is None or _connection_path != path:
        if _connection is not None:
            _connection.close()
        _connection = duckdb.connect(path)
        _connection_path = path
        initialize_catalog(_connection)
    return _connection


def initialize_catalog(conn: duckdb.DuckDBPyConnection | None = None) -> None:
    active = conn if conn is not None else get_connection()
    active.execute("""
        CREATE TABLE IF NOT EXISTS repositories (
            repo_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            name TEXT NOT NULL,
            html_url TEXT NOT NULL,
            description TEXT,
            default_branch TEXT,
            is_public BOOLEAN NOT NULL,
            is_archived BOOLEAN NOT NULL,
            is_mirror BOOLEAN,
            detected_languages TEXT NOT NULL,
            topics TEXT NOT NULL,
            stars INTEGER,
            forks INTEGER,
            license_spdx TEXT,
            pushed_at TEXT,
            discovered_at TEXT NOT NULL,
            source_channel TEXT NOT NULL
        )
    """)
    active.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY,
            repo_id TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            default_branch TEXT,
            snapshot_path TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            analyzer_version TEXT NOT NULL,
            UNIQUE (repo_id, commit_sha, analyzer_version)
        )
    """)
    active.execute("""
        CREATE TABLE IF NOT EXISTS repository_cards (
            card_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            card_version TEXT NOT NULL,
            package_manifests TEXT NOT NULL,
            tree_summary TEXT NOT NULL,
            readme_excerpt TEXT,
            stack_signals TEXT NOT NULL,
            deterministic_features TEXT NOT NULL,
            gemma_profile TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (snapshot_id, card_version)
        )
    """)
    active.execute("""
        CREATE TABLE IF NOT EXISTS assets (
            asset_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            repo_id TEXT NOT NULL,
            capability TEXT NOT NULL,
            entry_paths TEXT NOT NULL,
            dependency_paths TEXT NOT NULL,
            external_dependencies TEXT NOT NULL,
            evidence_paths TEXT NOT NULL,
            synthesis TEXT NOT NULL,
            reuse_score DOUBLE NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (snapshot_id, capability)
        )
    """)
    active.execute("""
        CREATE TABLE IF NOT EXISTS reuse_outcomes (
            outcome_id TEXT PRIMARY KEY,
            asset_id TEXT,
            repo_id TEXT NOT NULL,
            task_signature TEXT NOT NULL,
            outcome TEXT NOT NULL,
            notes TEXT,
            recorded_at TEXT NOT NULL
        )
    """)
    active.execute("""
        CREATE TABLE IF NOT EXISTS analysis_runs (
            run_id TEXT PRIMARY KEY,
            stage_name TEXT NOT NULL,
            repo_id TEXT,
            snapshot_id TEXT,
            model_id TEXT,
            quantization TEXT,
            prompt_version TEXT,
            analyzer_version TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _hash_id(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:24]


def _owner_name(raw_repo: dict[str, Any]) -> tuple[str, str]:
    owner_obj = raw_repo.get("owner")
    owner = owner_obj.get("login") if isinstance(owner_obj, dict) else None
    name = raw_repo.get("name")
    full_name = raw_repo.get("full_name")
    if (not owner or not name) and isinstance(full_name, str) and "/" in full_name:
        owner, name = full_name.split("/", 1)
    if not owner or not name:
        raise ValueError("Repository metadata must include owner and name.")
    return str(owner), str(name)


def upsert_repository(raw_repo: dict[str, Any], source_channel: str) -> str:
    owner, name = _owner_name(raw_repo)
    repo_id = f"{owner}/{name}"
    license_obj = raw_repo.get("license")
    license_spdx = None
    if isinstance(license_obj, dict):
        license_spdx = license_obj.get("spdx_id")

    language = raw_repo.get("language")
    detected_languages = {"primary": language} if language else {}
    topics = raw_repo.get("topics") or []
    is_public = not bool(raw_repo.get("private", False))

    conn = get_connection()
    conn.execute(
        """
        INSERT OR REPLACE INTO repositories (
            repo_id, owner, name, html_url, description, default_branch,
            is_public, is_archived, is_mirror, detected_languages, topics,
            stars, forks, license_spdx, pushed_at, discovered_at, source_channel
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            repo_id,
            owner,
            name,
            raw_repo.get("html_url") or f"https://github.com/{repo_id}",
            raw_repo.get("description"),
            raw_repo.get("default_branch"),
            is_public,
            bool(raw_repo.get("archived", False)),
            raw_repo.get("mirror_url") is not None,
            _json_dump(detected_languages),
            _json_dump(topics),
            int(raw_repo.get("stargazers_count", 0) or 0),
            int(raw_repo.get("forks_count", 0) or 0),
            license_spdx,
            raw_repo.get("pushed_at"),
            _now_iso(),
            source_channel,
        ],
    )
    return repo_id


def get_repository(repo_id: str) -> dict[str, Any] | None:
    row = get_connection().execute(
        "SELECT * FROM repositories WHERE repo_id = ?",
        [repo_id],
    ).fetchone()
    if row is None:
        return None
    columns = [str(c[0]) for c in get_connection().description]
    return dict(zip(columns, row, strict=False))


def list_repositories_for_qualification(limit: int) -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT * FROM repositories
        ORDER BY COALESCE(stars, 0) DESC, discovered_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    columns = [str(c[0]) for c in conn.description]
    return [dict(zip(columns, row, strict=False)) for row in rows]


def upsert_snapshot(
    repo_id: str,
    commit_sha: str,
    default_branch: str | None,
    local_path: Path,
    analyzer_version: str = ANALYZER_VERSION,
) -> str:
    snapshot_id = _hash_id(repo_id, commit_sha, analyzer_version)
    get_connection().execute(
        """
        INSERT INTO snapshots (
            snapshot_id, repo_id, commit_sha, default_branch,
            snapshot_path, indexed_at, analyzer_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (snapshot_id) DO UPDATE SET
            repo_id = excluded.repo_id,
            commit_sha = excluded.commit_sha,
            default_branch = excluded.default_branch,
            snapshot_path = excluded.snapshot_path,
            indexed_at = excluded.indexed_at,
            analyzer_version = excluded.analyzer_version
        """,
        [
            snapshot_id,
            repo_id,
            commit_sha,
            default_branch,
            str(local_path),
            _now_iso(),
            analyzer_version,
        ],
    )
    return snapshot_id


def upsert_repository_card(snapshot_id: str, card: dict[str, Any]) -> str:
    card_version = str(card.get("card_version", "repo-card-v1"))
    card_id = _hash_id(snapshot_id, card_version)
    get_connection().execute(
        """
        INSERT INTO repository_cards (
            card_id, snapshot_id, card_version, package_manifests,
            tree_summary, readme_excerpt, stack_signals,
            deterministic_features, gemma_profile, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (card_id) DO UPDATE SET
            snapshot_id = excluded.snapshot_id,
            card_version = excluded.card_version,
            package_manifests = excluded.package_manifests,
            tree_summary = excluded.tree_summary,
            readme_excerpt = excluded.readme_excerpt,
            stack_signals = excluded.stack_signals,
            deterministic_features = excluded.deterministic_features,
            gemma_profile = excluded.gemma_profile,
            created_at = excluded.created_at
        """,
        [
            card_id,
            snapshot_id,
            card_version,
            _json_dump(card.get("package_manifests", {})),
            _json_dump(card.get("tree_summary", {})),
            card.get("readme_excerpt"),
            _json_dump(card.get("stack_signals", {})),
            _json_dump(card.get("deterministic_features", {})),
            _json_dump(card.get("gemma_profile")) if card.get("gemma_profile") else None,
            _now_iso(),
        ],
    )
    return card_id


def list_repository_cards_for_profile(limit: int, force: bool = False) -> list[dict[str, Any]]:
    conn = get_connection()
    where = "" if force else "WHERE c.gemma_profile IS NULL"
    rows = conn.execute(
        f"""
        SELECT
            c.card_id,
            c.snapshot_id,
            c.card_version,
            c.package_manifests,
            c.tree_summary,
            c.readme_excerpt,
            c.stack_signals,
            c.deterministic_features,
            c.gemma_profile,
            s.repo_id,
            s.commit_sha,
            r.html_url
        FROM repository_cards c
        JOIN snapshots s ON s.snapshot_id = c.snapshot_id
        JOIN repositories r ON r.repo_id = s.repo_id
        {where}
        ORDER BY c.created_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    columns = [str(c[0]) for c in conn.description]
    cards: list[dict[str, Any]] = []
    for row in rows:
        data = dict(zip(columns, row, strict=False))
        for key, default in (
            ("package_manifests", {}),
            ("tree_summary", {}),
            ("stack_signals", {}),
            ("deterministic_features", {}),
            ("gemma_profile", None),
        ):
            data[key] = _json_load(data.get(key), default)
        cards.append(data)
    return cards


def update_repository_card_gemma_profile(card_id: str, profile: dict[str, Any]) -> None:
    get_connection().execute(
        "UPDATE repository_cards SET gemma_profile = ? WHERE card_id = ?",
        [_json_dump(profile), card_id],
    )


def list_snapshots_for_evidence(limit: int) -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            s.snapshot_id, s.repo_id, s.commit_sha, s.default_branch,
            s.snapshot_path, s.indexed_at, r.html_url, r.owner, r.name
        FROM snapshots s
        JOIN repositories r ON r.repo_id = s.repo_id
        JOIN repository_cards c ON c.snapshot_id = s.snapshot_id
        ORDER BY s.indexed_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    columns = [str(c[0]) for c in conn.description]
    return [dict(zip(columns, row, strict=False)) for row in rows]


def upsert_asset(
    snapshot_id: str,
    repo_id: str,
    capability: str,
    evidence: dict[str, Any],
) -> str:
    entry_paths = [str(p) for p in evidence.get("entry_paths", [])]
    asset_id = _hash_id(snapshot_id, capability)
    get_connection().execute(
        """
        INSERT INTO assets (
            asset_id, snapshot_id, repo_id, capability, entry_paths,
            dependency_paths, external_dependencies, evidence_paths,
            synthesis, reuse_score, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (asset_id) DO UPDATE SET
            snapshot_id = excluded.snapshot_id,
            repo_id = excluded.repo_id,
            capability = excluded.capability,
            entry_paths = excluded.entry_paths,
            dependency_paths = excluded.dependency_paths,
            external_dependencies = excluded.external_dependencies,
            evidence_paths = excluded.evidence_paths,
            synthesis = excluded.synthesis,
            reuse_score = excluded.reuse_score,
            created_at = excluded.created_at
        """,
        [
            asset_id,
            snapshot_id,
            repo_id,
            capability,
            _json_dump(entry_paths),
            _json_dump(evidence.get("dependency_paths", [])),
            _json_dump(evidence.get("external_dependencies", [])),
            _json_dump(evidence.get("evidence_paths", [])),
            _json_dump(evidence.get("synthesis", {})),
            float(evidence.get("reuse_score", 0.0)),
            _now_iso(),
        ],
    )
    return asset_id


def get_asset_detail(asset_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT
            a.*, s.commit_sha, s.snapshot_path, r.html_url, r.owner, r.name
        FROM assets a
        JOIN snapshots s ON s.snapshot_id = a.snapshot_id
        JOIN repositories r ON r.repo_id = a.repo_id
        WHERE a.asset_id = ?
        """,
        [asset_id],
    ).fetchone()
    if row is None:
        return None
    columns = [str(c[0]) for c in conn.description]
    detail = dict(zip(columns, row, strict=False))
    for key, default in (
        ("entry_paths", []),
        ("dependency_paths", []),
        ("external_dependencies", []),
        ("evidence_paths", []),
        ("synthesis", {}),
    ):
        detail[key] = _json_load(detail.get(key), default)
    return detail


def search_assets(task: str, max_repos: int) -> list[ReusableCandidate]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            a.asset_id, a.repo_id, a.capability, a.entry_paths,
            a.dependency_paths, a.external_dependencies, a.evidence_paths,
            a.synthesis, a.reuse_score, s.commit_sha, r.html_url
        FROM assets a
        JOIN snapshots s ON s.snapshot_id = a.snapshot_id
        JOIN repositories r ON r.repo_id = a.repo_id
        """
    ).fetchall()
    columns = [str(c[0]) for c in conn.description]
    task_terms = {term for term in task.lower().replace("-", " ").split() if len(term) > 2}

    scored: list[tuple[float, ReusableCandidate]] = []
    for row in rows:
        data = dict(zip(columns, row, strict=False))
        entry_paths = _json_load(data.get("entry_paths"), [])
        dependency_paths = _json_load(data.get("dependency_paths"), [])
        external_dependencies = _json_load(data.get("external_dependencies"), [])
        evidence_paths = _json_load(data.get("evidence_paths"), [])
        synthesis = _json_load(data.get("synthesis"), {})
        searchable = " ".join(
            [
                str(data.get("capability", "")),
                str(data.get("repo_id", "")),
                " ".join(entry_paths),
                " ".join(external_dependencies),
                " ".join(synthesis.get("adaptation_notes", [])),
            ]
        ).lower()
        overlap = sum(1 for term in task_terms if term in searchable)
        score = float(data.get("reuse_score", 0.0)) + (overlap * 0.08)
        candidate = ReusableCandidate(
            candidate_id=str(data["asset_id"]),
            repo_id=str(data["repo_id"]),
            html_url=str(data["html_url"]),
            commit_sha=str(data["commit_sha"]),
            capability=str(data["capability"]),
            score=round(min(score, 1.0), 4),
            entry_paths=[str(p) for p in entry_paths],
            dependency_paths=[str(p) for p in dependency_paths],
            external_dependencies=[str(p) for p in external_dependencies],
            evidence_paths=[str(p) for p in evidence_paths],
            adaptation_notes=[str(p) for p in synthesis.get("adaptation_notes", [])],
        )
        scored.append((candidate.score, candidate))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in scored[:max_repos]]


def record_reuse_outcome(
    asset_id: str | None,
    repo_id: str,
    task_signature: str,
    outcome: str,
    notes: str | None = None,
) -> str:
    if outcome not in ALLOWED_REUSE_OUTCOMES:
        allowed = ", ".join(sorted(ALLOWED_REUSE_OUTCOMES))
        raise ValueError(f"Invalid outcome '{outcome}'. Allowed: {allowed}")
    outcome_id = _hash_id(asset_id or "", repo_id, task_signature, outcome, _now_iso())
    get_connection().execute(
        """
        INSERT INTO reuse_outcomes (
            outcome_id, asset_id, repo_id, task_signature,
            outcome, notes, recorded_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [outcome_id, asset_id, repo_id, task_signature, outcome, notes, _now_iso()],
    )
    return outcome_id


def task_signature(task: str) -> str:
    normalized = " ".join(task.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def record_analysis_run(
    stage_name: str,
    status: str,
    details: dict[str, Any],
    repo_id: str | None = None,
    snapshot_id: str | None = None,
    model_id: str | None = None,
    quantization: str | None = None,
    prompt_version: str | None = None,
    analyzer_version: str = ANALYZER_VERSION,
) -> str:
    run_id = _hash_id(stage_name, status, _now_iso())
    get_connection().execute(
        """
        INSERT INTO analysis_runs (
            run_id, stage_name, repo_id, snapshot_id, model_id,
            quantization, prompt_version, analyzer_version, status,
            details, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            stage_name,
            repo_id,
            snapshot_id,
            model_id,
            quantization,
            prompt_version,
            analyzer_version,
            status,
            _json_dump(details),
            _now_iso(),
        ],
    )
    return run_id


def garbage_collect_snapshots(keep_per_repo: int) -> dict[str, int]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT snapshot_id, repo_id, snapshot_path, indexed_at
        FROM snapshots
        ORDER BY repo_id, indexed_at DESC
        """
    ).fetchall()
    columns = [str(c[0]) for c in conn.description]
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        data = dict(zip(columns, row, strict=False))
        by_repo.setdefault(str(data["repo_id"]), []).append(data)

    removed = 0
    home = ensure_home().resolve()
    for snapshots in by_repo.values():
        for snapshot in snapshots[keep_per_repo:]:
            path = Path(str(snapshot["snapshot_path"])).resolve()
            if home in path.parents and path.exists():
                shutil.rmtree(path)
            snapshot_id = str(snapshot["snapshot_id"])
            conn.execute("DELETE FROM assets WHERE snapshot_id = ?", [snapshot_id])
            conn.execute("DELETE FROM repository_cards WHERE snapshot_id = ?", [snapshot_id])
            conn.execute("DELETE FROM snapshots WHERE snapshot_id = ?", [snapshot_id])
            removed += 1
    return {"removed_snapshots": removed}
