"""Caller-supplied navigation hints, validated inside the existing source policy."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .fastcontext_constants import MAX_READ_FILE_BYTES
from .fastcontext_types import FastContextError
from .path_safety import PathSafetyError, resolve_under_root


@dataclass(frozen=True)
class InvestigationAnchor:
    path: str
    start_line: int | None = None
    end_line: int | None = None
    symbol: str | None = None


def parse_cli_anchor(value: str) -> InvestigationAnchor:
    match = re.fullmatch(r"(.+?)(?::([0-9]+)(?:-([0-9]+))?)?", value)
    if not match:
        raise ValueError("Anchor must be path[:start[-end]].")
    return InvestigationAnchor(
        match[1], int(match[2]) if match[2] else None, int(match[3]) if match[3] else None
    )


def anchor_seed(root: Path, anchors: list[InvestigationAnchor]) -> dict[str, Any]:
    if not 1 <= len(anchors) <= 6:
        raise FastContextError("Supply 1..6 start anchors.")
    validated = []
    for anchor in anchors:
        try:
            if len(anchor.path) > 500 or Path(anchor.path).is_absolute():
                raise FastContextError("Anchor path must be relative to source_root.")
            path, relative = resolve_under_root(root, anchor.path)
            if relative != anchor.path or path.is_symlink() or not path.is_file():
                raise FastContextError("Anchor must name an existing literal regular source file.")
            with path.open("rb") as stream:
                raw = stream.read(MAX_READ_FILE_BYTES + 1)
            if len(raw) > MAX_READ_FILE_BYTES or b"\0" in raw:
                raise FastContextError("Anchor exceeds source read bounds or is binary.")
        except (OSError, PathSafetyError) as exc:
            raise FastContextError("Anchor is outside permitted source or unavailable.") from exc
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = anchor.start_line or 1
        end = anchor.end_line if anchor.end_line is not None else start
        if anchor.start_line == 0 or not 1 <= start <= end <= len(lines):
            raise FastContextError("Anchor line range does not exist in its source file.")
        if end - start + 1 > 160:
            raise FastContextError("Anchor range exceeds 160 lines.")
        if anchor.symbol is not None:
            if (
                not anchor.symbol
                or len(anchor.symbol) > 200
                or anchor.symbol not in raw.decode("utf-8", errors="replace")
            ):
                raise FastContextError("Anchor symbol hint was not found in the selected file.")
        validated.append(asdict(anchor))
    paths = list(dict.fromkeys(item["path"] for item in validated))
    # These hints are not observations. The investigator must still Read before
    # citing them; no broad seed search is performed when anchors are present.
    return {
        "start_anchors": validated,
        "priority_paths": paths,
        "likely_source_files": paths,
        "task_type": "source_relation",
        "priority_file_matches": [
            {"path": item["path"], "line": item["start_line"] or 1} for item in validated
        ],
    }
