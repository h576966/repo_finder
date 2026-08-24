from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import catalog_core

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "golden"


def load_suite_json(
    suite: str,
    aliases: dict[str, str],
    *,
    suite_label: str,
    title_label: str,
) -> dict[str, Any]:
    path = suite_path(suite, aliases, suite_label=suite_label)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read {suite_label} suite '{suite}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{title_label} suite '{suite}' is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{title_label} suite '{suite}' must be a JSON object.")
    return parsed


def suite_path(suite: str, aliases: dict[str, str], *, suite_label: str) -> Path:
    candidate = Path(suite)
    if candidate.exists():
        return candidate
    filename = aliases.get(suite, suite)
    path = GOLDEN_DIR / filename
    if path.exists():
        return path
    raise ValueError(f"Unknown {suite_label} suite '{suite}'.")


def default_report_path(run_dir: str, suite_id: str, label: str | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{safe_label(label)}" if label else ""
    return catalog_core.ensure_home() / run_dir / suite_id / f"{timestamp}{suffix}.json"


def write_report(report: dict[str, Any], path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report_path"] = str(path)
    return report


def safe_label(label: str | None) -> str:
    if not label:
        return ""
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in label)


def run_id(suite_id: str, label: str | None) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{safe_label(suite_id)}_{safe_label(label)}"
