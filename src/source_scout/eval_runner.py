import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import catalog

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "golden"
SUITE_ALIASES = {
    "ui-reuse": "ui_reuse_v1.json",
    "nextjs-backend": "nextjs_backend_v1.json",
}
UI_REUSE_PASSING_TOP1 = 6
UI_REUSE_PASSING_TOP3 = 8


def load_suite(suite: str) -> dict[str, Any]:
    path = _suite_path(suite)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read eval suite '{suite}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Eval suite '{suite}' is not valid JSON: {exc}") from exc
    return validate_suite(parsed)


def validate_suite(raw_suite: dict[str, Any]) -> dict[str, Any]:
    suite_id = str(raw_suite.get("suite_id", "")).strip()
    if not suite_id:
        raise ValueError("Eval suite requires suite_id.")
    tasks = raw_suite.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Eval suite requires a non-empty tasks list.")

    validated_tasks = []
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise ValueError(f"Task {index} must be an object.")
        validated_tasks.append(_validate_task(task, index))

    return {
        "suite_id": suite_id,
        "description": str(raw_suite.get("description", "")),
        "tasks": validated_tasks,
    }


def run_eval(
    suite: str,
    top_k: int,
    label: str | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    loaded = load_suite(suite)
    report = evaluate_suite(loaded, top_k=top_k, label=label)
    path = output_path or default_report_path(str(loaded["suite_id"]), label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report_path"] = str(path)
    catalog.record_analysis_run(
        "eval",
        "completed" if report["passed"] else "failed",
        {
            "suite_id": loaded["suite_id"],
            "label": label,
            "top_k": top_k,
            "metrics": report["metrics"],
            "report_path": str(path),
        },
    )
    return report


def evaluate_suite(suite: dict[str, Any], top_k: int, label: str | None = None) -> dict[str, Any]:
    if top_k < 1:
        raise ValueError("top_k must be at least 1.")

    task_reports = [_evaluate_task(task, top_k) for task in suite["tasks"]]
    metrics = _metrics(task_reports)
    passed = _passes_threshold(str(suite["suite_id"]), metrics)
    return {
        "suite_id": suite["suite_id"],
        "description": suite.get("description", ""),
        "label": label,
        "top_k": top_k,
        "timestamp": datetime.now(UTC).isoformat(),
        "passed": passed,
        "metrics": metrics,
        "tasks": task_reports,
    }


def default_report_path(suite_id: str, label: str | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{_safe_label(label)}" if label else ""
    return catalog.ensure_home() / "eval_runs" / suite_id / f"{timestamp}{suffix}.json"


def _suite_path(suite: str) -> Path:
    candidate = Path(suite)
    if candidate.exists():
        return candidate
    filename = SUITE_ALIASES.get(suite, suite)
    path = GOLDEN_DIR / filename
    if path.exists():
        return path
    raise ValueError(f"Unknown eval suite '{suite}'.")


def _validate_task(task: dict[str, Any], index: int) -> dict[str, Any]:
    task_id = str(task.get("id", "")).strip()
    task_text = str(task.get("task", "")).strip()
    capability = str(task.get("capability", "")).strip()
    if not task_id or not task_text or not capability:
        raise ValueError(f"Task {index} requires id, task, and capability.")
    expected = _string_list(task.get("expected_repo_ids"))
    acceptable = _string_list(task.get("acceptable_repo_ids"))
    if not expected and not acceptable:
        raise ValueError(f"Task {task_id} requires expected or acceptable repos.")
    return {
        "id": task_id,
        "task": task_text,
        "capability": capability,
        "expected_repo_ids": expected,
        "acceptable_repo_ids": acceptable,
        "avoid_repo_ids": _string_list(task.get("avoid_repo_ids")),
        "required_path_terms_any": _string_list(task.get("required_path_terms_any")),
        "required_dependencies_any": _string_list(task.get("required_dependencies_any")),
        "max_rank_for_hit": int(task.get("max_rank_for_hit", 3)),
        "notes": str(task.get("notes", "")),
    }


def _evaluate_task(task: dict[str, Any], top_k: int) -> dict[str, Any]:
    results = catalog.search_assets(str(task["task"]), max_repos=top_k)
    candidates: list[dict[str, Any]] = []
    first_hit_rank: int | None = None
    blocked_label_match = False
    avoid_violations = 0
    max_rank = int(task["max_rank_for_hit"])

    for rank, candidate in enumerate(results, start=1):
        expected = candidate.repo_id in task["expected_repo_ids"]
        acceptable = candidate.repo_id in task["acceptable_repo_ids"]
        label_match = expected or acceptable
        path_ok = _path_constraint_ok(candidate, task["required_path_terms_any"])
        dependency_ok = _dependency_constraint_ok(candidate, task["required_dependencies_any"])
        evidence_ok = bool(candidate.evidence_paths)
        avoid_violation = (
            rank <= 3
            and candidate.repo_id in task["avoid_repo_ids"]
            and not _avoid_exception(candidate, str(task["capability"]))
        )
        if avoid_violation:
            avoid_violations += 1

        failure_reasons = _failure_reasons(
            label_match=label_match,
            path_ok=path_ok,
            dependency_ok=dependency_ok,
            evidence_ok=evidence_ok,
            avoid_violation=avoid_violation,
        )
        if label_match and rank <= max_rank and (not path_ok or not dependency_ok or not evidence_ok):
            blocked_label_match = True
        if (
            first_hit_rank is None
            and label_match
            and rank <= max_rank
            and path_ok
            and dependency_ok
            and evidence_ok
            and not avoid_violation
        ):
            first_hit_rank = rank

        candidates.append(
            {
                "rank": rank,
                "candidate_id": candidate.candidate_id,
                "repo_id": candidate.repo_id,
                "score": candidate.score,
                "capability": candidate.capability,
                "label_match": label_match,
                "expected_match": expected,
                "acceptable_match": acceptable,
                "entry_paths": candidate.entry_paths,
                "external_dependencies": candidate.external_dependencies,
                "evidence_paths": candidate.evidence_paths,
                "failure_reasons": failure_reasons,
            }
        )

    constraint_failures = 1 if first_hit_rank is None and blocked_label_match else 0
    return {
        "id": task["id"],
        "task": task["task"],
        "capability": task["capability"],
        "max_rank_for_hit": max_rank,
        "first_hit_rank": first_hit_rank,
        "top_1_hit": first_hit_rank == 1,
        "top_3_hit": first_hit_rank is not None and first_hit_rank <= 3,
        "top_5_hit": first_hit_rank is not None and first_hit_rank <= 5,
        "avoid_violations": avoid_violations,
        "constraint_failures": constraint_failures,
        "candidates": candidates,
    }


def _metrics(task_reports: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(task_reports)
    top_1 = sum(1 for task in task_reports if task["top_1_hit"])
    top_3 = sum(1 for task in task_reports if task["top_3_hit"])
    top_5 = sum(1 for task in task_reports if task["top_5_hit"])
    reciprocal_sum = sum(1 / task["first_hit_rank"] for task in task_reports if task["first_hit_rank"])
    avoid_violations = sum(int(task["avoid_violations"]) for task in task_reports)
    constraint_failures = sum(int(task["constraint_failures"]) for task in task_reports)
    return {
        "task_count": total,
        "top_1_hits": top_1,
        "top_3_hits": top_3,
        "top_5_hits": top_5,
        "top_1_hit_rate": round(top_1 / total, 4) if total else 0.0,
        "top_3_hit_rate": round(top_3 / total, 4) if total else 0.0,
        "top_5_hit_rate": round(top_5 / total, 4) if total else 0.0,
        "mrr": round(reciprocal_sum / total, 4) if total else 0.0,
        "avoid_repo_violations": avoid_violations,
        "evidence_constraint_failures": constraint_failures,
    }


def _passes_threshold(suite_id: str, metrics: dict[str, Any]) -> bool:
    if suite_id == "ui-reuse":
        return (
            int(metrics["top_3_hits"]) >= UI_REUSE_PASSING_TOP3
            and int(metrics["top_1_hits"]) >= UI_REUSE_PASSING_TOP1
            and int(metrics["avoid_repo_violations"]) == 0
            and int(metrics["evidence_constraint_failures"]) == 0
        )
    return int(metrics["top_3_hits"]) >= max(1, int(metrics["task_count"]) // 2)


def _path_constraint_ok(candidate: Any, required_terms: list[str]) -> bool:
    if not required_terms:
        return True
    searchable = " ".join(candidate.entry_paths + candidate.evidence_paths).lower()
    return any(term.lower() in searchable for term in required_terms)


def _dependency_constraint_ok(candidate: Any, required_dependencies: list[str]) -> bool:
    if not required_dependencies:
        return True
    available = {dependency.lower() for dependency in candidate.external_dependencies}
    return any(dependency.lower() in available for dependency in required_dependencies)


def _avoid_exception(candidate: Any, capability: str) -> bool:
    if candidate.repo_id == "ufukayyildiz/omnidock":
        return any(path.startswith("src/ui/") for path in candidate.entry_paths + candidate.evidence_paths)
    if (
        candidate.repo_id == "x0ll/Ransomware-Attack-Detection-Using-Machine-Learning"
        and capability == "data-table"
    ):
        text = " ".join(candidate.entry_paths + candidate.evidence_paths).lower()
        return any(term in text for term in ("table", "data-table", "columns"))
    return False


def _failure_reasons(
    *,
    label_match: bool,
    path_ok: bool,
    dependency_ok: bool,
    evidence_ok: bool,
    avoid_violation: bool,
) -> list[str]:
    reasons: list[str] = []
    if not label_match:
        reasons.append("repo_not_labeled_relevant")
    if not path_ok:
        reasons.append("missing_required_path_term")
    if not dependency_ok:
        reasons.append("missing_required_dependency")
    if not evidence_ok:
        reasons.append("missing_evidence_paths")
    if avoid_violation:
        reasons.append("avoid_repo_in_top3")
    return reasons


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Expected a list of strings.")
    return [str(item) for item in value]


def _safe_label(label: str | None) -> str:
    if not label:
        return ""
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in label)
