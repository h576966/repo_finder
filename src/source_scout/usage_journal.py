"""Local usage facts and explicit Codex feedback; no automatic quality scoring."""

import hashlib
import inspect
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, cast

SCHEMA = "source-scout-usage-v1"
OUTCOMES = ("helped", "partly_helped", "did_not_help", "unassessed")
_kind: ContextVar[str | None] = ContextVar("usage_kind", default=None)
_logger = logging.getLogger(__name__)


@contextmanager
def evaluation_usage() -> Iterator[None]:
    token = _kind.set("evaluation")
    try:
        yield
    finally:
        _kind.reset(token)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_new(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n")
    temporary.rename(path)


def _summary(result: Any) -> dict[str, Any]:
    raw = asdict(result)
    keys = (
        "status",
        "abstention_reason",
        "truncated",
        "warnings",
        "missing_evidence",
        "missing_context",
        "stop_reason",
        "run_id",
        "report_path",
        "model_id",
        "prompt_version",
        "analyzer_version",
        "schema_version",
        "evidence_paths",
        "reference_id",
        "snapshot_id",
        "commit_sha",
        "path",
        "content_sha256",
        "target_profile_fingerprint",
    )
    summary = {key: raw[key] for key in keys if key in raw}
    if "results" in raw:
        summary["results"] = [
            {
                key: item[key]
                for key in (
                    "reference_id",
                    "snapshot_id",
                    "repo_id",
                    "commit_sha",
                    "path",
                    "content_sha256",
                    "relevance_score",
                    "matched_terms",
                )
            }
            for item in raw["results"]
        ]
    if "snippets" in raw:
        summary["snippets"] = [
            {key: item[key] for key in ("path", "start_line", "end_line", "content_sha256")}
            for item in raw["snippets"]
        ]
    return summary


def _record(
    operation: str,
    inputs: dict[str, Any],
    started: float,
    result: Any,
    error: BaseException | None,
) -> None:
    try:
        configured = os.environ.get("SOURCE_SCOUT_HOME")
        root = (
            Path(configured)
            if configured
            else Path(inputs.get("project_path") or Path.cwd()) / ".source_scout"
        )
        run_root = root.expanduser().resolve() / "usage" / uuid.uuid4().hex
        run_root.mkdir(parents=True, exist_ok=False)
        report_path = run_root / "result.json"
        # Fingerprint the actual Python source, including uncommitted edits.
        digest = hashlib.sha256()
        for source in sorted(Path(__file__).parent.glob("*.py")):
            digest.update(source.name.encode() + b"\0" + source.read_bytes() + b"\0")
        payload = {
            "schema_version": SCHEMA,
            "usage_id": run_root.name,
            "timestamp": _now(),
            "kind": _kind.get() or os.environ.get("SOURCE_SCOUT_USAGE_KIND", "usage"),
            "operation": operation,
            "cwd": str(Path.cwd()),
            "inputs": inputs,
            "code_sha256": digest.hexdigest(),
            "duration_seconds": round(time.monotonic() - started, 4),
            "result": _summary(result) if result is not None else None,
            "error": {"type": type(error).__name__} if error is not None else None,
        }
        _write_new(report_path, payload)
        if result is not None:
            result.usage = {"report_path": str(report_path)}
        if error is not None:
            error.usage = {"report_path": str(report_path)}  # type: ignore[attr-defined]
            _logger.warning("Usage recorded for failed %s: %s", operation, report_path)
    except (OSError, ValueError, TypeError) as exc:
        # Local journaling must not replace an operation's result or exception.
        if result is not None:
            result.usage = {"error": f"Usage journal unavailable: {type(exc).__name__}"}
        _logger.warning("Usage journal unavailable for %s: %s", operation, type(exc).__name__)


def journaled[F: Callable[..., Any]](function: F) -> F:
    signature = inspect.signature(function)

    def inputs_for(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        # Credentials, transports and source excerpts never enter the usage record.
        return {
            key: str(value) if isinstance(value, Path) else value
            for key, value in bound.arguments.items()
            if key
            in {
                "task",
                "reference_id",
                "project_path",
                "target_project_path",
                "max_results",
                "max_turns",
                "use_case",
                "attempted_local_methods",
                "reason",
            }
        }

    @wraps(function)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        inputs, started = inputs_for(args, kwargs), time.monotonic()
        result, error = None, None
        try:
            result = function(*args, **kwargs)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            _record(function.__name__, inputs, started, result, error)

    @wraps(function)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        inputs, started = inputs_for(args, kwargs), time.monotonic()
        result, error = None, None
        try:
            result = await function(*args, **kwargs)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            _record(function.__name__, inputs, started, result, error)

    return cast(F, async_wrapper if inspect.iscoroutinefunction(function) else sync_wrapper)


def write_feedback(
    report_path: str | Path,
    outcome: str,
    observation: str,
    evidence: str = "",
) -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise ValueError(f"Outcome must be one of {OUTCOMES}.")
    if not observation.strip() or len(observation) > 2000 or len(evidence) > 4000:
        raise ValueError("Supply an observation (1-2000 characters) and evidence of at most 4000 characters.")
    path = Path(report_path).expanduser().resolve(strict=True)
    if path.stat().st_size > 128_000:
        raise ValueError("Usage report is too large.")
    report = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != SCHEMA
        or path.name != "result.json"
        or report.get("usage_id") != path.parent.name
    ):
        raise ValueError("Expected the result.json path returned in usage.report_path.")
    feedback = {
        "schema_version": "source-scout-feedback-v1",
        "usage_id": report["usage_id"],
        "timestamp": _now(),
        "assessor": "codex",
        "outcome": outcome,
        "observation": observation.strip(),
        "evidence": evidence.strip() or None,
    }
    feedback_path = path.parent / f"feedback-{uuid.uuid4().hex}.json"
    _write_new(feedback_path, feedback)
    return {"feedback_path": str(feedback_path), **feedback}
