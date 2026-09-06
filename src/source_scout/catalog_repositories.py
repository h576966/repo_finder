"""Repository and snapshot identities shared with historical catalog data."""

from pathlib import Path
from typing import Any

from .catalog_core import ANALYZER_VERSION, _hash_id, _json_dump, get_connection
from .constants import _now_iso


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
    with get_connection() as conn:
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
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM repositories WHERE repo_id = ?",
            [repo_id],
        ).fetchone()
        if row is None:
            return None
        columns = [str(c[0]) for c in conn.description]
        return dict(zip(columns, row, strict=False))


def upsert_snapshot(
    repo_id: str,
    commit_sha: str,
    default_branch: str | None,
    local_path: Path,
    analyzer_version: str = ANALYZER_VERSION,
) -> str:
    with get_connection() as conn:
        snapshot_id = _hash_id(repo_id, commit_sha, analyzer_version)
        conn.execute(
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
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = ?",
            [snapshot_id],
        ).fetchone()
        if row is None:
            return None
        columns = [str(c[0]) for c in conn.description]
        return dict(zip(columns, row, strict=False))
