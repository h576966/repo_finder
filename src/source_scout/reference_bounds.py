"""Presentation bounds independent of evidence completeness and execution status."""

from __future__ import annotations

import json
from typing import Any

MAX_RESPONSE_BYTES = 64_000
MAX_METADATA_BYTES = 8_000
MAX_EXCERPT_BYTES = 6_000
MAX_TASK_CHARS = 2_000


def json_size(value: Any) -> int:
    # ASCII encoding also bounds JSON transports that escape Unicode.
    return len(json.dumps(value, ensure_ascii=True).encode("ascii"))


def metadata_view(value: Any) -> tuple[Any, bool]:
    clipped = False

    def visit(item: Any, depth: int) -> Any:
        nonlocal clipped
        if depth > 6:
            clipped = True
            return None
        if isinstance(item, str):
            clipped |= len(item) > 500
            return item[:500]
        if isinstance(item, dict):
            clipped |= len(item) > 20
            return {str(key)[:100]: visit(val, depth + 1) for key, val in list(item.items())[:20]}
        if isinstance(item, (list, tuple)):
            clipped |= len(item) > 20
            return [visit(val, depth + 1) for val in item[:20]]
        return item

    result = visit(value, 0)
    if json_size(result) > MAX_METADATA_BYTES:
        return {}, True
    return result, clipped


def bounded_response(result: dict[str, Any]) -> dict[str, Any]:
    """Drop complete presentation units, never cut a cited line or identity."""
    if json_size(result) <= MAX_RESPONSE_BYTES:
        return result
    result["truncated"] = True
    result.setdefault("warnings", []).append("Presentation limited to 64000 serialized JSON bytes.")
    for key in ("repository_facts", "target_fit", "license", "manifests", "snippets", "results"):
        if key not in result:
            continue
        if isinstance(result[key], list):
            while result[key] and json_size(result) > MAX_RESPONSE_BYTES:
                result[key].pop()
        else:
            result[key] = {}
        if json_size(result) <= MAX_RESPONSE_BYTES:
            return result
    # Input validation and field limits normally make this unreachable. Fail
    # closed for oversized historical identities instead of misquoting them.
    raise ValueError("Reference response identity exceeds the serialized response budget.")
