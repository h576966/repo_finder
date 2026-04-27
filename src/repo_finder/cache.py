import json
import os
from datetime import UTC, datetime
from typing import Any

import duckdb

from .constants import _now_iso

_cached_connection: duckdb.DuckDBPyConnection | None = None


def _get_connection() -> duckdb.DuckDBPyConnection:
    global _cached_connection
    if _cached_connection is None:
        db_path = os.environ.get("CACHE_DB_PATH", ":memory:")
        _cached_connection = duckdb.connect(db_path)
        _cached_connection.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                ttl_seconds INTEGER NOT NULL
            )
        """)
    return _cached_connection


def cache_get(key: str) -> dict[str, Any] | None:
    conn = _get_connection()
    row = conn.execute(
        "SELECT value, created_at, ttl_seconds FROM cache WHERE key = ?",
        [key],
    ).fetchone()

    if row is None:
        return None

    value_json, created_at, ttl_seconds = row

    if isinstance(created_at, str):
        created_at_dt = datetime.fromisoformat(created_at)
    else:
        created_at_dt = created_at

    if created_at_dt.tzinfo is None:
        created_at_dt = created_at_dt.replace(tzinfo=UTC)

    now = datetime.now(UTC)
    age_seconds = (now - created_at_dt).total_seconds()
    if age_seconds > ttl_seconds:
        conn.execute("DELETE FROM cache WHERE key = ?", [key])
        return None

    return json.loads(value_json)  # type: ignore[no-any-return]


def cache_set(key: str, value: dict[str, Any], ttl_seconds: int) -> None:
    conn = _get_connection()
    now = _now_iso()
    value_json = json.dumps(value)
    conn.execute(
        """
        INSERT OR REPLACE INTO cache (key, value, created_at, ttl_seconds)
        VALUES (?, ?, ?, ?)
        """,
        [key, value_json, now, ttl_seconds],
    )


def cache_delete(key: str) -> None:
    conn = _get_connection()
    conn.execute("DELETE FROM cache WHERE key = ?", [key])


def get_ttl(cache_type: str) -> int:
    env_map = {
        "search": "CACHE_TTL_SEARCH",
        "metadata": "CACHE_TTL_METADATA",
        "readme": "CACHE_TTL_README",
    }
    default_map = {
        "search": 1800,
        "metadata": 3600,
        "readme": 7200,
    }

    env_var = env_map.get(cache_type)
    if env_var:
        val = os.environ.get(env_var)
        if val is not None:
            try:
                return int(val)
            except ValueError:
                pass

    return default_map.get(cache_type, 3600)
