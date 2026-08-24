import shutil
from pathlib import Path
from typing import Any

from .catalog_assets import _resolve_snapshot_path
from .catalog_core import (
    ANALYZER_VERSION,
    _hash_id,
    _json_dump,
    _json_load,
    ensure_home,
    get_connection,
)
from .constants import MAX_REPOSITORY_SIZE_KB, _now_iso


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
