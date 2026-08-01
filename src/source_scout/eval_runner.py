import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import assessment_rules, assessor, bundles, catalog, eval_support
from .target_profile import TargetProfileV1, build_target_profile

SUITE_ALIASES = {
    "ui-reuse": "ui_reuse_v1.json",
    "nextjs-backend": "nextjs_backend_v1.json",
    "personal-code": "personal_code_v1.json",
    "core-holdout": "core_reuse_holdout_v1.json",
}
UI_REUSE_PASSING_TOP1 = 6
UI_REUSE_PASSING_TOP3 = 8
FastContextPolicy = Literal["auto", "always", "never"]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_suite(suite: str) -> dict[str, Any]:
    parsed = eval_support.load_suite_json(
        suite,
        SUITE_ALIASES,
        suite_label="eval",
        title_label="Eval",
    )
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
    eval_support.write_report(report, path)
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


async def run_reuse_loop_report(
    suite: str,
    top_k: int,
    label: str | None = None,
    output_path: Path | None = None,
    *,
    limit_tasks: int | None = None,
    fastcontext_policy: FastContextPolicy = "never",
    max_evidence_rounds: int = 0,
    force_assessment: bool = True,
    assessment_runtime: assessor.AssessmentRuntime | None = None,
) -> dict[str, Any]:
    loaded = load_suite(suite)
    report = await evaluate_reuse_loop_suite(
        loaded,
        top_k=top_k,
        label=label,
        limit_tasks=limit_tasks,
        fastcontext_policy=fastcontext_policy,
        max_evidence_rounds=max_evidence_rounds,
        force_assessment=force_assessment,
        assessment_runtime=assessment_runtime,
    )
    path = output_path or reuse_loop_report_path(str(loaded["suite_id"]), label)
    eval_support.write_report(report, path)
    catalog.record_analysis_run(
        "eval-reuse-loop",
        "completed" if report["passed"] else "failed",
        {
            "suite_id": loaded["suite_id"],
            "label": label,
            "top_k": top_k,
            "limit_tasks": limit_tasks,
            "fastcontext_policy": fastcontext_policy,
            "max_evidence_rounds": max_evidence_rounds,
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


async def evaluate_reuse_loop_suite(
    suite: dict[str, Any],
    *,
    top_k: int,
    label: str | None = None,
    limit_tasks: int | None = None,
    fastcontext_policy: FastContextPolicy = "never",
    max_evidence_rounds: int = 0,
    force_assessment: bool = True,
    assessment_runtime: assessor.AssessmentRuntime | None = None,
) -> dict[str, Any]:
    if top_k < 1:
        raise ValueError("top_k must be at least 1.")
    if limit_tasks is not None and limit_tasks < 1:
        raise ValueError("limit_tasks must be at least 1.")
    if fastcontext_policy not in {"auto", "always", "never"}:
        raise ValueError("fastcontext_policy must be one of: auto, always, never.")
    if max_evidence_rounds < 0 or max_evidence_rounds > 2:
        raise ValueError("max_evidence_rounds must be between 0 and 2.")

    tasks = list(suite["tasks"])
    if limit_tasks is not None:
        tasks = tasks[:limit_tasks]
    task_reports = [
        await _evaluate_reuse_loop_task(
            task,
            top_k=top_k,
            fastcontext_policy=fastcontext_policy,
            max_evidence_rounds=max_evidence_rounds,
            force_assessment=force_assessment,
            assessment_runtime=assessment_runtime,
        )
        for task in tasks
    ]
    metrics = _reuse_loop_metrics(task_reports)
    return {
        "suite_id": suite["suite_id"],
        "description": suite.get("description", ""),
        "label": label,
        "top_k": top_k,
        "limit_tasks": limit_tasks,
        "fastcontext_policy": fastcontext_policy,
        "max_evidence_rounds": max_evidence_rounds,
        "timestamp": datetime.now(UTC).isoformat(),
        "passed": (
            int(metrics["reuse_loop_success_count"]) == int(metrics["task_count"])
            and int(metrics["no_match_downstream_work_count"]) == 0
            and int(metrics["assessment_error_count"]) == 0
            and int(metrics["bundle_error_count"]) == 0
            and int(metrics["bundle_quality_failure_count"]) == 0
        ),
        "metrics": metrics,
        "tasks": task_reports,
    }


def default_report_path(suite_id: str, label: str | None = None) -> Path:
    return eval_support.default_report_path("eval_runs", suite_id, label)


def reuse_loop_report_path(suite_id: str, label: str | None = None) -> Path:
    return eval_support.default_report_path("reuse_loop_reports", suite_id, label)


def _suite_path(suite: str) -> Path:
    return eval_support.suite_path(suite, SUITE_ALIASES, suite_label="eval")


def _validate_task(task: dict[str, Any], index: int) -> dict[str, Any]:
    task_id = str(task.get("id", "")).strip()
    task_text = str(task.get("task", "")).strip()
    capability = str(task.get("capability", "")).strip()
    if not task_id or not task_text or not capability:
        raise ValueError(f"Task {index} requires id, task, and capability.")
    expect_no_match = task.get("expect_no_match", False)
    if not isinstance(expect_no_match, bool):
        raise ValueError(f"Task {task_id} expect_no_match must be a boolean.")
    expected = _string_list(task.get("expected_repo_ids"))
    acceptable = _string_list(task.get("acceptable_repo_ids"))
    if not expect_no_match and not expected and not acceptable:
        raise ValueError(f"Task {task_id} requires expected or acceptable repos.")
    max_unresolved_local_imports = task.get("max_unresolved_local_imports")
    if max_unresolved_local_imports is not None and (
        isinstance(max_unresolved_local_imports, bool)
        or not isinstance(max_unresolved_local_imports, int)
        or max_unresolved_local_imports < 0
    ):
        raise ValueError(
            f"Task {task_id} max_unresolved_local_imports must be a non-negative integer."
        )
    return {
        "id": task_id,
        "task": task_text,
        "capability": capability,
        "expect_no_match": expect_no_match,
        "project_path": str(task.get("project_path") or "").strip(),
        "expected_commit_sha": str(task.get("expected_commit_sha") or "").strip(),
        "expected_source_paths_any": _string_list(task.get("expected_source_paths_any")),
        "expected_repo_ids": expected,
        "acceptable_repo_ids": acceptable,
        "avoid_repo_ids": _string_list(task.get("avoid_repo_ids")),
        "required_path_terms_any": _string_list(task.get("required_path_terms_any")),
        "required_dependencies_any": _string_list(task.get("required_dependencies_any")),
        "required_bundle_files_all": _string_list(task.get("required_bundle_files_all")),
        "allowed_bundle_files": _string_list(task.get("allowed_bundle_files")),
        "max_unresolved_local_imports": max_unresolved_local_imports,
        "max_rank_for_hit": int(task.get("max_rank_for_hit", 3)),
        "notes": str(task.get("notes", "")),
    }


def _resolved_project_path(project_path: str | None) -> str | None:
    if project_path is None or not project_path.strip():
        return None
    path = Path(project_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


def _target_profile_for_task(task: dict[str, Any]) -> TargetProfileV1 | None:
    project_path = _resolved_project_path(str(task.get("project_path") or ""))
    return build_target_profile(project_path) if project_path is not None else None


def _evaluate_task(task: dict[str, Any], top_k: int) -> dict[str, Any]:
    profile = _target_profile_for_task(task)
    results = catalog.search_assets(str(task["task"]), max_repos=top_k, target_profile=profile)
    expect_no_match = bool(task["expect_no_match"])
    candidates: list[dict[str, Any]] = []
    first_hit_rank: int | None = None
    blocked_label_match = False
    avoid_violations = 0
    max_rank = int(task["max_rank_for_hit"])

    for rank, candidate in enumerate(results, start=1):
        expected = candidate.repo_id in task["expected_repo_ids"]
        acceptable = candidate.repo_id in task["acceptable_repo_ids"]
        label_match = expected or acceptable
        capability_ok = candidate.capability == task["capability"]
        path_ok = _path_constraint_ok(candidate, task["required_path_terms_any"])
        dependency_ok = _dependency_constraint_ok(candidate, task["required_dependencies_any"])
        evidence_ok = bool(candidate.evidence_paths)
        commit_ok = _commit_constraint_ok(candidate, str(task["expected_commit_sha"]))
        source_path_ok = _source_path_constraint_ok(
            candidate,
            task["expected_source_paths_any"],
        )
        avoid_violation = (
            rank <= 3
            and candidate.repo_id in task["avoid_repo_ids"]
            and not _avoid_exception(candidate, str(task["capability"]))
        )
        if avoid_violation:
            avoid_violations += 1

        failure_reasons = _failure_reasons(
            expect_no_match=expect_no_match,
            label_match=label_match,
            capability_ok=capability_ok,
            path_ok=path_ok,
            dependency_ok=dependency_ok,
            evidence_ok=evidence_ok,
            commit_ok=commit_ok,
            source_path_ok=source_path_ok,
            avoid_violation=avoid_violation,
        )
        if label_match and rank <= max_rank and not all(
            (capability_ok, path_ok, dependency_ok, evidence_ok, commit_ok, source_path_ok)
        ):
            blocked_label_match = True
        if (
            first_hit_rank is None
            and not expect_no_match
            and label_match
            and capability_ok
            and rank <= max_rank
            and path_ok
            and dependency_ok
            and evidence_ok
            and commit_ok
            and source_path_ok
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
                "capability_match": capability_ok,
                "commit_sha": candidate.commit_sha,
                "label_match": label_match,
                "expected_match": expected,
                "acceptable_match": acceptable,
                "commit_sha_match": commit_ok,
                "source_path_match": source_path_ok,
                "entry_paths": candidate.entry_paths,
                "external_dependencies": candidate.external_dependencies,
                "evidence_paths": candidate.evidence_paths,
                "failure_reasons": failure_reasons,
            }
        )

    constraint_failures = 1 if first_hit_rank is None and blocked_label_match else 0
    no_match_correct = expect_no_match and not results
    retrieval_correct = no_match_correct or (not expect_no_match and first_hit_rank is not None)
    failure_buckets = _retrieval_failure_buckets(
        expect_no_match=expect_no_match,
        returned_count=len(results),
        hit_rank=first_hit_rank,
        avoid_violations=avoid_violations,
        constraint_failures=constraint_failures,
    )
    return {
        "id": task["id"],
        "task": task["task"],
        "capability": task["capability"],
        "expect_no_match": expect_no_match,
        "project_path": task["project_path"],
        "expected_commit_sha": task["expected_commit_sha"],
        "expected_source_paths_any": task["expected_source_paths_any"],
        "max_rank_for_hit": max_rank,
        "first_hit_rank": first_hit_rank,
        "top_1_hit": first_hit_rank == 1,
        "top_3_hit": first_hit_rank is not None and first_hit_rank <= 3,
        "top_5_hit": first_hit_rank is not None and first_hit_rank <= 5,
        "top_1_capability_correct": bool(results)
        and results[0].capability == task["capability"],
        "avoid_violations": avoid_violations,
        "constraint_failures": constraint_failures,
        "no_match_correct": no_match_correct,
        "unexpected_candidate_on_no_match": expect_no_match and bool(results),
        "retrieval_correct": retrieval_correct,
        "failure_buckets": failure_buckets,
        "candidates": candidates,
    }


async def _evaluate_reuse_loop_task(
    task: dict[str, Any],
    *,
    top_k: int,
    fastcontext_policy: FastContextPolicy,
    max_evidence_rounds: int,
    force_assessment: bool,
    assessment_runtime: assessor.AssessmentRuntime | None,
) -> dict[str, Any]:
    task_text = str(task["task"])
    profile = _target_profile_for_task(task)
    task_signature = catalog.task_signature(
        task_text,
        profile.fingerprint if profile is not None else "",
    )
    results = catalog.search_assets(task_text, max_repos=top_k, target_profile=profile)
    expect_no_match = bool(task["expect_no_match"])
    returned = [
        {
            "rank": rank,
            "candidate_id": candidate.candidate_id,
            "repo_id": candidate.repo_id,
            "capability": candidate.capability,
            "target_fit_score": float(candidate.target_fit_score),
        }
        for rank, candidate in enumerate(results, start=1)
    ]
    repo_hit_rank = _expected_or_acceptable_rank(
        returned,
        expected_repo_ids=task["expected_repo_ids"],
        acceptable_repo_ids=task["acceptable_repo_ids"],
    )
    hit_rank = _expected_candidate_rank(results, task)
    selected = results[0] if results else None
    selected_candidate_id = selected.candidate_id if selected is not None else None
    selected_repo_id = selected.repo_id if selected is not None else None
    selected_capability = selected.capability if selected is not None else None
    selected_is_expected_or_acceptable = _is_expected_or_acceptable(
        selected_repo_id,
        expected_repo_ids=task["expected_repo_ids"],
        acceptable_repo_ids=task["acceptable_repo_ids"],
    )
    selected_meets_expectations = selected is not None and _candidate_matches_task(selected, task)
    selected_capability_correct = selected_capability == task["capability"]
    no_match_correct = expect_no_match and not results
    retrieval_correct = no_match_correct or (not expect_no_match and hit_rank is not None)
    retrieval_failure_buckets = _retrieval_failure_buckets(
        expect_no_match=expect_no_match,
        returned_count=len(results),
        hit_rank=hit_rank,
        avoid_violations=0,
        constraint_failures=int(repo_hit_rank is not None and hit_rank is None),
    )

    assessment_fields: dict[str, Any] = {
        "assessment_final_verdict": None,
        "reuse_score": None,
        "confidence": None,
        "evidence_coverage": None,
        "notable_validation_notes": [],
        "assessment_id": None,
        "assessment_error": None,
    }
    bundle_fields = _empty_bundle_fields(task)
    assessment_attempted = False
    bundle_attempted = False
    if selected_candidate_id is not None and not expect_no_match:
        assessment_attempted = True
        assessment_fields = await _reuse_loop_assessment_fields(
            candidate_id=selected_candidate_id,
            task=task_text,
            fastcontext_policy=fastcontext_policy,
            max_evidence_rounds=max_evidence_rounds,
            force_assessment=force_assessment,
            assessment_runtime=assessment_runtime,
            project_path=str(task["project_path"]) or None,
        )
        if (
            assessment_fields["assessment_id"]
            and assessment_fields["assessment_final_verdict"]
            in {assessment_rules.VERDICT_SELECT, assessment_rules.VERDICT_INSPECT}
        ):
            bundle_attempted = True
            bundle_fields = _reuse_loop_bundle_fields(
                str(assessment_fields["assessment_id"]),
                task,
            )

    failure_buckets = list(retrieval_failure_buckets)
    if not expect_no_match and selected is not None and not selected_meets_expectations:
        failure_buckets.append("selected_candidate_failed_constraints")
    if assessment_fields["assessment_error"]:
        failure_buckets.append("assessment_error")
    assessment_accepted = assessment_fields["assessment_final_verdict"] in {
        assessment_rules.VERDICT_SELECT,
        assessment_rules.VERDICT_INSPECT,
    }
    if (
        not expect_no_match
        and assessment_fields["assessment_final_verdict"] is not None
        and not assessment_accepted
    ):
        failure_buckets.append("assessment_not_bundle_eligible")
    failure_buckets.extend(bundle_fields["bundle_failure_buckets"])
    bundle_created = bool(bundle_fields["bundle_path"])
    no_match_downstream_work = expect_no_match and (assessment_attempted or bundle_attempted)
    reuse_loop_success = (
        no_match_correct and not no_match_downstream_work
        if expect_no_match
        else (
            retrieval_correct
            and selected_meets_expectations
            and assessment_accepted
            and bundle_created
            and not bundle_fields["bundle_failure_buckets"]
        )
    )

    return {
        "id": task["id"],
        "task": task_text,
        "task_signature": task_signature,
        "target_profile_fingerprint": profile.fingerprint if profile is not None else "",
        "expect_no_match": expect_no_match,
        "project_path": task["project_path"],
        "expected_commit_sha": task["expected_commit_sha"],
        "expected_source_paths_any": task["expected_source_paths_any"],
        "required_bundle_files_all": task["required_bundle_files_all"],
        "allowed_bundle_files": task["allowed_bundle_files"],
        "max_unresolved_local_imports": task["max_unresolved_local_imports"],
        "expected_repo_ids": task["expected_repo_ids"],
        "acceptable_repo_ids": task["acceptable_repo_ids"],
        "returned_candidates": returned,
        "expected_or_acceptable_repo_in_top_k": repo_hit_rank is not None,
        "first_expected_or_acceptable_rank": repo_hit_rank,
        "matching_candidate_in_top_k": hit_rank is not None,
        "first_matching_candidate_rank": hit_rank,
        "no_match_correct": no_match_correct,
        "unexpected_candidate_on_no_match": expect_no_match and bool(results),
        "retrieval_correct": retrieval_correct,
        "selected_candidate_id": selected_candidate_id,
        "selected_repo_id": selected_repo_id,
        "selected_capability": selected_capability,
        "selected_capability_correct": selected_capability_correct,
        "selected_is_expected_or_acceptable": selected_is_expected_or_acceptable,
        "selected_meets_expectations": selected_meets_expectations,
        "assessment_attempted": assessment_attempted,
        "assessment_accepted": assessment_accepted,
        "bundle_attempted": bundle_attempted,
        "bundle_created": bundle_created,
        "no_match_downstream_work": no_match_downstream_work,
        "reuse_loop_success": reuse_loop_success,
        "failure_buckets": failure_buckets,
        **assessment_fields,
        **bundle_fields,
    }


async def _reuse_loop_assessment_fields(
    *,
    candidate_id: str,
    task: str,
    fastcontext_policy: FastContextPolicy,
    max_evidence_rounds: int,
    force_assessment: bool,
    assessment_runtime: assessor.AssessmentRuntime | None,
    project_path: str | None,
) -> dict[str, Any]:
    try:
        assessment = await assessor.assess_candidate(
            candidate_id=candidate_id,
            task=task,
            project_path=_resolved_project_path(project_path),
            fastcontext_policy=fastcontext_policy,
            max_evidence_rounds=max_evidence_rounds,
            force=force_assessment,
            runtime=assessment_runtime,
        )
    except Exception as exc:
        return {
            "assessment_final_verdict": None,
            "reuse_score": None,
            "confidence": None,
            "evidence_coverage": None,
            "notable_validation_notes": [],
            "assessment_id": None,
            "assessment_error": str(exc),
        }
    return {
        "assessment_final_verdict": str(assessment.final_verdict),
        "reuse_score": float(assessment.reuse_score),
        "confidence": float(assessment.confidence),
        "evidence_coverage": float(assessment.evidence_coverage),
        "notable_validation_notes": _notable_validation_notes(assessment.validation_notes),
        "assessment_id": str(assessment.assessment_id),
        "assessment_error": None,
    }


def _empty_bundle_fields(task: dict[str, Any]) -> dict[str, Any]:
    quality_expected = _bundle_quality_expected(task)
    failure_buckets = ["bundle_not_created"] if not task["expect_no_match"] else []
    return {
        "bundle_path": None,
        "bundle_commit_sha": None,
        "copied_files": [],
        "copied_file_count": 0,
        "missing_files": [],
        "missing_file_count": 0,
        "recommended_read_order": [],
        "file_hash_count": 0,
        "file_hash_failures": [],
        "bundle_file_hash_failure_count": 0,
        "bundle_total_bytes": 0,
        "missing_required_bundle_files": list(task["required_bundle_files_all"]),
        "unexpected_bundle_files": [],
        "unresolved_local_imports": [],
        "unresolved_local_import_count": 0,
        "bundle_required_file_recall": 0.0 if task["required_bundle_files_all"] else None,
        "bundle_allowed_file_precision": None,
        "bundle_commit_sha_match": None,
        "bundle_quality_expected": quality_expected,
        "bundle_quality_passed": False if quality_expected else None,
        "bundle_failure_buckets": failure_buckets,
        "bundle_error": None,
    }


def _reuse_loop_bundle_fields(
    assessment_id: str,
    task: dict[str, Any],
) -> dict[str, Any]:
    try:
        bundle = bundles.create_source_bundle(assessment_id)
    except Exception as exc:
        fields = _empty_bundle_fields(task)
        fields["bundle_failure_buckets"] = ["bundle_error"]
        fields["bundle_error"] = str(exc)
        return fields

    copied_files = _string_values(getattr(bundle, "files", []))
    missing_files = _string_values(getattr(bundle, "missing_files", []))
    recommended_read_order = _string_values(getattr(bundle, "recommended_read_order", []))
    unresolved_local_imports = _string_values(
        getattr(bundle, "unresolved_local_imports", getattr(bundle, "unresolved_imports", []))
    )
    required_files = list(task["required_bundle_files_all"])
    required_normalized = {_normalize_path(path) for path in required_files}
    copied_normalized = {_normalize_path(path) for path in copied_files}
    missing_required = [
        path for path in required_files if _normalize_path(path) not in copied_normalized
    ]
    allowed_files = list(task["allowed_bundle_files"])
    allowed_normalized = required_normalized | {_normalize_path(path) for path in allowed_files}
    unexpected_files = (
        [path for path in copied_files if _normalize_path(path) not in allowed_normalized]
        if allowed_files
        else []
    )
    required_recall = (
        round((len(required_files) - len(missing_required)) / len(required_files), 4)
        if required_files
        else None
    )
    allowed_precision = (
        round((len(copied_files) - len(unexpected_files)) / len(copied_files), 4)
        if allowed_files and copied_files
        else (1.0 if allowed_files else None)
    )
    expected_commit_sha = str(task["expected_commit_sha"])
    bundle_commit_sha = str(getattr(bundle, "commit_sha", ""))
    commit_match = bundle_commit_sha == expected_commit_sha if expected_commit_sha else None
    max_unresolved = task["max_unresolved_local_imports"]
    quality_expected = _bundle_quality_expected(task)
    bundle_path = str(getattr(bundle, "bundle_path", "")) or None
    failure_buckets: list[str] = []
    if bundle_path is None:
        failure_buckets.append("bundle_not_created")
    if missing_required:
        failure_buckets.append("bundle_missing_required_files")
    if unexpected_files:
        failure_buckets.append("bundle_unexpected_files")
    if missing_files:
        failure_buckets.append("bundle_missing_files")
    if max_unresolved is not None and len(unresolved_local_imports) > int(max_unresolved):
        failure_buckets.append("bundle_unresolved_local_imports")
    if commit_match is False:
        failure_buckets.append("bundle_commit_sha_mismatch")

    raw_hashes = getattr(bundle, "file_hashes", {})
    file_hash_count = len(raw_hashes) if isinstance(raw_hashes, dict) else 0
    hash_failures, actual_total_bytes = _bundle_hash_failures(
        bundle_path,
        copied_files,
        raw_hashes,
    )
    declared_total_bytes = int(getattr(bundle, "total_bytes", actual_total_bytes) or 0)
    if declared_total_bytes != actual_total_bytes:
        failure_buckets.append("bundle_total_bytes_mismatch")
    if hash_failures:
        failure_buckets.append("bundle_file_hash_failure")
    return {
        "bundle_path": bundle_path,
        "bundle_commit_sha": bundle_commit_sha or None,
        "copied_files": copied_files,
        "copied_file_count": len(copied_files),
        "missing_files": missing_files,
        "missing_file_count": len(missing_files),
        "recommended_read_order": recommended_read_order,
        "file_hash_count": file_hash_count,
        "file_hash_failures": hash_failures,
        "bundle_file_hash_failure_count": len(hash_failures),
        "bundle_total_bytes": actual_total_bytes,
        "missing_required_bundle_files": missing_required,
        "unexpected_bundle_files": unexpected_files,
        "unresolved_local_imports": unresolved_local_imports,
        "unresolved_local_import_count": len(unresolved_local_imports),
        "bundle_required_file_recall": required_recall,
        "bundle_allowed_file_precision": allowed_precision,
        "bundle_commit_sha_match": commit_match,
        "bundle_quality_expected": quality_expected,
        "bundle_quality_passed": not failure_buckets if quality_expected else None,
        "bundle_failure_buckets": failure_buckets,
        "bundle_error": None,
    }


def _metrics(task_reports: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(task_reports)
    positive_tasks = [task for task in task_reports if not task["expect_no_match"]]
    no_match_tasks = [task for task in task_reports if task["expect_no_match"]]
    positive_total = len(positive_tasks)
    no_match_total = len(no_match_tasks)
    top_1 = sum(1 for task in positive_tasks if task["top_1_hit"])
    top_3 = sum(1 for task in positive_tasks if task["top_3_hit"])
    top_5 = sum(1 for task in positive_tasks if task["top_5_hit"])
    reciprocal_sum = sum(
        1 / task["first_hit_rank"] for task in positive_tasks if task["first_hit_rank"]
    )
    positive_correct = sum(1 for task in positive_tasks if task["retrieval_correct"])
    capability_correct = sum(1 for task in positive_tasks if task["top_1_capability_correct"])
    target_profile_tasks = [task for task in positive_tasks if task["project_path"]]
    target_fit_top_1 = sum(1 for task in target_profile_tasks if task["top_1_hit"])
    correct_no_match = sum(1 for task in no_match_tasks if task["no_match_correct"])
    retrieval_correct = positive_correct + correct_no_match
    avoid_violations = sum(int(task["avoid_violations"]) for task in task_reports)
    constraint_failures = sum(int(task["constraint_failures"]) for task in task_reports)
    return {
        "task_count": total,
        "positive_task_count": positive_total,
        "no_match_task_count": no_match_total,
        "positive_retrieval_correct_count": positive_correct,
        "positive_retrieval_correct_rate": _rate(positive_correct, positive_total),
        "top_1_capability_correct_count": capability_correct,
        "top_1_capability_correct_rate": _rate(capability_correct, positive_total),
        "target_profile_task_count": len(target_profile_tasks),
        "target_fit_top_1_count": target_fit_top_1,
        "target_fit_top_1_rate": _rate(target_fit_top_1, len(target_profile_tasks)),
        "correct_no_match_count": correct_no_match,
        "correct_no_match_rate": _rate(correct_no_match, no_match_total),
        "unexpected_candidate_on_no_match_count": no_match_total - correct_no_match,
        "retrieval_correct_count": retrieval_correct,
        "retrieval_correct_rate": _rate(retrieval_correct, total),
        "top_1_hits": top_1,
        "top_3_hits": top_3,
        "top_5_hits": top_5,
        "top_1_hit_rate": _rate(top_1, positive_total),
        "top_3_hit_rate": _rate(top_3, positive_total),
        "top_5_hit_rate": _rate(top_5, positive_total),
        "mrr": round(reciprocal_sum / positive_total, 4) if positive_total else 0.0,
        "avoid_repo_violations": avoid_violations,
        "evidence_constraint_failures": constraint_failures,
    }


def _reuse_loop_metrics(task_reports: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(task_reports)
    positive_tasks = [task for task in task_reports if not task["expect_no_match"]]
    no_match_tasks = [task for task in task_reports if task["expect_no_match"]]
    positive_total = len(positive_tasks)
    no_match_total = len(no_match_tasks)
    top_k_hits = sum(
        1 for task in positive_tasks if task["expected_or_acceptable_repo_in_top_k"]
    )
    top_1_hits = sum(
        1 for task in positive_tasks if task["selected_is_expected_or_acceptable"]
    )
    positive_correct = sum(1 for task in positive_tasks if task["retrieval_correct"])
    capability_correct = sum(
        1 for task in positive_tasks if task["selected_capability_correct"]
    )
    target_profile_tasks = [task for task in positive_tasks if task["project_path"]]
    target_fit_top_1 = sum(
        1 for task in target_profile_tasks if task["selected_meets_expectations"]
    )
    correct_no_match = sum(1 for task in no_match_tasks if task["no_match_correct"])
    retrieval_correct = positive_correct + correct_no_match
    assessed = sum(1 for task in task_reports if task["assessment_final_verdict"] is not None)
    bundled = sum(1 for task in task_reports if task["bundle_path"])
    quality_tasks = [task for task in task_reports if task["bundle_quality_expected"]]
    required_file_count = sum(len(task["required_bundle_files_all"]) for task in quality_tasks)
    missing_required_count = sum(
        len(task["missing_required_bundle_files"]) for task in quality_tasks
    )
    allowed_tasks = [task for task in quality_tasks if task["allowed_bundle_files"]]
    allowed_copied_count = sum(int(task["copied_file_count"]) for task in allowed_tasks)
    unexpected_file_count = sum(len(task["unexpected_bundle_files"]) for task in quality_tasks)
    bundle_failure_buckets = [
        str(bucket)
        for task in task_reports
        for bucket in task["bundle_failure_buckets"]
    ]
    accepted_assessments = sum(
        1 for task in positive_tasks if task["assessment_accepted"]
    )
    positive_bundles = sum(1 for task in positive_tasks if task["bundle_created"])
    positive_loop_successes = sum(
        1 for task in positive_tasks if task["reuse_loop_success"]
    )
    no_match_loop_successes = sum(
        1 for task in no_match_tasks if task["reuse_loop_success"]
    )
    loop_successes = positive_loop_successes + no_match_loop_successes
    return {
        "task_count": total,
        "positive_task_count": positive_total,
        "no_match_task_count": no_match_total,
        "positive_retrieval_correct_count": positive_correct,
        "positive_retrieval_correct_rate": _rate(positive_correct, positive_total),
        "top_1_capability_correct_count": capability_correct,
        "top_1_capability_correct_rate": _rate(capability_correct, positive_total),
        "target_profile_task_count": len(target_profile_tasks),
        "target_fit_top_1_count": target_fit_top_1,
        "target_fit_top_1_rate": _rate(target_fit_top_1, len(target_profile_tasks)),
        "correct_no_match_count": correct_no_match,
        "correct_no_match_rate": _rate(correct_no_match, no_match_total),
        "unexpected_candidate_on_no_match_count": no_match_total - correct_no_match,
        "retrieval_correct_count": retrieval_correct,
        "retrieval_correct_rate": _rate(retrieval_correct, total),
        "top_1_expected_or_acceptable_hits": top_1_hits,
        "top_1_expected_or_acceptable_hit_rate": _rate(top_1_hits, positive_total),
        "top_k_expected_or_acceptable_hits": top_k_hits,
        "top_k_expected_or_acceptable_hit_rate": _rate(top_k_hits, positive_total),
        "assessed_count": assessed,
        "selected_verdict_counts": _count_values(
            [
                str(task["assessment_final_verdict"])
                for task in task_reports
                if task["assessment_final_verdict"] is not None
            ]
        ),
        "assessment_error_count": sum(1 for task in task_reports if task["assessment_error"]),
        "accepted_assessment_count": accepted_assessments,
        "accepted_assessment_rate": _rate(accepted_assessments, positive_total),
        "unacceptable_assessment_count": sum(
            1
            for task in positive_tasks
            if task["assessment_final_verdict"] is not None
            and not task["assessment_accepted"]
        ),
        "bundle_count": bundled,
        "positive_bundle_count": positive_bundles,
        "positive_bundle_rate": _rate(positive_bundles, positive_total),
        "bundle_error_count": sum(1 for task in task_reports if task["bundle_error"]),
        "positive_reuse_loop_success_count": positive_loop_successes,
        "positive_reuse_loop_success_rate": _rate(positive_loop_successes, positive_total),
        "no_match_reuse_loop_success_count": no_match_loop_successes,
        "no_match_reuse_loop_success_rate": _rate(no_match_loop_successes, no_match_total),
        "reuse_loop_success_count": loop_successes,
        "reuse_loop_success_rate": _rate(loop_successes, total),
        "no_match_downstream_work_count": sum(
            1 for task in no_match_tasks if task["no_match_downstream_work"]
        ),
        "copied_file_count": sum(int(task["copied_file_count"]) for task in task_reports),
        "missing_file_count": sum(int(task["missing_file_count"]) for task in task_reports),
        "file_hash_count": sum(int(task["file_hash_count"]) for task in task_reports),
        "bundle_file_hash_failure_count": sum(
            int(task["bundle_file_hash_failure_count"]) for task in task_reports
        ),
        "bundle_total_bytes": sum(int(task["bundle_total_bytes"]) for task in task_reports),
        "bundle_max_bytes": max(
            (int(task["bundle_total_bytes"]) for task in task_reports),
            default=0,
        ),
        "bundle_quality_task_count": len(quality_tasks),
        "bundle_quality_pass_count": sum(
            1 for task in quality_tasks if task["bundle_quality_passed"] is True
        ),
        "bundle_quality_failure_count": sum(
            1 for task in quality_tasks if task["bundle_quality_passed"] is False
        ),
        "bundle_required_file_recall": _rate(
            required_file_count - missing_required_count,
            required_file_count,
        ),
        "bundle_allowed_file_precision": _rate(
            allowed_copied_count - unexpected_file_count,
            allowed_copied_count,
        ),
        "bundle_missing_required_file_count": missing_required_count,
        "bundle_unexpected_file_count": unexpected_file_count,
        "bundle_unresolved_local_import_count": sum(
            int(task["unresolved_local_import_count"]) for task in task_reports
        ),
        "bundle_commit_sha_mismatch_count": sum(
            1 for task in task_reports if task["bundle_commit_sha_match"] is False
        ),
        "bundle_failure_bucket_counts": _count_values(bundle_failure_buckets),
    }


def _expected_or_acceptable_rank(
    returned: list[dict[str, Any]],
    *,
    expected_repo_ids: list[str],
    acceptable_repo_ids: list[str],
) -> int | None:
    for candidate in returned:
        if _is_expected_or_acceptable(
            str(candidate["repo_id"]),
            expected_repo_ids=expected_repo_ids,
            acceptable_repo_ids=acceptable_repo_ids,
        ):
            return int(candidate["rank"])
    return None


def _expected_candidate_rank(candidates: list[Any], task: dict[str, Any]) -> int | None:
    for rank, candidate in enumerate(candidates, start=1):
        if rank > int(task["max_rank_for_hit"]):
            break
        if _candidate_matches_task(candidate, task):
            return rank
    return None


def _candidate_matches_task(candidate: Any, task: dict[str, Any]) -> bool:
    return (
        str(getattr(candidate, "capability", "")) == str(task["capability"])
        and _is_expected_or_acceptable(
            str(candidate.repo_id),
            expected_repo_ids=task["expected_repo_ids"],
            acceptable_repo_ids=task["acceptable_repo_ids"],
        )
        and _path_constraint_ok(candidate, task["required_path_terms_any"])
        and _dependency_constraint_ok(candidate, task["required_dependencies_any"])
        and bool(candidate.evidence_paths)
        and _commit_constraint_ok(candidate, str(task["expected_commit_sha"]))
        and _source_path_constraint_ok(candidate, task["expected_source_paths_any"])
    )


def _is_expected_or_acceptable(
    repo_id: str | None,
    *,
    expected_repo_ids: list[str],
    acceptable_repo_ids: list[str],
) -> bool:
    if repo_id is None:
        return False
    return repo_id in (set(expected_repo_ids) | set(acceptable_repo_ids))


def _notable_validation_notes(notes: list[str]) -> list[str]:
    return [note for note in notes if note.strip()][:5]


def _count_values(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _passes_threshold(suite_id: str, metrics: dict[str, Any]) -> bool:
    no_match_total = int(metrics["no_match_task_count"])
    if int(metrics["correct_no_match_count"]) != no_match_total:
        return False
    positive_total = int(metrics["positive_task_count"])
    if positive_total == 0:
        return no_match_total > 0
    if suite_id == "ui-reuse":
        return (
            int(metrics["top_3_hits"]) >= UI_REUSE_PASSING_TOP3
            and int(metrics["top_1_hits"]) >= UI_REUSE_PASSING_TOP1
            and int(metrics["avoid_repo_violations"]) == 0
            and int(metrics["evidence_constraint_failures"]) == 0
        )
    return int(metrics["top_3_hits"]) >= max(1, positive_total // 2)


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


def _commit_constraint_ok(candidate: Any, expected_commit_sha: str) -> bool:
    return not expected_commit_sha or str(getattr(candidate, "commit_sha", "")) == expected_commit_sha


def _source_path_constraint_ok(candidate: Any, expected_paths: list[str]) -> bool:
    if not expected_paths:
        return True
    available = {
        _normalize_path(path)
        for path in [
            *_string_values(getattr(candidate, "entry_paths", [])),
            *[
                _evidence_file_path(path)
                for path in _string_values(getattr(candidate, "evidence_paths", []))
            ],
        ]
    }
    return any(_normalize_path(path) in available for path in expected_paths)


def _evidence_file_path(value: str) -> str:
    path, separator, suffix = value.rpartition(":")
    if separator and suffix.replace("-", "").isdigit():
        return path
    return value


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/")


def _string_values(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value]


def _bundle_hash_failures(
    bundle_path: str | None,
    copied_files: list[str],
    raw_hashes: Any,
) -> tuple[list[str], int]:
    if not copied_files:
        return [], 0
    if bundle_path is None:
        return [f"missing_bundle:{path}" for path in copied_files], 0
    hashes = raw_hashes if isinstance(raw_hashes, dict) else {}
    source_root = (Path(bundle_path) / "source").resolve()
    failures: list[str] = []
    total_bytes = 0
    for rel_path in copied_files:
        relative = Path(rel_path)
        if relative.is_absolute() or ".." in relative.parts:
            failures.append(f"unsafe_path:{rel_path}")
            continue
        path = (source_root / relative).resolve()
        if path != source_root and source_root not in path.parents:
            failures.append(f"unsafe_path:{rel_path}")
            continue
        if not path.is_file():
            failures.append(f"missing_file:{rel_path}")
            continue
        total_bytes += path.stat().st_size
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if str(hashes.get(rel_path, "")) != digest:
            failures.append(f"hash_mismatch:{rel_path}")
    return failures, total_bytes


def _bundle_quality_expected(task: dict[str, Any]) -> bool:
    return not bool(task["expect_no_match"]) and bool(
        task["required_bundle_files_all"]
        or task["allowed_bundle_files"]
        or task["expected_commit_sha"]
        or task["max_unresolved_local_imports"] is not None
    )


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
    expect_no_match: bool,
    label_match: bool,
    capability_ok: bool,
    path_ok: bool,
    dependency_ok: bool,
    evidence_ok: bool,
    commit_ok: bool,
    source_path_ok: bool,
    avoid_violation: bool,
) -> list[str]:
    if expect_no_match:
        return ["unexpected_candidate_on_no_match"]
    reasons: list[str] = []
    if not label_match:
        reasons.append("repo_not_labeled_relevant")
    if not capability_ok:
        reasons.append("capability_mismatch")
    if not path_ok:
        reasons.append("missing_required_path_term")
    if not dependency_ok:
        reasons.append("missing_required_dependency")
    if not evidence_ok:
        reasons.append("missing_evidence_paths")
    if not commit_ok:
        reasons.append("commit_sha_mismatch")
    if not source_path_ok:
        reasons.append("missing_expected_source_path")
    if avoid_violation:
        reasons.append("avoid_repo_in_top3")
    return reasons


def _retrieval_failure_buckets(
    *,
    expect_no_match: bool,
    returned_count: int,
    hit_rank: int | None,
    avoid_violations: int,
    constraint_failures: int,
) -> list[str]:
    buckets: list[str] = []
    if expect_no_match and returned_count:
        buckets.append("unexpected_candidate_on_no_match")
    elif not expect_no_match and hit_rank is None:
        buckets.append(
            "expected_candidate_failed_constraints"
            if constraint_failures
            else "expected_candidate_not_found"
        )
    if avoid_violations:
        buckets.append("avoid_repo_in_top3")
    return buckets


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Expected a list of strings.")
    return [str(item) for item in value]


def _safe_label(label: str | None) -> str:
    return eval_support.safe_label(label)
