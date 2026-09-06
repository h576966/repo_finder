"""Opt-in policy for additional remote Source Scout exploration requests."""

from __future__ import annotations

import math
import os
import tomllib
from pathlib import Path

DEFAULT_DEADLINE_SECONDS = 240.0


def remote_exploration_enabled(project_path: str | Path = ".") -> bool:
    override = os.environ.get("SOURCE_SCOUT_REMOTE_EXPLORATION")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes"}
    root = Path(project_path).expanduser().resolve()
    policy = root / ".source-scout.toml"
    if policy.is_symlink() or not policy.is_file():
        return False
    try:
        value = tomllib.loads(policy.read_text(encoding="utf-8"))
        return value.get("remote_exploration", {}).get("enabled") is True
    except (OSError, ValueError, AttributeError):
        return False


def exploration_deadline_seconds() -> float:
    try:
        value = float(os.environ.get("SOURCE_SCOUT_EXPLORATION_DEADLINE_SECONDS", "240"))
        return min(value, DEFAULT_DEADLINE_SECONDS) if math.isfinite(value) and value > 0 else 240.0
    except ValueError:
        return DEFAULT_DEADLINE_SECONDS
