import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from .constants import _now_iso

ANALYZER_VERSION = "deterministic-ui-v1"
DEFAULT_DB_NAME = "cache.duckdb"

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
