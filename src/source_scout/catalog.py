"""Historical catalog access; retired workflows are not executable."""

from __future__ import annotations

from typing import Any

from .catalog_core import (
    catalog_db_path,
    ensure_home,
    get_connection,
    initialize_catalog,
    reset_connection,
    snapshot_path,
    source_scout_home,
)
from .catalog_repositories import get_repository, get_snapshot, upsert_repository, upsert_snapshot

__all__ = [
    "catalog_db_path",
    "ensure_home",
    "get_connection",
    "initialize_catalog",
    "reset_connection",
    "snapshot_path",
    "source_scout_home",
    "get_repository",
    "get_snapshot",
    "upsert_repository",
    "upsert_snapshot",
    "read_records",
]

HISTORICAL_TABLES = frozenset(
    {
        "repositories",
        "snapshots",
        "repository_cards",
        "assets",
        "reuse_assessments",
        "reuse_outcomes",
        "analysis_runs",
        "evidence_refinements",
        "reference_sources",
        "implementation_references",
    }
)


def read_records(table: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Paginated read/export without reinterpreting old fields or touching artifacts."""
    if table not in HISTORICAL_TABLES or not 1 <= limit <= 100 or offset < 0:
        raise ValueError("Select a known table, limit 1..100 and nonnegative offset.")
    with get_connection() as conn:
        cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY 1 LIMIT ? OFFSET ?', [limit, offset])
        columns = [str(col[0]) for col in cursor.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    return {"table": table, "offset": offset, "rows": rows, "next_offset": offset + len(rows)}
