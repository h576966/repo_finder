from pathlib import Path
from typing import Any

from .catalog_core import _hash_id, _json_dump, _json_load, get_connection, snapshot_path
from .constants import _now_iso


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
