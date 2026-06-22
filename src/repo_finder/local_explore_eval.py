import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import catalog, fastcontext, lmstudio

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "golden"
SUITE_ALIASES = {
    "repo-finder": "local_explore_repo_finder_v1.json",
    "local-explore-repo-finder": "local_explore_repo_finder_v1.json",
}
PROMPT_VERSION = fastcontext.PROMPT_VERSION
ANALYZER_VERSION = "local-explore-eval-v1"
DEFAULT_PATH_HIT_RATE = 0.75
DEFAULT_LINE_OVERLAP_RATE = 0.5
DEFAULT_MAX_BAD_CITATIONS_PER_TASK = 3


@dataclass(frozen=True)
class ExpectedCitation:
    path: str
    start_line: int | None = None
    end_line: int | None = None
    required: bool = True


@dataclass(frozen=True)
class ReturnedCitation:
    path: str
    start_line: int | None = None
    end_line: int | None = None
    raw: str = ""


def load_suite(suite: str) -> dict[str, Any]:
    path = _suite_path(suite)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read local exploration suite '{suite}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Local exploration suite '{suite}' is not valid JSON: {exc}") from exc
    return validate_suite(parsed)


def validate_suite(raw_suite: dict[str, Any]) -> dict[str, Any]:
    suite_id = str(raw_suite.get("suite_id", "")).strip()
    if not suite_id:
        raise ValueError("Local exploration suite requires suite_id.")
    tasks = raw_suite.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Local exploration suite requires a non-empty tasks list.")

    validated_tasks = [_validate_task(task, index) for index, task in enumerate(tasks, start=1)]
    return {
        "suite_id": suite_id,
        "description": str(raw_suite.get("description", "")),
        "default_project_path": str(raw_suite.get("default_project_path", ".")),
        "pass_threshold": _validate_threshold(raw_suite.get("pass_threshold", {})),
        "tasks": validated_tasks,
    }


async def run_local_explore_eval(
    suite: str,
    max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
    label: str | None = None,
    output_path: Path | None = None,
    limit_tasks: int | None = None,
) -> dict[str, Any]:
    loaded = load_suite(suite)
    report = await evaluate_suite(
        loaded,
        max_turns=max_turns,
        label=label,
        limit_tasks=limit_tasks,
    )
    path = output_path or default_report_path(str(loaded["suite_id"]), label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report_path"] = str(path)
    return report


async def evaluate_suite(
    suite: dict[str, Any],
    max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
    label: str | None = None,
    limit_tasks: int | None = None,
) -> dict[str, Any]:
    if max_turns < 1:
        raise ValueError("max_turns must be at least 1.")
    if limit_tasks is not None and limit_tasks < 1:
        raise ValueError("limit_tasks must be at least 1.")

    tasks = list(suite["tasks"])
    if limit_tasks is not None:
        tasks = tasks[:limit_tasks]

    task_reports = []
    for task in tasks:
        task_reports.append(
            await _evaluate_task(
                task,
                default_project_path=str(suite["default_project_path"]),
                max_turns=max_turns,
            )
        )

    metrics = _metrics(task_reports)
    passed = _passes_threshold(metrics, suite["pass_threshold"])
    return {
        "suite_id": suite["suite_id"],
        "description": suite.get("description", ""),
        "label": label,
        "max_turns": max_turns,
        "model_id": lmstudio.get_config().fastcontext_model,
        "prompt_version": PROMPT_VERSION,
        "analyzer_version": ANALYZER_VERSION,
        "timestamp": datetime.now(UTC).isoformat(),
        "passed": passed,
        "pass_threshold": suite["pass_threshold"],
        "metrics": metrics,
        "tasks": task_reports,
    }


def default_report_path(suite_id: str, label: str | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{_safe_label(label)}" if label else ""
    return catalog.ensure_home() / "local_explore_eval_runs" / suite_id / f"{timestamp}{suffix}.json"


def _suite_path(suite: str) -> Path:
    candidate = Path(suite)
    if candidate.exists():
        return candidate
    filename = SUITE_ALIASES.get(suite, suite)
    path = GOLDEN_DIR / filename
    if path.exists():
        return path
    raise ValueError(f"Unknown local exploration suite '{suite}'.")


def _validate_task(task: Any, index: int) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise ValueError(f"Task {index} must be an object.")
    task_id = str(task.get("id", "")).strip()
    task_text = str(task.get("task", "")).strip()
    if not task_id or not task_text:
        raise ValueError(f"Task {index} requires id and task.")
    expected = _expected_citations(task.get("expected_citations"), task_id, required=True)
    acceptable = _expected_citations(
        task.get("acceptable_citations", []),
        task_id,
        required=False,
    )
    return {
        "id": task_id,
        "task": task_text,
        "project_path": str(task.get("project_path", "")).strip(),
        "expected_citations": expected,
        "acceptable_citations": acceptable,
        "manual_search_terms": _string_list(task.get("manual_search_terms")),
        "notes": str(task.get("notes", "")),
    }


def _validate_threshold(raw: Any) -> dict[str, Any]:
    threshold = raw if isinstance(raw, dict) else {}
    return {
        "path_hit_rate": _float_threshold(
            threshold.get("path_hit_rate"),
            DEFAULT_PATH_HIT_RATE,
        ),
        "line_overlap_rate": _float_threshold(
            threshold.get("line_overlap_rate"),
            DEFAULT_LINE_OVERLAP_RATE,
        ),
        "max_bad_citations_per_task": int(
            threshold.get(
                "max_bad_citations_per_task",
                DEFAULT_MAX_BAD_CITATIONS_PER_TASK,
            )
        ),
    }


async def _evaluate_task(
    task: dict[str, Any],
    *,
    default_project_path: str,
    max_turns: int,
) -> dict[str, Any]:
    project_root = _resolve_project_path(str(task.get("project_path") or default_project_path))
    started = time.perf_counter()
    error: str | None = None
    result: Any = None
    try:
        result = await fastcontext.explore_local_project(
            task=str(task["task"]),
            project_path=project_root,
            max_turns=max_turns,
        )
    except Exception as exc:
        error = str(exc)
    duration_seconds = round(time.perf_counter() - started, 4)

    returned_paths = list(getattr(result, "evidence_paths", [])) if result else []
    returned_citations = [_parse_returned_citation(path) for path in returned_paths]
    expected = [
        _expected_from_dict(raw)
        for raw in task["expected_citations"]
        if isinstance(raw, dict)
    ]
    acceptable = [
        _expected_from_dict(raw)
        for raw in task["acceptable_citations"]
        if isinstance(raw, dict)
    ]
    manual = _manual_search_proxy(
        project_root,
        _manual_terms(task),
    )
    scoring = _score_citations(project_root, expected, acceptable, returned_citations)
    tool_trace = list(getattr(result, "tool_trace", [])) if result else []
    result_status = str(getattr(result, "status", "")) if result else ""
    report = {
        "id": task["id"],
        "task": task["task"],
        "project_path": str(project_root),
        "status": "failed" if error else result_status or "completed",
        "error": error,
        "duration_seconds": duration_seconds,
        "expected_citations": task["expected_citations"],
        "acceptable_citations": task["acceptable_citations"],
        "returned_citations": returned_paths,
        "notes": list(getattr(result, "notes", [])) if result else [],
        "tool_trace": tool_trace,
        "turn_count": len(tool_trace),
        "tool_call_count": sum(
            int(turn.get("tool_call_count", 0))
            for turn in tool_trace
            if isinstance(turn, dict)
        ),
        "manual_search": manual,
        **scoring,
    }
    report["manual_search_file_reduction"] = _reduction_ratio(
        int(manual["file_count"]),
        int(report["returned_file_count"]),
    )
    report["passed"] = (
        error is None
        and result_status == "completed"
        and bool(report["any_expected_path_hit"])
        and bool(report["any_line_overlap_hit"])
        and int(report["invalid_citation_count"]) == 0
    )
    report["failure_buckets"] = _failure_bucket_flags(report)
    return report


def _resolve_project_path(raw_path: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw_path or "."))
    path = Path(expanded)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _manual_terms(task: dict[str, Any]) -> list[str]:
    terms = [term for term in task["manual_search_terms"] if term.strip()]
    if terms:
        return terms
    return _default_terms(str(task["task"]))


def _manual_search_proxy(project_root: Path, terms: list[str]) -> dict[str, Any]:
    files: set[str] = set()
    match_count = 0
    truncated = False
    term_reports = []
    for term in terms:
        result = fastcontext.grep_paths(
            project_root,
            re.escape(term),
            limit=200,
        )
        matches = result["matches"] if isinstance(result.get("matches"), list) else []
        term_files = {str(match.get("path")) for match in matches if isinstance(match, dict)}
        files.update(term_files)
        match_count += len(matches)
        truncated = truncated or bool(result.get("truncated"))
        term_reports.append(
            {
                "term": term,
                "file_count": len(term_files),
                "match_count": len(matches),
                "truncated": bool(result.get("truncated")),
            }
        )
    return {
        "terms": terms,
        "file_count": len(files),
        "match_count": match_count,
        "truncated": truncated,
        "term_reports": term_reports,
    }


def _score_citations(
    project_root: Path,
    expected: list[ExpectedCitation],
    acceptable: list[ExpectedCitation],
    returned: list[ReturnedCitation],
) -> dict[str, Any]:
    expected_paths = {citation.path for citation in expected}
    accepted_paths = expected_paths | {citation.path for citation in acceptable}
    returned_paths = {citation.path for citation in returned}
    accepted_for_precision = accepted_paths or returned_paths
    missing_expected_paths = sorted(expected_paths - returned_paths)
    unexpected_citations = [
        citation.raw
        for citation in returned
        if citation.path not in accepted_paths
    ]
    invalid_citations = [
        citation.raw
        for citation in returned
        if _invalid_citation_reason(project_root, citation) is not None
    ]
    invalid_details = [
        {
            "citation": citation.raw,
            "reason": _invalid_citation_reason(project_root, citation),
        }
        for citation in returned
        if _invalid_citation_reason(project_root, citation) is not None
    ]
    path_hits = [
        citation.path
        for citation in expected
        if citation.path in returned_paths
    ]
    line_hits = [
        citation.path
        for citation in expected
        if _has_line_overlap(citation, returned)
    ]
    required_expected = [citation for citation in expected if citation.required]
    all_required_paths_hit = all(citation.path in returned_paths for citation in required_expected)
    file_true_positive = len(returned_paths & accepted_for_precision)
    file_precision = _ratio(file_true_positive, len(returned_paths))
    file_recall = _ratio(len(returned_paths & expected_paths), len(expected_paths))
    expected_lines = _line_set(expected)
    accepted_lines = _line_set([*expected, *acceptable])
    returned_lines = _line_set(returned)
    if not accepted_lines:
        accepted_lines = returned_lines
    line_precision = _ratio(len(returned_lines & accepted_lines), len(returned_lines))
    line_recall = _ratio(len(returned_lines & expected_lines), len(expected_lines))
    citation_count = len(returned)
    over_budget = (
        citation_count > fastcontext.MAX_FINAL_CITATIONS
        or len(returned_paths) > fastcontext.MAX_FINAL_FILES
    )
    budget_violation_count = max(0, citation_count - fastcontext.MAX_FINAL_CITATIONS) + max(
        0,
        len(returned_paths) - fastcontext.MAX_FINAL_FILES,
    )
    expected_label_count = max(1, len(expected_paths))
    return {
        "returned_citation_count": citation_count,
        "returned_file_count": len(returned_paths),
        "expected_path_hits": sorted(set(path_hits)),
        "line_overlap_hits": sorted(set(line_hits)),
        "missing_expected_paths": missing_expected_paths,
        "unexpected_citations": unexpected_citations,
        "bad_citation_count": len(unexpected_citations),
        "invalid_citations": invalid_citations,
        "invalid_citation_count": len(invalid_citations),
        "invalid_citation_details": invalid_details,
        "file_precision": file_precision,
        "file_recall": file_recall,
        "file_f1": _f1(file_precision, file_recall),
        "line_precision": line_precision,
        "line_recall": line_recall,
        "line_f1": _f1(line_precision, line_recall),
        "file_explore_score": _explore_score(
            file_precision,
            file_recall,
            citation_count,
            expected_label_count,
        ),
        "line_explore_score": _explore_score(
            line_precision,
            line_recall,
            citation_count,
            expected_label_count,
        ),
        "explore_score": round(
            (
                _explore_score(file_precision, file_recall, citation_count, expected_label_count)
                + _explore_score(line_precision, line_recall, citation_count, expected_label_count)
            )
            / 2,
            4,
        ),
        "over_budget": over_budget,
        "citation_budget_violation_count": budget_violation_count,
        "valid_citation_rate": _ratio(len(returned) - len(invalid_citations), len(returned)),
        "any_expected_path_hit": bool(path_hits),
        "all_required_paths_hit": all_required_paths_hit,
        "any_line_overlap_hit": bool(line_hits),
    }


def _failure_bucket_flags(report: dict[str, Any]) -> dict[str, bool]:
    status = str(report.get("status") or "")
    tool_trace = report.get("tool_trace", [])
    returned_file_count = int(report.get("returned_file_count", 0))
    expected_path_hit = bool(report.get("any_expected_path_hit"))
    line_overlap_hit = bool(report.get("any_line_overlap_hit"))
    flags = {
        "no_tool_calls": int(report.get("tool_call_count", 0)) == 0,
        "wrong_file": returned_file_count > 0 and not expected_path_hit,
        "right_file_wrong_range": expected_path_hit and not line_overlap_hit,
        "invalid_final_citation": int(report.get("invalid_citation_count", 0)) > 0,
        "unsupported_final_citation": _unsupported_citation_count(report) > 0,
        "final_answer_oscillation": _has_final_answer_oscillation(tool_trace),
        "fallback_observations": status == "fallback_observations",
    }
    return flags


def _has_final_answer_oscillation(tool_trace: Any) -> bool:
    if not isinstance(tool_trace, list):
        return False
    seen_disabled_after_tools = False
    seen_tool_turn = False
    for turn in tool_trace:
        if not isinstance(turn, dict):
            continue
        tools_enabled = bool(turn.get("tools_enabled", False))
        tool_call_count = int(turn.get("tool_call_count", 0))
        if seen_disabled_after_tools and tools_enabled:
            return True
        if seen_tool_turn and not tools_enabled:
            seen_disabled_after_tools = True
        if tool_call_count > 0:
            seen_tool_turn = True
    return False


def _invalid_citation_reason(project_root: Path, citation: ReturnedCitation) -> str | None:
    path = project_root / citation.path
    if not path.is_file():
        return "missing_file"
    if citation.start_line is None:
        return None
    start_line = citation.start_line
    end_line = citation.end_line if citation.end_line is not None else start_line
    if start_line <= 0 or end_line <= 0:
        return "non_positive_range"
    if end_line < start_line:
        return "reversed_range"
    line_count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    if start_line > line_count or end_line > line_count:
        return "range_past_eof"
    return None


def _has_line_overlap(expected: ExpectedCitation, returned: list[ReturnedCitation]) -> bool:
    for citation in returned:
        if citation.path != expected.path:
            continue
        if expected.start_line is None or citation.start_line is None:
            return True
        expected_end = expected.end_line or expected.start_line
        returned_end = citation.end_line or citation.start_line
        if expected.start_line <= returned_end and citation.start_line <= expected_end:
            return True
    return False


def _line_set(citations: list[Any]) -> set[tuple[str, int]]:
    lines: set[tuple[str, int]] = set()
    for citation in citations:
        if citation.start_line is None:
            continue
        end_line = citation.end_line if citation.end_line is not None else citation.start_line
        if end_line < citation.start_line:
            continue
        for line in range(citation.start_line, end_line + 1):
            lines.add((citation.path, line))
    return lines


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _f1(precision: float, recall: float) -> float:
    if precision + recall <= 0:
        return 0.0
    return round((2 * precision * recall) / (precision + recall), 4)


def _explore_score(
    precision: float,
    recall: float,
    citation_count: int,
    expected_count: int,
    *,
    beta: float = 0.5,
    penalty_weight: float = 0.1,
) -> float:
    if precision + recall <= 0:
        f_beta = 0.0
    else:
        beta_squared = beta**2
        f_beta = ((1 + beta_squared) * precision * recall) / ((beta_squared * precision) + recall)
    label_count = max(1, expected_count)
    penalty = penalty_weight * max(0.0, (citation_count - label_count) / label_count)
    return round(f_beta - penalty, 4)


def _metrics(task_reports: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(task_reports)
    completed = sum(1 for task in task_reports if task["status"] == "completed")
    passed = sum(1 for task in task_reports if task["passed"])
    path_hits = sum(1 for task in task_reports if task["any_expected_path_hit"])
    line_hits = sum(1 for task in task_reports if task["any_line_overlap_hit"])
    bad_citations = sum(int(task["bad_citation_count"]) for task in task_reports)
    invalid_citations = sum(int(task["invalid_citation_count"]) for task in task_reports)
    citation_budget_violations = sum(
        int(task.get("citation_budget_violation_count", 0))
        for task in task_reports
    )
    over_budget_tasks = sum(1 for task in task_reports if bool(task.get("over_budget", False)))
    unsupported_citations = sum(_unsupported_citation_count(task) for task in task_reports)
    total_duration = sum(float(task["duration_seconds"]) for task in task_reports)
    manual_files = [
        int(task["manual_search"]["file_count"])
        for task in task_reports
        if int(task["manual_search"]["file_count"]) > 0
    ]
    reductions = [
        float(task["manual_search_file_reduction"])
        for task in task_reports
        if task["manual_search_file_reduction"] is not None
    ]
    failure_bucket_counts = _failure_bucket_counts(task_reports)
    return {
        "task_count": total,
        "completed_tasks": completed,
        "failed_tasks": total - completed,
        "passed_tasks": passed,
        "path_hits": path_hits,
        "line_overlap_hits": line_hits,
        "path_hit_rate": round(path_hits / total, 4) if total else 0.0,
        "line_overlap_rate": round(line_hits / total, 4) if total else 0.0,
        "bad_citation_count": bad_citations,
        "invalid_citation_count": invalid_citations,
        "unsupported_citation_count": unsupported_citations,
        "average_citation_count": _average_metric(task_reports, "returned_citation_count"),
        "over_budget_task_count": over_budget_tasks,
        "citation_budget_violation_count": citation_budget_violations,
        "average_file_precision": _average_metric(task_reports, "file_precision"),
        "average_file_recall": _average_metric(task_reports, "file_recall"),
        "average_file_f1": _average_metric(task_reports, "file_f1"),
        "average_line_precision": _average_metric(task_reports, "line_precision"),
        "average_line_recall": _average_metric(task_reports, "line_recall"),
        "average_line_f1": _average_metric(task_reports, "line_f1"),
        "average_file_explore_score": _average_metric(task_reports, "file_explore_score"),
        "average_line_explore_score": _average_metric(task_reports, "line_explore_score"),
        "average_explore_score": _average_metric(task_reports, "explore_score"),
        "average_valid_citation_rate": _average_metric(task_reports, "valid_citation_rate"),
        "total_duration_seconds": round(total_duration, 4),
        "average_duration_seconds": round(total_duration / total, 4) if total else 0.0,
        "average_manual_search_files": round(sum(manual_files) / len(manual_files), 4)
        if manual_files
        else 0.0,
        "average_file_reduction": round(sum(reductions) / len(reductions), 4)
        if reductions
        else 0.0,
        "failure_bucket_counts": failure_bucket_counts,
        "tool_call_count": sum(int(task["tool_call_count"]) for task in task_reports),
        "turn_count": sum(int(task["turn_count"]) for task in task_reports),
    }


def _failure_bucket_counts(task_reports: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "no_tool_calls": 0,
        "wrong_file": 0,
        "right_file_wrong_range": 0,
        "invalid_final_citation": 0,
        "unsupported_final_citation": 0,
        "final_answer_oscillation": 0,
        "fallback_observations": 0,
    }
    for task in task_reports:
        flags = task.get("failure_buckets", {})
        if not isinstance(flags, dict):
            continue
        for bucket in counts:
            if bool(flags.get(bucket)):
                counts[bucket] += 1
    return counts


def _unsupported_citation_count(task: dict[str, Any]) -> int:
    count = 0
    for turn in task.get("tool_trace", []):
        if not isinstance(turn, dict):
            continue
        notes = turn.get("validation_notes", [])
        if not isinstance(notes, list):
            continue
        count += sum(
            1
            for note in notes
            if isinstance(note, str) and "unsupported" in note.lower()
        )
    return count


def _average_metric(task_reports: list[dict[str, Any]], key: str) -> float:
    values = [float(task[key]) for task in task_reports if key in task]
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)


def _passes_threshold(metrics: dict[str, Any], threshold: dict[str, Any]) -> bool:
    max_bad = int(threshold["max_bad_citations_per_task"]) * int(metrics["task_count"])
    return (
        int(metrics["failed_tasks"]) == 0
        and float(metrics["path_hit_rate"]) >= float(threshold["path_hit_rate"])
        and float(metrics["line_overlap_rate"]) >= float(threshold["line_overlap_rate"])
        and int(metrics["bad_citation_count"]) <= max_bad
        and int(metrics["invalid_citation_count"]) == 0
    )


def _expected_citations(value: Any, task_id: str, *, required: bool) -> list[dict[str, Any]]:
    if not value and not required:
        return []
    if not isinstance(value, list) or not value:
        raise ValueError(f"Task {task_id} requires non-empty expected_citations.")
    citations = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"Task {task_id} expected_citations entries must be objects.")
        path = str(item.get("path", "")).strip().replace("\\", "/")
        if not path:
            raise ValueError(f"Task {task_id} expected citation requires path.")
        citations.append(
            {
                "path": path,
                "start_line": _optional_int(item.get("start_line")),
                "end_line": _optional_int(item.get("end_line")),
                "required": bool(item.get("required", True)),
                "reason": str(item.get("reason", "")),
            }
        )
    return citations


def _expected_from_dict(value: dict[str, Any]) -> ExpectedCitation:
    return ExpectedCitation(
        path=str(value["path"]),
        start_line=_optional_int(value.get("start_line")),
        end_line=_optional_int(value.get("end_line")),
        required=bool(value.get("required", True)),
    )


def _parse_returned_citation(raw: str) -> ReturnedCitation:
    match = re.match(r"(?P<path>.*?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$", raw)
    if match is None:
        return ReturnedCitation(path=raw.replace("\\", "/"), raw=raw)
    return ReturnedCitation(
        path=match.group("path").replace("\\", "/"),
        start_line=_optional_int(match.group("start")),
        end_line=_optional_int(match.group("end")),
        raw=raw,
    )


def _default_terms(task: str) -> list[str]:
    terms = []
    for raw_term in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,}", task):
        normalized = raw_term.lower()
        if normalized in {"find", "where", "code", "repo", "this", "that", "with"}:
            continue
        terms.append(raw_term)
    return sorted(set(terms), key=str.lower)[:6]


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Expected a list of strings.")
    return [str(item) for item in value]


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_threshold(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _reduction_ratio(manual_file_count: int, evidence_file_count: int) -> float | None:
    if manual_file_count <= 0:
        return None
    return round(1 - (evidence_file_count / manual_file_count), 4)


def _safe_label(label: str | None) -> str:
    if not label:
        return ""
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in label)
