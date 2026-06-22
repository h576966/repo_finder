from datetime import UTC, datetime

SKIP_DIRS: set[str] = {
    "__pycache__", ".git", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "node_modules", ".venv", "venv",
}
MAX_REPOSITORY_SIZE_KB = 200_000
MAX_REPO_AGE_DAYS = 730
MAX_STALE_DAYS = 180


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
