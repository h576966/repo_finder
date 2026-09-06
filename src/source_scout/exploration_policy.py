"""Small, fail-closed routing contract for remote exploration."""

from __future__ import annotations

import math
import os
import tomllib
from pathlib import Path
from typing import Literal, get_args

DEFAULT_DEADLINE_SECONDS = 240.0
ExplorationMode = Literal["off", "selective", "on"]
ExplorationUseCase = Literal[
    "cross_file_contract", "indirect_runtime_flow", "ambiguous_ownership", "architecture_trace"
]
LocalMethod = Literal["rg", "direct_read", "serena"]
SELECTIVE_USE_CASES = get_args(ExplorationUseCase)
LOCAL_METHODS = get_args(LocalMethod)


def _parse_mode(value: object) -> ExplorationMode:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"on", "1", "true", "yes"}:
            return "on"
        if normalized == "selective":
            return "selective"
    return "off"


def remote_exploration_mode(project_path: str | Path = ".") -> ExplorationMode:
    """Environment wins over project mode, which wins over legacy enabled.

    Missing, unreadable, malformed or invalid policy fails closed to off.
    Reading this one configuration file does not collect repository source.
    """
    override = os.environ.get("SOURCE_SCOUT_REMOTE_EXPLORATION")
    if override is not None:
        return _parse_mode(override)
    root = Path(project_path).expanduser().resolve()
    policy = root / ".source-scout.toml"
    if policy.is_symlink() or not policy.is_file():
        return "off"
    try:
        value = tomllib.loads(policy.read_text(encoding="utf-8"))
        section = value.get("remote_exploration", {})
        if not isinstance(section, dict):
            return "off"
        if "mode" in section:
            return _parse_mode(section["mode"])
        return "on" if section.get("enabled") is True else "off"
    except (OSError, ValueError, AttributeError):
        return "off"


def remote_exploration_enabled(project_path: str | Path = ".") -> bool:
    """Compatibility/debug gate only; investigations must validate their route."""
    return remote_exploration_mode(project_path) != "off"


def validate_investigation(
    mode: ExplorationMode,
    task: str,
    reason: str,
    use_case: ExplorationUseCase | None,
    attempted_local_methods: list[LocalMethod] | None,
) -> None:
    """Validate caller-declared local attempts, not proof of tool execution."""
    if not task.strip() or not reason.strip():
        raise ValueError("task and a concrete reason for remote exploration are required.")
    if use_case is not None and use_case not in SELECTIVE_USE_CASES:
        raise ValueError(f"use_case must be one of: {', '.join(SELECTIVE_USE_CASES)}.")
    if attempted_local_methods is not None and (
        not isinstance(attempted_local_methods, list)
        or any(method not in LOCAL_METHODS for method in attempted_local_methods)
    ):
        raise ValueError(f"attempted_local_methods must be a list of: {', '.join(LOCAL_METHODS)}.")
    if mode == "selective":
        if use_case is None:
            raise ValueError("Selective exploration requires an approved use_case.")
        if not attempted_local_methods:
            raise ValueError("Selective exploration requires at least one attempted_local_methods entry.")


def exploration_deadline_seconds() -> float:
    try:
        value = float(os.environ.get("SOURCE_SCOUT_EXPLORATION_DEADLINE_SECONDS", "240"))
        return min(value, DEFAULT_DEADLINE_SECONDS) if math.isfinite(value) and value > 0 else 240.0
    except ValueError:
        return DEFAULT_DEADLINE_SECONDS
