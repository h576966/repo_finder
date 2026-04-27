from datetime import UTC, datetime

SKIP_DIRS: set[str] = {
    "__pycache__", ".git", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "node_modules", ".venv", "venv",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
