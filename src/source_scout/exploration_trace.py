"""Local accounting for one bounded exploration, without inferred prices."""

from __future__ import annotations

from typing import Any


def summarize_requests(trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    requests = [item for item in trajectory if item.get("request_index") is not None]
    usage: dict[str, int | None] = {}
    for name in ("input_tokens", "cached_input_tokens", "output_tokens"):
        values: list[int | None] = []
        for request in requests:
            raw = request.get("usage") or {}
            value = (
                (raw.get("input_tokens_details") or {}).get("cached_tokens")
                if (name == "cached_input_tokens")
                else raw.get(name)
            )
            values.append(value if isinstance(value, int) and not isinstance(value, bool) else None)
        usage[name] = sum(v for v in values if v is not None) if values and None not in values else None
    return {
        "request_count": len(requests),
        "known_retries": 0,
        "returned_models": sorted({r["response_model"] for r in requests if r.get("response_model")}),
        "usage": usage,
        "usage_complete": bool(requests) and all(value is not None for value in usage.values()),
        "cost": None,
        "usage_note": (
            "Cached input is a subset of input; never add it to input totals. Missing usage is unknown."
        ),
    }
