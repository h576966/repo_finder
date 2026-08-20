import hashlib
import json
import math
import os
import re
import shutil
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from .capabilities import (
    AI_DATA_CAPABILITIES,
    BACKEND_CAPABILITIES,
    CAPABILITY_INTENT_HINTS,
    COMMAND_PALETTE_DEPENDENCIES,
    UI_CAPABILITIES,
)
from .catalog_scoring import (
    MIN_LABEL_SIGNAL,
    POSSIBLE_LABEL_SCORE_CAP,
    STRONG_LABEL_SIGNAL,
    _backend_path_alignment_score,
    _capability_intent_scores,
    _capability_label_signal_score,
    _float_value,
    _has_backend_path,
    _has_profile_signal,
    _paths_contain_any,
    _profile_match_score,
    _synthesis_score,
    _task_terms,
)
from .constants import MAX_REPO_AGE_DAYS, MAX_REPOSITORY_SIZE_KB, MAX_STALE_DAYS, _now_iso
from .models import (
    AdaptationStep,
    AssessmentDimensions,
    CouplingRisk,
    EvidenceBackedReason,
    MissingEvidenceRequest,
    RequirementAssessment,
    ReusableCandidate,
    ReuseAssessmentResult,
)
from .target_profile import TargetProfileV1, npm_unambiguous_major

ANALYZER_VERSION = "deterministic-ui-v1"
DEFAULT_DB_NAME = "cache.duckdb"
RETRIEVAL_THRESHOLD_VERSION = "retrieval-threshold-v1"
MIN_RETRIEVAL_SCORE_V1 = 0.35
MIN_TASK_RELEVANCE_SIGNAL_V1 = 0.50
BM25_ROLE_VERSION = "bm25-role-v1"
BM25_MAX_BOOST = 0.18
BM25_MIN_ROLE_OVERLAP = 2
_BM25_STOP_WORDS = {
    "and",
    "for",
    "from",
    "implementation",
    "implement",
    "into",
    "that",
    "the",
    "using",
    "with",
}
_CAPABILITY_ROLE_TERMS = {
    "command-palette": {
        "actions",
        "keyboard",
        "launcher",
        "navigation",
        "navigating",
        "overlay",
        "shortcuts",
    },
    "data-table": {
        "browser",
        "filters",
        "pagination",
        "records",
        "sorting",
        "tabular",
    },
}
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


def source_scout_home() -> Path:
    configured = os.environ.get("SOURCE_SCOUT_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / ".source_scout").resolve()


def ensure_home() -> Path:
    home = source_scout_home()
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


def _safe_bundle_segment(value: str, label: str) -> str:
    segment = str(value).strip()
    if (
        not segment
        or Path(segment).is_absolute()
        or "/" in segment
        or "\\" in segment
        or segment in {".", ".."}
    ):
        raise ValueError(f"Unsafe bundle {label}: {value}")
    return segment


def bundle_path(candidate_id: str, task_signature: str | None = None) -> Path:
    root = ensure_home() / "bundles" / _safe_bundle_segment(candidate_id, "candidate_id")
    if task_signature is None:
        return root
    return root / _safe_bundle_segment(task_signature, "task_signature")


def assessment_bundle_path(candidate_id: str, assessment_id: str) -> Path:
    root = ensure_home() / "bundles" / _safe_bundle_segment(candidate_id, "candidate_id")
    return root / _safe_bundle_segment(assessment_id, "assessment_id")


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
            repo_size_kb INTEGER,
            repo_created_at TEXT,
            pushed_at TEXT,
            is_fork BOOLEAN,
            is_template BOOLEAN,
            discovered_at TEXT NOT NULL,
            source_channel TEXT NOT NULL
        )
    """)
    _ensure_column(active, "repositories", "repo_size_kb", "INTEGER")
    _ensure_column(active, "repositories", "repo_created_at", "TEXT")
    _ensure_column(active, "repositories", "is_fork", "BOOLEAN")
    _ensure_column(active, "repositories", "is_template", "BOOLEAN")
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
            repository_profile TEXT,
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
    active.execute("""
        CREATE TABLE IF NOT EXISTS evidence_refinements (
            refinement_id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL,
            repo_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            task_signature TEXT NOT NULL,
            capability TEXT NOT NULL,
            model_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            query TEXT NOT NULL,
            evidence_paths TEXT NOT NULL,
            notes TEXT NOT NULL,
            trajectory TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    active.execute("""
        CREATE TABLE IF NOT EXISTS reuse_assessments (
            assessment_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            repo_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            task TEXT NOT NULL,
            task_signature TEXT NOT NULL,
            model_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            analyzer_version TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            target_profile TEXT NOT NULL,
            target_profile_fingerprint TEXT NOT NULL,
            fastcontext_policy TEXT NOT NULL,
            fastcontext_status TEXT NOT NULL,
            license_status TEXT NOT NULL,
            recommended_verdict TEXT NOT NULL,
            final_verdict TEXT NOT NULL,
            reuse_score DOUBLE NOT NULL,
            model_confidence DOUBLE NOT NULL,
            confidence DOUBLE NOT NULL,
            evidence_coverage DOUBLE NOT NULL,
            requirement_count INTEGER NOT NULL,
            satisfied_requirement_count INTEGER NOT NULL,
            evidence_requirement_count INTEGER NOT NULL,
            dimensions TEXT NOT NULL,
            requirements TEXT NOT NULL,
            reasons TEXT NOT NULL,
            adaptation_steps TEXT NOT NULL,
            coupling_risks TEXT NOT NULL,
            missing_evidence TEXT NOT NULL,
            evidence_ledger TEXT NOT NULL,
            validation_notes TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    _migrate_column_name(
        active,
        "repository_cards",
        old_name="gemma_profile",
        new_name="repository_profile",
    )
    _ensure_column(active, "reuse_assessments", "target_profile", "TEXT")
    _ensure_column(active, "reuse_assessments", "target_profile_fingerprint", "TEXT")


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _hash_id(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:24]


def _cutoff_date(days: int) -> str:
    cutoff = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = cutoff - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%d")


def _ensure_column(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    column_name: str,
    column_type: str,
) -> None:
    exists = conn.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = ? AND column_name = ?
        """,
        [table_name, column_name],
    ).fetchone()
    if exists is None:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _migrate_column_name(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    *,
    old_name: str,
    new_name: str,
) -> None:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = ? AND column_name IN (?, ?)
        """,
        [table_name, old_name, new_name],
    ).fetchall()
    columns = {str(row[0]) for row in rows}
    if old_name in columns and new_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} RENAME COLUMN {old_name} TO {new_name}")
    elif old_name in columns and new_name in columns:
        conn.execute(f"UPDATE {table_name} SET {new_name} = COALESCE({new_name}, {old_name})")
        conn.execute(f"ALTER TABLE {table_name} DROP COLUMN {old_name}")


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
            stars, forks, license_spdx, repo_size_kb, repo_created_at, pushed_at,
            is_fork, is_template, discovered_at, source_channel
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            _int_or_none(raw_repo.get("size")),
            raw_repo.get("created_at"),
            raw_repo.get("pushed_at"),
            bool(raw_repo.get("fork", False)),
            bool(raw_repo.get("is_template", False)),
            _now_iso(),
            source_channel,
        ],
    )
    return repo_id


def get_repository(repo_id: str) -> dict[str, Any] | None:
    row = (
        get_connection()
        .execute(
            "SELECT * FROM repositories WHERE repo_id = ?",
            [repo_id],
        )
        .fetchone()
    )
    if row is None:
        return None
    columns = [str(c[0]) for c in get_connection().description]
    return dict(zip(columns, row, strict=False))


def list_repositories_for_qualification(limit: int) -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT * FROM repositories
        WHERE is_public = true
            AND is_archived = false
            AND (is_mirror IS NULL OR is_mirror = false)
            AND (is_fork IS NULL OR is_fork = false)
            AND (is_template IS NULL OR is_template = false)
            AND (repo_size_kb IS NULL OR repo_size_kb <= ?)
        ORDER BY COALESCE(stars, 0) DESC, discovered_at DESC
        LIMIT ?
        """,
        [MAX_REPOSITORY_SIZE_KB, limit],
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


def get_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM snapshots WHERE snapshot_id = ?",
        [snapshot_id],
    ).fetchone()
    if row is None:
        return None
    columns = [str(c[0]) for c in conn.description]
    return dict(zip(columns, row, strict=False))


def upsert_repository_card(snapshot_id: str, card: dict[str, Any]) -> str:
    card_version = str(card.get("card_version", "repo-card-v1"))
    card_id = _hash_id(snapshot_id, card_version)
    get_connection().execute(
        """
        INSERT INTO repository_cards (
            card_id, snapshot_id, card_version, package_manifests,
            tree_summary, readme_excerpt, stack_signals,
            deterministic_features, repository_profile, created_at
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
            repository_profile = excluded.repository_profile,
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
            _json_dump(card.get("repository_profile")) if card.get("repository_profile") else None,
            _now_iso(),
        ],
    )
    return card_id


def get_repository_card_for_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT
            c.card_id,
            c.snapshot_id,
            c.card_version,
            c.package_manifests,
            c.tree_summary,
            c.readme_excerpt,
            c.stack_signals,
            c.deterministic_features,
            c.repository_profile,
            c.created_at,
            s.repo_id,
            s.commit_sha,
            r.html_url
        FROM repository_cards c
        JOIN snapshots s ON s.snapshot_id = c.snapshot_id
        JOIN repositories r ON r.repo_id = s.repo_id
        WHERE c.snapshot_id = ?
        ORDER BY c.created_at DESC
        LIMIT 1
        """,
        [snapshot_id],
    ).fetchone()
    if row is None:
        return None
    columns = [str(c[0]) for c in conn.description]
    data = dict(zip(columns, row, strict=False))
    for key, default in (
        ("package_manifests", {}),
        ("tree_summary", {}),
        ("stack_signals", {}),
        ("deterministic_features", {}),
        ("repository_profile", None),
    ):
        data[key] = _json_load(data.get(key), default)
    return data


def list_repository_cards_for_profile(
    limit: int,
    force: bool = False,
    profile_schema_version: str | None = None,
) -> list[dict[str, Any]]:
    conn = get_connection()
    where = ""
    params: list[Any] = [limit]
    if not force:
        if profile_schema_version:
            where = """
        WHERE c.repository_profile IS NULL
            OR json_extract_string(c.repository_profile, '$.schema_version') IS NULL
            OR json_extract_string(c.repository_profile, '$.schema_version') != ?
            """
            params = [profile_schema_version, limit]
        else:
            where = "WHERE c.repository_profile IS NULL"
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
            c.repository_profile,
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
        params,
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
            ("repository_profile", None),
        ):
            data[key] = _json_load(data.get(key), default)
        cards.append(data)
    return cards


def update_repository_card_profile(card_id: str, profile: dict[str, Any]) -> None:
    get_connection().execute(
        "UPDATE repository_cards SET repository_profile = ? WHERE card_id = ?",
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
    snapshots = []
    for row in rows:
        data = dict(zip(columns, row, strict=False))
        data["snapshot_path"] = str(
            _resolve_snapshot_path(
                data.get("snapshot_path"),
                owner=str(data["owner"]),
                repo=str(data["name"]),
                commit_sha=str(data["commit_sha"]),
            )
        )
        snapshots.append(data)
    return snapshots


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


def delete_assets_for_snapshots(capability: str, snapshot_ids: list[str]) -> None:
    if not snapshot_ids:
        return
    placeholders = ", ".join("?" for _ in snapshot_ids)
    get_connection().execute(
        f"DELETE FROM assets WHERE capability = ? AND snapshot_id IN ({placeholders})",
        [capability, *snapshot_ids],
    )


def get_asset_detail(asset_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT
            a.*, s.commit_sha, s.snapshot_path, r.html_url, r.owner, r.name,
            r.license_spdx
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
    detail["snapshot_path"] = str(
        _resolve_snapshot_path(
            detail.get("snapshot_path"),
            owner=str(detail["owner"]),
            repo=str(detail["name"]),
            commit_sha=str(detail["commit_sha"]),
        )
    )
    return detail


def get_latest_snapshot_identity(repo_id: str) -> tuple[str, str] | None:
    row = (
        get_connection()
        .execute(
            """
        SELECT snapshot_id, commit_sha
        FROM snapshots
        WHERE repo_id = ?
        ORDER BY indexed_at DESC, snapshot_id DESC
        LIMIT 1
        """,
            [repo_id],
        )
        .fetchone()
    )
    if row is None:
        return None
    return str(row[0]), str(row[1])


def _resolve_snapshot_path(
    raw_path: Any,
    *,
    owner: str,
    repo: str,
    commit_sha: str,
) -> Path:
    path = Path(str(raw_path)).expanduser()
    if path.exists():
        return path.resolve()
    current_path = snapshot_path(owner, repo, commit_sha)
    if current_path.exists():
        return current_path.resolve()
    return path


_FRAMEWORK_DEPENDENCY_SIGNALS = {
    "@nestjs/core": "nestjs",
    "@sveltejs/kit": "sveltekit",
    "django": "django",
    "express": "express",
    "fastapi": "fastapi",
    "fastify": "fastify",
    "flask": "flask",
    "hono": "hono",
    "next": "nextjs",
    "nuxt": "nuxt",
    "react": "react",
    "starlette": "starlette",
    "svelte": "svelte",
    "vue": "vue",
}
_NPM_MAJOR_FRAMEWORK_DEPENDENCIES = {
    "@nestjs/core",
    "@sveltejs/kit",
    "express",
    "fastify",
    "hono",
    "next",
    "nuxt",
    "react",
    "svelte",
    "vue",
}


def _target_fit(
    target_profile: TargetProfileV1 | None,
    *,
    detected_languages: Any,
    package_manifests: Any,
    stack_signals: Any,
) -> tuple[float, list[str]]:
    if target_profile is None:
        return 0.0, []

    notes: list[str] = []
    score = 0.0
    candidate_languages = _candidate_languages(detected_languages, stack_signals)
    target_ecosystems = _language_ecosystems(set(target_profile.languages))
    candidate_ecosystems = _language_ecosystems(candidate_languages)
    if target_ecosystems and candidate_ecosystems:
        shared_ecosystems = sorted(target_ecosystems & candidate_ecosystems)
        if shared_ecosystems:
            score += 0.03
            notes.append(f"Shared language ecosystem: {', '.join(shared_ecosystems)}.")
        else:
            score -= 0.10
            notes.append("Candidate and target use different language ecosystems.")

    candidate_dependencies = _candidate_dependencies(package_manifests)
    target_dependencies = {
        item.name: item.constraint
        for item in (*target_profile.runtime_dependencies, *target_profile.dev_dependencies)
    }
    target_npm_dependencies = {
        item.name: item.constraint
        for item in (*target_profile.runtime_dependencies, *target_profile.dev_dependencies)
        if item.ecosystem == "npm"
    }
    dependency_overlap = sorted(set(candidate_dependencies) & set(target_dependencies))
    if dependency_overlap:
        score += min(0.03, len(dependency_overlap) * 0.01)
        notes.append(f"Shared dependencies: {', '.join(dependency_overlap[:6])}.")

    candidate_frameworks = {
        signal
        for dependency, signal in _FRAMEWORK_DEPENDENCY_SIGNALS.items()
        if dependency in candidate_dependencies
    }
    candidate_frameworks.update(_candidate_stack_frameworks(stack_signals))
    shared_frameworks = sorted(set(target_profile.framework_signals) & candidate_frameworks)
    if shared_frameworks:
        score += 0.04
        notes.append(f"Shared frameworks: {', '.join(shared_frameworks)}.")

    major_conflicts: list[str] = []
    unknown_major_constraints: list[str] = []
    for dependency in sorted(_NPM_MAJOR_FRAMEWORK_DEPENDENCIES & set(target_npm_dependencies)):
        candidate_constraint = candidate_dependencies.get(dependency, "")
        if dependency not in candidate_dependencies:
            continue
        target_constraint = target_npm_dependencies.get(dependency, "")
        candidate_major = npm_unambiguous_major(candidate_constraint)
        target_major = npm_unambiguous_major(target_constraint)
        if candidate_major is not None and target_major is not None and candidate_major != target_major:
            major_conflicts.append(f"{dependency} {candidate_major}→{target_major}")
        elif candidate_major is None or target_major is None:
            unknown_major_constraints.append(dependency)
    if major_conflicts:
        score -= 0.10
        notes.append(f"Known npm major-version conflict: {', '.join(major_conflicts)}.")
    if unknown_major_constraints:
        notes.append(
            f"npm major-version compatibility is unknown for: {', '.join(unknown_major_constraints)}."
        )

    if not notes:
        notes.append("Target compatibility is unknown from available deterministic metadata.")
    return round(max(-0.20, min(0.10, score)), 4), notes


def _candidate_languages(detected_languages: Any, stack_signals: Any) -> set[str]:
    languages: set[str] = set()
    if isinstance(detected_languages, dict):
        languages.update(str(value).strip().lower() for value in detected_languages.values() if value)
    elif isinstance(detected_languages, list):
        languages.update(str(value).strip().lower() for value in detected_languages if value)
    if isinstance(stack_signals, dict):
        if bool(stack_signals.get("has_typescript_files")):
            languages.add("typescript")
        if bool(stack_signals.get("has_javascript_or_typescript_files")):
            languages.add("javascript")
        if bool(stack_signals.get("has_python_files")):
            languages.add("python")
    return languages


def _language_ecosystems(languages: set[str]) -> set[str]:
    ecosystems: set[str] = set()
    if languages & {"javascript", "typescript", "tsx", "jsx"}:
        ecosystems.add("node")
    if "python" in languages:
        ecosystems.add("python")
    return ecosystems


def _candidate_dependencies(package_manifests: Any) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    if not isinstance(package_manifests, dict):
        return dependencies
    for manifest in package_manifests.values():
        if not isinstance(manifest, dict):
            continue
        for section in ("dependencies", "devDependencies"):
            values = manifest.get(section)
            if not isinstance(values, dict):
                continue
            for name, constraint in values.items():
                dependencies.setdefault(str(name).strip().lower(), str(constraint).strip())
    return dependencies


def _candidate_stack_frameworks(stack_signals: Any) -> set[str]:
    if not isinstance(stack_signals, dict):
        return set()
    signals: set[str] = set()
    if bool(stack_signals.get("has_next_dependency")):
        signals.add("nextjs")
    if bool(stack_signals.get("has_react_dependency")):
        signals.add("react")
    return signals


def _bm25_terms(value: str) -> list[str]:
    return [
        term
        for term in re.findall(r"[a-z0-9]+", value.lower())
        if len(term) > 2 and term not in _BM25_STOP_WORDS
    ]


def _role_card_terms(data: dict[str, Any]) -> list[str]:
    capability = str(data.get("capability", ""))
    entry_paths = _json_load(data.get("entry_paths"), [])
    dependency_paths = _json_load(data.get("dependency_paths"), [])
    external_dependencies = _json_load(data.get("external_dependencies"), [])
    evidence_paths = _json_load(data.get("evidence_paths"), [])
    role_terms = {
        capability,
        *CAPABILITY_INTENT_HINTS.get(capability, set()),
        *_CAPABILITY_ROLE_TERMS.get(capability, set()),
        *[str(path) for path in entry_paths],
        *[str(path) for path in dependency_paths],
        *[str(path) for path in evidence_paths],
        *[str(dependency) for dependency in external_dependencies],
    }
    return _bm25_terms(" ".join(sorted(role_terms)))


def _bm25_role_scores(task: str, rows: list[dict[str, Any]]) -> dict[str, float]:
    query_terms = set(_bm25_terms(task))
    if not query_terms or not rows:
        return {}
    documents = [_role_card_terms(data) for data in rows]
    average_length = sum(len(document) for document in documents) / len(documents)
    if average_length <= 0:
        return {}
    document_frequency = {term: sum(1 for document in documents if term in document) for term in query_terms}
    raw_scores: dict[str, float] = {}
    for data, document in zip(rows, documents, strict=True):
        frequencies = Counter(document)
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if frequency <= 0:
                continue
            frequency_in_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                1 + (len(documents) - frequency_in_documents + 0.5) / (frequency_in_documents + 0.5)
            )
            denominator = frequency + 1.2 * (0.25 + 0.75 * len(document) / average_length)
            score += inverse_document_frequency * (frequency * 2.2 / denominator)
        raw_scores[str(data["asset_id"])] = score
    maximum = max(raw_scores.values(), default=0.0)
    if maximum <= 0:
        return {}
    return {asset_id: round(score / maximum, 4) for asset_id, score in raw_scores.items()}


def _role_overlap(task_terms: set[str], capability: str) -> int:
    return len(task_terms & _CAPABILITY_ROLE_TERMS.get(capability, set()))


def search_assets(
    task: str,
    max_repos: int,
    *,
    target_profile: TargetProfileV1 | None = None,
) -> list[ReusableCandidate]:
    conn = get_connection()
    created_cutoff = _cutoff_date(MAX_REPO_AGE_DAYS)
    pushed_cutoff = _cutoff_date(MAX_STALE_DAYS)
    rows = conn.execute(
        """
        WITH ranked_snapshots AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY repo_id
                    ORDER BY indexed_at DESC, snapshot_id DESC
                ) AS snapshot_rank
            FROM snapshots
        )
        SELECT
            a.asset_id, a.repo_id, a.capability, a.entry_paths,
            a.dependency_paths, a.external_dependencies, a.evidence_paths,
            a.synthesis, a.reuse_score, s.commit_sha, r.html_url,
            r.is_public, r.is_archived, r.repo_size_kb, r.repo_created_at,
            r.pushed_at, r.detected_languages, c.repository_profile,
            c.package_manifests, c.stack_signals
        FROM assets a
        JOIN ranked_snapshots s ON s.snapshot_id = a.snapshot_id
        JOIN repositories r ON r.repo_id = a.repo_id
        LEFT JOIN repository_cards c ON c.snapshot_id = a.snapshot_id
        WHERE s.snapshot_rank = 1
            AND r.is_public = true
            AND r.is_archived = false
            AND (r.is_mirror IS NULL OR r.is_mirror = false)
            AND (r.is_fork IS NULL OR r.is_fork = false)
            AND (r.is_template IS NULL OR r.is_template = false)
            AND (r.repo_size_kb IS NULL OR r.repo_size_kb <= ?)
            AND r.repo_created_at IS NOT NULL
            AND r.repo_created_at >= ?
            AND r.pushed_at IS NOT NULL
            AND r.pushed_at >= ?
        """,
        [MAX_REPOSITORY_SIZE_KB, created_cutoff, pushed_cutoff],
    ).fetchall()
    columns = [str(c[0]) for c in conn.description]
    row_data = [dict(zip(columns, row, strict=False)) for row in rows]
    task_terms = _task_terms(task)
    bm25_scores = _bm25_role_scores(task, row_data)
    signature = task_signature(
        task,
        target_profile.fingerprint if target_profile is not None else "",
    )
    intent_scores = _capability_intent_scores(task)
    best_intent_score = max(intent_scores.values(), default=0.0)
    primary_intent = max(intent_scores.items(), key=lambda item: item[1], default=("", 0.0))[0]
    has_bm25_role_intent = (
        max(
            (_role_overlap(task_terms, capability) for capability in _CAPABILITY_ROLE_TERMS),
            default=0,
        )
        >= BM25_MIN_ROLE_OVERLAP
    )

    scored: list[tuple[float, ReusableCandidate]] = []
    for data in row_data:
        entry_paths = _json_load(data.get("entry_paths"), [])
        dependency_paths = _json_load(data.get("dependency_paths"), [])
        external_dependencies = _json_load(data.get("external_dependencies"), [])
        evidence_paths = _json_load(data.get("evidence_paths"), [])
        synthesis = _json_load(data.get("synthesis"), {})
        repository_profile = _json_load(data.get("repository_profile"), None)
        detected_languages = _json_load(data.get("detected_languages"), {})
        package_manifests = _json_load(data.get("package_manifests"), {})
        stack_signals = _json_load(data.get("stack_signals"), {})
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
        capability = str(data["capability"])
        profile_has_signal = _has_profile_signal(repository_profile)
        profile_score = _profile_match_score(repository_profile) if profile_has_signal else 0.0
        ui_path_score = _synthesis_score(synthesis, "ui_path_score")
        noise_penalty = _synthesis_score(synthesis, "noise_penalty")
        capability_path_score = _synthesis_score(synthesis, "capability_path_score")
        label_signal_score = _capability_label_signal_score(
            capability,
            entry_paths + evidence_paths,
            external_dependencies,
            synthesis,
        )
        if label_signal_score < MIN_LABEL_SIGNAL:
            continue
        path_alignment_score = _backend_path_alignment_score(
            capability,
            entry_paths + evidence_paths,
        )
        base_score = _float_value(data.get("reuse_score"))
        if label_signal_score < STRONG_LABEL_SIGNAL:
            base_score = min(base_score, POSSIBLE_LABEL_SCORE_CAP)
        capability_intent_score = intent_scores.get(capability, 0.0)
        score = (
            (base_score * 0.55)
            + (ui_path_score * 0.2)
            + (profile_score * 0.25)
            + (capability_path_score * 0.12)
            + (label_signal_score * 0.08)
            + (capability_intent_score * 0.28)
            + min(0.12, overlap * 0.035)
            - (noise_penalty * 0.16)
            + path_alignment_score
        )
        if best_intent_score >= 0.35 and capability_intent_score < best_intent_score * 0.75:
            score -= 0.32
        if (
            primary_intent in BACKEND_CAPABILITIES
            and best_intent_score >= 0.7
            and capability != primary_intent
        ):
            score -= 0.22
        if (
            primary_intent in AI_DATA_CAPABILITIES
            and best_intent_score >= 0.7
            and capability != primary_intent
        ):
            score -= 0.18
        if best_intent_score >= 0.7 and capability in UI_CAPABILITIES and capability_intent_score < 0.35:
            score -= 0.18
        if capability == "data-table" and "@tanstack/react-table" not in external_dependencies:
            score -= (1 - capability_path_score) * 0.12
        if capability == "command-palette":
            dependency_set = set(external_dependencies)
            has_command_dependency = bool(COMMAND_PALETTE_DEPENDENCIES & dependency_set)
            if has_command_dependency:
                score += 0.12
            elif capability_path_score <= 0:
                score -= 0.42
            else:
                score += 0.04
        if capability == "server-actions" and not _paths_contain_any(
            entry_paths + evidence_paths,
            {"actions", "server-action"},
        ):
            score -= 0.3
        if capability == "trpc-router":
            has_trpc_dependency = "@trpc/server" in external_dependencies
            has_trpc_path = _paths_contain_any(entry_paths + evidence_paths, {"trpc"})
            if has_trpc_dependency and has_trpc_path:
                score += 0.18
            elif not has_trpc_dependency:
                score -= 0.6
        if capability == "data-access":
            db_dependencies = {"drizzle-orm", "prisma", "@prisma/client"} & set(external_dependencies)
            has_specific_db_path = _paths_contain_any(
                entry_paths + evidence_paths,
                {"drizzle", "prisma", "schema"},
            )
            if db_dependencies:
                score += 0.12
            elif not has_specific_db_path:
                score -= 0.36
        if capability == "file-storage":
            storage_dependencies = {
                "@aws-sdk/client-s3",
                "@vercel/blob",
                "@supabase/supabase-js",
                "firebase",
                "googleapis",
                "react-dropzone",
                "uploadthing",
            } & set(external_dependencies)
            has_storage_path = _paths_contain_any(
                entry_paths + evidence_paths,
                {"attachment", "blob", "document", "drive", "r2", "s3", "storage", "upload"},
            )
            if storage_dependencies:
                score += 0.14
            if not has_storage_path:
                score -= 0.32
        if capability == "model-server-integration":
            has_model_server_path = _paths_contain_any(
                entry_paths + evidence_paths,
                {"chat", "completion", "lmstudio", "model", "models", "ollama", "openai", "responses"},
            )
            if has_model_server_path:
                score += 0.12
            else:
                score -= 0.28
        if capability == "local-ai-integration":
            if "embedding" in task_terms:
                has_embedding_path = _paths_contain_any(
                    entry_paths + evidence_paths,
                    {"embed", "embedding", "embeddings"},
                )
                if has_embedding_path:
                    score += 0.18
                else:
                    score -= 0.35
            if "ollama" in task_terms and not _paths_contain_any(
                entry_paths + evidence_paths,
                {"ollama"},
            ):
                score -= 0.24
        if capability == "node-ai-sdk":
            ai_sdk_dependencies = {
                "@ai-sdk/anthropic",
                "@ai-sdk/openai",
                "@ai-sdk/react",
                "ai",
            } & set(external_dependencies)
            asks_for_ai_sdk = bool({"sdk", "streaming", "stream"} & task_terms)
            if ai_sdk_dependencies:
                score += 0.18
            elif asks_for_ai_sdk:
                score -= 0.35
        data_tool_terms = {"duckdb", "pandas", "polars"}
        if task_terms & data_tool_terms:
            data_tool_dependencies = data_tool_terms & set(external_dependencies)
            if data_tool_dependencies:
                score += 0.3
            elif capability == "data-pipeline":
                score -= 0.42
        if capability in BACKEND_CAPABILITIES and not _has_backend_path(entry_paths):
            score -= 0.28
        if capability in AI_DATA_CAPABILITIES and path_alignment_score < 0:
            score -= 0.08
        if profile_has_signal and profile_score < 0.12:
            score -= 0.08
        if not entry_paths:
            score -= 0.12
        sort_score = max(0.0, score)
        bm25_score = bm25_scores.get(str(data["asset_id"]), 0.0)
        if has_bm25_role_intent:
            if (
                _role_overlap(task_terms, capability) < BM25_MIN_ROLE_OVERLAP
                or bm25_score < MIN_TASK_RELEVANCE_SIGNAL_V1
            ):
                continue
            sort_score += bm25_score * BM25_MAX_BOOST
        else:
            task_relevance_signal = max(
                capability_intent_score,
                min(1.0, overlap / 3.0),
            )
            if best_intent_score < 0.18 or task_relevance_signal < MIN_TASK_RELEVANCE_SIGNAL_V1:
                continue
        if sort_score < MIN_RETRIEVAL_SCORE_V1:
            continue
        target_fit_score, target_fit_notes = _target_fit(
            target_profile,
            detected_languages=detected_languages,
            package_manifests=package_manifests,
            stack_signals=stack_signals,
        )
        sort_score = max(0.0, sort_score + target_fit_score)
        display_score = min(sort_score, 1.0)
        candidate = ReusableCandidate(
            candidate_id=str(data["asset_id"]),
            repo_id=str(data["repo_id"]),
            html_url=str(data["html_url"]),
            commit_sha=str(data["commit_sha"]),
            capability=capability,
            score=round(display_score, 4),
            task_signature=signature,
            entry_paths=[str(p) for p in entry_paths],
            dependency_paths=[str(p) for p in dependency_paths],
            external_dependencies=[str(p) for p in external_dependencies],
            evidence_paths=[str(p) for p in evidence_paths],
            adaptation_notes=[str(p) for p in synthesis.get("adaptation_notes", [])],
            target_fit_score=target_fit_score,
            target_fit_notes=target_fit_notes,
        )
        scored.append((sort_score, candidate))

    scored.sort(key=lambda item: item[0], reverse=True)
    unique_by_repo: dict[str, ReusableCandidate] = {}
    for _, candidate in scored:
        if candidate.repo_id not in unique_by_repo:
            unique_by_repo[candidate.repo_id] = candidate
        if len(unique_by_repo) >= max_repos:
            break
    return list(unique_by_repo.values())


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


def task_signature(task: str, target_profile_fingerprint: str = "") -> str:
    normalized = " ".join(task.lower().split())
    if target_profile_fingerprint:
        normalized = f"{normalized}\0{target_profile_fingerprint}"
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


def store_evidence_refinement(
    *,
    asset_id: str,
    repo_id: str,
    snapshot_id: str,
    task_signature: str,
    parent_task_signature: str | None = None,
    capability: str,
    model_id: str,
    prompt_version: str,
    schema_version: str,
    query: str,
    evidence_paths: list[str],
    notes: list[str],
    trajectory: list[dict[str, Any]],
) -> str:
    stored_task_signature = parent_task_signature or task_signature
    refinement_id = _hash_id(
        asset_id,
        stored_task_signature,
        model_id,
        prompt_version,
        _now_iso(),
    )
    get_connection().execute(
        """
        INSERT INTO evidence_refinements (
            refinement_id, asset_id, repo_id, snapshot_id, task_signature,
            capability, model_id, prompt_version, schema_version, query,
            evidence_paths, notes, trajectory, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            refinement_id,
            asset_id,
            repo_id,
            snapshot_id,
            stored_task_signature,
            capability,
            model_id,
            prompt_version,
            schema_version,
            query,
            _json_dump(evidence_paths),
            _json_dump(notes),
            _json_dump(trajectory),
            _now_iso(),
        ],
    )
    return refinement_id


def list_evidence_refinements(
    asset_id: str,
    *,
    limit: int = 5,
    task_signature: str | None = None,
) -> list[dict[str, Any]]:
    conn = get_connection()
    where = "WHERE asset_id = ?"
    params: list[Any] = [asset_id]
    if task_signature is not None:
        where += " AND task_signature = ?"
        params.append(task_signature)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT *
        FROM evidence_refinements
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    columns = [str(c[0]) for c in conn.description]
    refinements: list[dict[str, Any]] = []
    for row in rows:
        data = dict(zip(columns, row, strict=False))
        for key in ("evidence_paths", "notes", "trajectory"):
            loaded = _json_load(data.get(key), [])
            data[key] = loaded if isinstance(loaded, list) else []
        refinements.append(data)
    return refinements


def _json_dicts(value: str | None) -> list[dict[str, Any]]:
    loaded = _json_load(value, [])
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, dict)]


def _reuse_assessment_from_row(data: dict[str, Any]) -> ReuseAssessmentResult:
    dimensions_data = _json_load(data.get("dimensions"), {})
    if not isinstance(dimensions_data, dict):
        dimensions_data = {}
    dimensions = AssessmentDimensions(
        functional_fit=float(dimensions_data.get("functional_fit", 0.0)),
        extractability=float(dimensions_data.get("extractability", 0.0)),
        dependency_fit=float(dimensions_data.get("dependency_fit", 0.0)),
        coupling_risk=float(dimensions_data.get("coupling_risk", 0.0)),
        maintenance_risk=float(dimensions_data.get("maintenance_risk", 0.0)),
    )
    return ReuseAssessmentResult(
        assessment_id=str(data["assessment_id"]),
        candidate_id=str(data["candidate_id"]),
        repo_id=str(data["repo_id"]),
        snapshot_id=str(data["snapshot_id"]),
        commit_sha=str(data["commit_sha"]),
        task=str(data["task"]),
        task_signature=str(data["task_signature"]),
        model_id=str(data["model_id"]),
        prompt_version=str(data["prompt_version"]),
        schema_version=str(data["schema_version"]),
        analyzer_version=str(data["analyzer_version"]),
        input_fingerprint=str(data["input_fingerprint"]),
        target_profile=_json_load(data.get("target_profile"), {}),
        target_profile_fingerprint=str(data.get("target_profile_fingerprint") or ""),
        fastcontext_policy=str(data["fastcontext_policy"]),
        fastcontext_status=str(data["fastcontext_status"]),
        license_status=str(data["license_status"]),
        recommended_verdict=str(data["recommended_verdict"]),
        final_verdict=str(data["final_verdict"]),
        reuse_score=float(data["reuse_score"]),
        model_confidence=float(data["model_confidence"]),
        confidence=float(data["confidence"]),
        evidence_coverage=float(data["evidence_coverage"]),
        requirement_count=int(data["requirement_count"]),
        satisfied_requirement_count=int(data["satisfied_requirement_count"]),
        evidence_requirement_count=int(data["evidence_requirement_count"]),
        dimensions=dimensions,
        requirements=[
            RequirementAssessment(
                requirement=str(item.get("requirement", "")),
                satisfied=bool(item.get("satisfied", False)),
                status=str(
                    item.get(
                        "status",
                        "satisfied" if bool(item.get("satisfied", False)) else "unsatisfied",
                    )
                ),
                evidence_paths=[str(path) for path in item.get("evidence_paths", [])],
                notes=[str(note) for note in item.get("notes", [])],
            )
            for item in _json_dicts(data.get("requirements"))
        ],
        reasons=[
            EvidenceBackedReason(
                reason=str(item.get("reason", "")),
                evidence_paths=[str(path) for path in item.get("evidence_paths", [])],
            )
            for item in _json_dicts(data.get("reasons"))
        ],
        adaptation_steps=[
            AdaptationStep(
                summary=str(item.get("summary", "")),
                source_paths=[str(path) for path in item.get("source_paths", [])],
                target_hint=str(item.get("target_hint", "")),
                notes=[str(note) for note in item.get("notes", [])],
            )
            for item in _json_dicts(data.get("adaptation_steps"))
        ],
        coupling_risks=[
            CouplingRisk(
                risk=str(item.get("risk", "")),
                severity=str(item.get("severity", "medium")),
                evidence_paths=[str(path) for path in item.get("evidence_paths", [])],
                mitigation=str(item.get("mitigation", "")),
                hard_blocker=bool(item.get("hard_blocker", False)),
            )
            for item in _json_dicts(data.get("coupling_risks"))
        ],
        missing_evidence=[
            MissingEvidenceRequest(
                question=str(item.get("question", "")),
                suggested_paths=[str(path) for path in item.get("suggested_paths", [])],
                reason=str(item.get("reason", "")),
            )
            for item in _json_dicts(data.get("missing_evidence"))
        ],
        evidence_ledger=_json_dicts(data.get("evidence_ledger")),
        validation_notes=[str(note) for note in _json_load(data.get("validation_notes"), [])],
        created_at=str(data["created_at"]),
    )


def store_reuse_assessment(assessment: ReuseAssessmentResult) -> str:
    created_at = assessment.created_at or _now_iso()
    assessment_id = assessment.assessment_id or _hash_id(
        assessment.candidate_id,
        assessment.task_signature,
        assessment.input_fingerprint,
        created_at,
    )
    get_connection().execute(
        """
        INSERT INTO reuse_assessments (
            assessment_id, candidate_id, repo_id, snapshot_id, commit_sha,
            task, task_signature, model_id, prompt_version, schema_version,
            analyzer_version, input_fingerprint, target_profile,
            target_profile_fingerprint, fastcontext_policy, fastcontext_status,
            license_status, recommended_verdict, final_verdict, reuse_score,
            model_confidence, confidence, evidence_coverage, requirement_count,
            satisfied_requirement_count, evidence_requirement_count, dimensions,
            requirements, reasons, adaptation_steps, coupling_risks,
            missing_evidence, evidence_ledger, validation_notes, created_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?
        )
        """,
        [
            assessment_id,
            assessment.candidate_id,
            assessment.repo_id,
            assessment.snapshot_id,
            assessment.commit_sha,
            assessment.task,
            assessment.task_signature,
            assessment.model_id,
            assessment.prompt_version,
            assessment.schema_version,
            assessment.analyzer_version,
            assessment.input_fingerprint,
            _json_dump(assessment.target_profile),
            assessment.target_profile_fingerprint,
            assessment.fastcontext_policy,
            assessment.fastcontext_status,
            assessment.license_status,
            assessment.recommended_verdict,
            assessment.final_verdict,
            assessment.reuse_score,
            assessment.model_confidence,
            assessment.confidence,
            assessment.evidence_coverage,
            assessment.requirement_count,
            assessment.satisfied_requirement_count,
            assessment.evidence_requirement_count,
            _json_dump(asdict(assessment.dimensions)),
            _json_dump([asdict(item) for item in assessment.requirements]),
            _json_dump([asdict(item) for item in assessment.reasons]),
            _json_dump([asdict(item) for item in assessment.adaptation_steps]),
            _json_dump([asdict(item) for item in assessment.coupling_risks]),
            _json_dump([asdict(item) for item in assessment.missing_evidence]),
            _json_dump(assessment.evidence_ledger),
            _json_dump(assessment.validation_notes),
            created_at,
        ],
    )
    return assessment_id


def get_reuse_assessment(assessment_id: str) -> ReuseAssessmentResult | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM reuse_assessments WHERE assessment_id = ?",
        [assessment_id],
    ).fetchone()
    if row is None:
        return None
    columns = [str(c[0]) for c in conn.description]
    return _reuse_assessment_from_row(dict(zip(columns, row, strict=False)))


def get_latest_reuse_assessment(
    candidate_id: str,
    task_signature: str,
    input_fingerprint: str,
) -> ReuseAssessmentResult | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT *
        FROM reuse_assessments
        WHERE candidate_id = ?
            AND task_signature = ?
            AND input_fingerprint = ?
        ORDER BY created_at DESC, assessment_id DESC
        LIMIT 1
        """,
        [candidate_id, task_signature, input_fingerprint],
    ).fetchone()
    if row is None:
        return None
    columns = [str(c[0]) for c in conn.description]
    return _reuse_assessment_from_row(dict(zip(columns, row, strict=False)))


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
