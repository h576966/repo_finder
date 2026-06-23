from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from . import assessment_rules, assessor, catalog, evidence_ledger, fastcontext, lmstudio

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "evals" / "golden"
SUITE_ALIASES = {
    "assessment-smoke": "assessment_smoke_v1.json",
}
ANALYZER_VERSION = "assessment-eval-v1"


def load_suite(suite: str) -> dict[str, Any]:
    path = _suite_path(suite)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read assessment eval suite '{suite}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Assessment eval suite '{suite}' is not valid JSON: {exc}") from exc
    return validate_suite(parsed)


def validate_suite(raw_suite: Mapping[str, Any]) -> dict[str, Any]:
    suite_id = str(raw_suite.get("suite_id", "")).strip()
    if not suite_id:
        raise ValueError("Assessment eval suite requires suite_id.")
    tasks = raw_suite.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Assessment eval suite requires a non-empty tasks list.")
    return {
        "suite_id": suite_id,
        "description": str(raw_suite.get("description", "")),
        "pass_threshold": _threshold(raw_suite.get("pass_threshold", {})),
        "tasks": [_validate_task(task, index) for index, task in enumerate(tasks, start=1)],
    }


async def run_assessment_eval(
    suite: str,
    label: str | None = None,
    output_path: Path | None = None,
    deterministic_only: bool = False,
) -> dict[str, Any]:
    loaded = load_suite(suite)
    report_path = output_path or default_report_path(str(loaded["suite_id"]), label)
    work_home = catalog.ensure_home() / "assessment_eval_work" / _run_id(str(loaded["suite_id"]), label)
    with _temporary_catalog_home(work_home):
        report = await evaluate_suite(
            loaded,
            label=label,
            deterministic_only=deterministic_only,
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report_path"] = str(report_path)
    catalog.record_analysis_run(
        "eval-assess",
        "completed" if report["passed"] else "failed",
        {
            "suite_id": loaded["suite_id"],
            "label": label,
            "deterministic_only": deterministic_only,
            "metrics": report["metrics"],
            "report_path": str(report_path),
        },
        analyzer_version=ANALYZER_VERSION,
    )
    return report


async def evaluate_suite(
    suite: Mapping[str, Any],
    *,
    label: str | None = None,
    deterministic_only: bool = False,
) -> dict[str, Any]:
    task_reports = []
    for task in suite["tasks"]:
        task_reports.append(
            await _evaluate_task(
                cast(dict[str, Any], task),
                deterministic_only=deterministic_only,
            )
        )
    metrics = _metrics(task_reports)
    passed = _passes_threshold(metrics, cast(dict[str, Any], suite["pass_threshold"]))
    return {
        "suite_id": suite["suite_id"],
        "description": suite.get("description", ""),
        "label": label,
        "deterministic_only": deterministic_only,
        "timestamp": datetime.now(UTC).isoformat(),
        "analyzer_version": ANALYZER_VERSION,
        "passed": passed,
        "pass_threshold": suite["pass_threshold"],
        "metrics": metrics,
        "tasks": task_reports,
        "failure_examples": _failure_examples(task_reports),
        "threshold_calibration_notes": _threshold_notes(metrics),
    }


def default_report_path(suite_id: str, label: str | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{_safe_label(label)}" if label else ""
    return catalog.ensure_home() / "assessment_eval_runs" / suite_id / f"{timestamp}{suffix}.json"


async def _evaluate_task(task: dict[str, Any], *, deterministic_only: bool) -> dict[str, Any]:
    candidate_id = _build_candidate(task)
    _store_prior_fastcontext_refinement(candidate_id, task)
    evidence_ids = _evidence_ids(candidate_id, str(task["task"]))
    gemma_sequence = _gemma_sequence(task, evidence_ids)
    fastcontext_config = task.get("fastcontext")
    if not isinstance(fastcontext_config, dict):
        fastcontext_config = {}

    original_chat_json = lmstudio.chat_json
    original_refine_candidate = fastcontext.refine_candidate
    model_calls = 0
    fastcontext_calls = 0

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal model_calls
        model_calls += 1
        if gemma_sequence:
            return gemma_sequence.pop(0)
        return _response("clear_reusable", evidence_ids)

    async def fake_refine_candidate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal fastcontext_calls
        fastcontext_calls += 1
        if str(fastcontext_config.get("status", "completed")) == "error":
            raise fastcontext.FastContextError(
                str(fastcontext_config.get("error", "mock FastContext failure"))
            )
        return _store_mock_refinement(
            candidate_id=str(kwargs.get("candidate_id", args[0] if args else candidate_id)),
            query=str(kwargs.get("task", "")),
            task_signature_override=kwargs.get("task_signature_override"),
            evidence_paths=_string_list(fastcontext_config.get("evidence_paths")),
            notes=_string_list(fastcontext_config.get("notes")),
        )

    cast(Any, lmstudio).chat_json = fake_chat_json
    cast(Any, fastcontext).refine_candidate = fake_refine_candidate
    error: str | None = None
    result: Any = None
    try:
        result = await assessor.assess_candidate(
            candidate_id,
            str(task["task"]),
            fastcontext_policy=_policy(task, deterministic_only),
            max_evidence_rounds=int(task["max_evidence_rounds"]),
            force=bool(task["force"]),
        )
        if bool(task["repeat_cached"]):
            result = await assessor.assess_candidate(
                candidate_id,
                str(task["task"]),
                fastcontext_policy=_policy(task, deterministic_only),
                max_evidence_rounds=int(task["max_evidence_rounds"]),
                force=False,
            )
    except Exception as exc:
        error = str(exc)
    finally:
        cast(Any, lmstudio).chat_json = original_chat_json
        cast(Any, fastcontext).refine_candidate = original_refine_candidate

    run_status = _latest_assessment_run_status()
    expected = list(task["expected_final_verdicts"])
    actual = str(getattr(result, "final_verdict", "")) if result else ""
    recommended = str(getattr(result, "recommended_verdict", "")) if result else ""
    validation_notes = list(getattr(result, "validation_notes", [])) if result else []
    stale_fastcontext_reused = _stale_fastcontext_reused(result, task)
    report = {
        "id": task["id"],
        "task": task["task"],
        "candidate_id": candidate_id,
        "assessment_id": getattr(result, "assessment_id", None) if result else None,
        "status": "failed" if error else "completed",
        "error": error,
        "expected_final_verdicts": expected,
        "recommended_verdict": recommended,
        "final_verdict": actual,
        "verdict_match": error is None and actual in expected and not stale_fastcontext_reused,
        "reuse_score": float(getattr(result, "reuse_score", 0.0)) if result else 0.0,
        "evidence_coverage": float(getattr(result, "evidence_coverage", 0.0)) if result else 0.0,
        "license_status": str(getattr(result, "license_status", "")) if result else "",
        "fastcontext_policy": (
            str(getattr(result, "fastcontext_policy", task["fastcontext_policy"]))
            if result
            else task["fastcontext_policy"]
        ),
        "fastcontext_status": str(getattr(result, "fastcontext_status", "")) if result else "",
        "model_call_count": model_calls,
        "fastcontext_call_count": fastcontext_calls,
        "analysis_run_status": run_status,
        "validation_notes": validation_notes,
        "stale_fastcontext_reused": stale_fastcontext_reused,
        "failure_reasons": _failure_reasons(
            error=error,
            verdict_match=error is None and actual in expected,
            stale_fastcontext_reused=stale_fastcontext_reused,
        ),
    }
    return report


def _build_candidate(task: Mapping[str, Any]) -> str:
    candidate = task["candidate"] if isinstance(task.get("candidate"), dict) else {}
    repo_id = str(candidate.get("repo_id") or f"eval/{task['id']}")
    owner, name = repo_id.split("/", 1)
    snapshot_root = catalog.ensure_home() / "fixtures" / _safe_label(str(task["id"]))
    snapshot_root.mkdir(parents=True, exist_ok=True)
    _write_files(snapshot_root, candidate)
    repo_metadata = {
        "owner": {"login": owner},
        "name": name,
        "full_name": repo_id,
        "html_url": f"https://github.com/{repo_id}",
        "private": False,
        "archived": False,
        "mirror_url": None,
        "fork": False,
        "is_template": False,
        "language": "TypeScript",
        "license": _license(candidate.get("license_spdx", "MIT")),
        "size": 10,
        "created_at": "2026-01-15T00:00:00Z",
        "pushed_at": "2026-06-20T12:00:00Z",
        "topics": ["nextjs"],
    }
    repo_key = catalog.upsert_repository(repo_metadata, "assessment-eval")
    snapshot_id = catalog.upsert_snapshot(
        repo_key,
        str(candidate.get("commit_sha", "evalsha")),
        "main",
        snapshot_root,
    )
    card = _repository_card(candidate)
    catalog.upsert_repository_card(snapshot_id, card)
    return catalog.upsert_asset(
        snapshot_id,
        repo_key,
        str(candidate.get("capability", "route-handlers")),
        {
            "entry_paths": _string_list(candidate.get("entry_paths")) or ["src/app/api/route.ts"],
            "dependency_paths": ["package.json"],
            "external_dependencies": _string_list(candidate.get("external_dependencies"))
            or ["next", "react", "zod"],
            "evidence_paths": _string_list(candidate.get("evidence_paths")),
            "synthesis": {"adaptation_notes": ["Assessment eval fixture."]},
            "reuse_score": float(candidate.get("reuse_score", 0.8)),
        },
    )


def _repository_card(candidate: Mapping[str, Any]) -> dict[str, Any]:
    capability = str(candidate.get("capability", "route-handlers"))
    return {
        "card_version": "repo-card-v1",
        "package_manifests": ["package.json"],
        "tree_summary": {"source_files": ["src/app/api/route.ts", "src/lib/schema.ts"]},
        "readme_excerpt": "Assessment eval fixture.",
        "stack_signals": {
            "has_next_dependency": True,
            "has_react_dependency": True,
            "has_typescript_files": True,
        },
        "deterministic_features": {"capabilities": [capability]},
        "gemma_profile": {
            "schema_version": "gemma-profile-v1",
            "repository_type": "reference_application",
            "capabilities": [{"name": capability, "confidence": 0.8}],
            "likely_usefulness": 0.8,
            "extractability": 0.8,
            "maintenance_quality": 0.8,
            "needs_fastcontext": False,
            "concerns": [],
        },
    }


def _write_files(snapshot_root: Path, candidate: Mapping[str, Any]) -> None:
    files = candidate.get("files")
    if not isinstance(files, dict):
        files = {
            "src/app/api/route.ts": "\n".join(
                [
                    "import { NextResponse } from 'next/server'",
                    "export async function GET() {",
                    "  return NextResponse.json({ ok: true })",
                    "}",
                ]
            ),
            "src/lib/schema.ts": "\n".join(
                [
                    "import { z } from 'zod'",
                    "export const RouteSchema = z.object({ ok: z.boolean() })",
                    "export type RouteSchemaInput = z.infer<typeof RouteSchema>",
                ]
            ),
            "package.json": json.dumps(
                {"dependencies": {"next": "15.0.0", "react": "19.0.0", "zod": "3.0.0"}},
                indent=2,
            ),
        }
    for raw_path, content in files.items():
        path = snapshot_root / str(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")


def _store_prior_fastcontext_refinement(candidate_id: str, task: Mapping[str, Any]) -> None:
    prior = task.get("prior_fastcontext_refinement")
    if not isinstance(prior, dict):
        return
    asset = catalog.get_asset_detail(candidate_id)
    if asset is None:
        return
    catalog.store_evidence_refinement(
        asset_id=candidate_id,
        repo_id=str(asset["repo_id"]),
        snapshot_id=str(asset["snapshot_id"]),
        task_signature=catalog.task_signature(str(prior.get("task", "other task"))),
        capability=str(asset["capability"]),
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        prompt_version=fastcontext.PROMPT_VERSION,
        schema_version=fastcontext.SCHEMA_VERSION,
        query=str(prior.get("query", "prior unrelated query")),
        evidence_paths=_string_list(prior.get("evidence_paths")),
        notes=["prior refinement for another task"],
        trajectory=[],
    )


def _store_mock_refinement(
    *,
    candidate_id: str,
    query: str,
    task_signature_override: object,
    evidence_paths: list[str],
    notes: list[str],
) -> dict[str, Any]:
    asset = catalog.get_asset_detail(candidate_id)
    if asset is None:
        raise fastcontext.FastContextError(f"Unknown candidate_id: {candidate_id}")
    refinement_id = catalog.store_evidence_refinement(
        asset_id=candidate_id,
        repo_id=str(asset["repo_id"]),
        snapshot_id=str(asset["snapshot_id"]),
        task_signature=catalog.task_signature(query),
        parent_task_signature=str(task_signature_override) if task_signature_override else None,
        capability=str(asset["capability"]),
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        prompt_version=fastcontext.PROMPT_VERSION,
        schema_version=fastcontext.SCHEMA_VERSION,
        query=query,
        evidence_paths=evidence_paths,
        notes=notes,
        trajectory=[],
    )
    run_id = catalog.record_analysis_run(
        "fastcontext-refine",
        "completed",
        {"candidate_id": candidate_id, "refinement_id": refinement_id},
        repo_id=str(asset["repo_id"]),
        snapshot_id=str(asset["snapshot_id"]),
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        prompt_version=fastcontext.PROMPT_VERSION,
        analyzer_version=fastcontext.ANALYZER_VERSION,
    )
    return {
        "refinement_id": refinement_id,
        "analysis_run_id": run_id,
        "evidence_paths": evidence_paths,
        "notes": notes,
    }


def _evidence_ids(candidate_id: str, task: str) -> list[str]:
    ledger = evidence_ledger.build_candidate_evidence_ledger(
        candidate_id,
        task_signature=catalog.task_signature(task),
    )
    return [str(item["evidence_id"]) for item in ledger.items]


def _gemma_sequence(task: Mapping[str, Any], evidence_ids: Sequence[str]) -> list[dict[str, Any]]:
    kinds = _string_list(task.get("gemma_sequence"))
    if not kinds:
        kinds = ["clear_reusable"]
    return [_response(kind, evidence_ids) for kind in kinds]


def _response(kind: str, evidence_ids: Sequence[str]) -> dict[str, Any]:
    first = evidence_ids[0] if evidence_ids else "E_missing"
    if kind == "weak_candidate":
        return _valid_response(
            first,
            recommended_verdict=assessment_rules.VERDICT_REJECT,
            model_confidence=0.9,
            functional_fit=0.1,
            extractability=0.2,
            dependency_fit=0.2,
            coupling_risk=0.8,
            maintenance_risk=0.5,
            requirement_status="unsatisfied",
        )
    if kind == "needs_fastcontext":
        response = _valid_response(first)
        response["missing_evidence"] = [
            {
                "question": "Find validation schema evidence for the route handler.",
                "preferred_retriever": "fastcontext",
                "priority": "high",
            }
        ]
        response["needs_fastcontext"] = True
        return response
    if kind == "unknown_evidence_id":
        response = _valid_response(first)
        response["requirement_assessments"][0]["evidence_ids"] = ["E_unknown"]
        return response
    return _valid_response(first)


def _valid_response(
    evidence_id: str,
    *,
    recommended_verdict: str = assessment_rules.VERDICT_SELECT,
    model_confidence: float = 0.95,
    functional_fit: float = 0.95,
    extractability: float = 0.9,
    dependency_fit: float = 0.9,
    coupling_risk: float = 0.05,
    maintenance_risk: float = 0.1,
    requirement_status: str = "satisfied",
) -> dict[str, Any]:
    return {
        "recommended_verdict": recommended_verdict,
        "model_confidence": model_confidence,
        "dimension_scores": {
            "functional_fit": functional_fit,
            "extractability": extractability,
            "dependency_fit": dependency_fit,
            "coupling_risk": coupling_risk,
            "maintenance_risk": maintenance_risk,
        },
        "requirement_assessments": [
            {
                "requirement": "Reusable implementation exists",
                "status": requirement_status,
                "evidence_ids": [evidence_id],
            }
        ],
        "fit_reasons": [{"text": "Evidence supports the assessment.", "evidence_ids": [evidence_id]}],
        "adaptation_plan": [
            {
                "step": "Copy and adapt the cited implementation.",
                "evidence_ids": [evidence_id],
            }
        ],
        "coupling_risks": [
            {
                "risk": "Imports may need adjustment.",
                "severity": "low",
                "evidence_ids": [evidence_id],
            }
        ],
        "blockers": [],
        "missing_evidence": [],
        "needs_fastcontext": False,
    }


def _metrics(task_reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(task_reports)
    completed = sum(1 for task in task_reports if task["status"] == "completed")
    verdict_matches = sum(1 for task in task_reports if task["verdict_match"])
    reuse_scores = [float(task["reuse_score"]) for task in task_reports if task["status"] == "completed"]
    evidence_coverages = [
        float(task["evidence_coverage"]) for task in task_reports if task["status"] == "completed"
    ]
    final_counts = {
        verdict: sum(1 for task in task_reports if task.get("final_verdict") == verdict)
        for verdict in (
            assessment_rules.VERDICT_SELECT,
            assessment_rules.VERDICT_INSPECT,
            assessment_rules.VERDICT_REJECT,
            assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE,
        )
    }
    return {
        "assessment_count": total,
        "completed_count": completed,
        "cache_hit_count": sum(1 for task in task_reports if task.get("analysis_run_status") == "cached"),
        "verdict_match_rate": round(verdict_matches / total, 4) if total else 0.0,
        "insufficient_evidence_rate": _rate(_insufficient_evidence_count(task_reports), total),
        "average_reuse_score": round(sum(reuse_scores) / len(reuse_scores), 4) if reuse_scores else 0.0,
        "average_evidence_coverage": round(sum(evidence_coverages) / len(evidence_coverages), 4)
        if evidence_coverages
        else 0.0,
        "unknown_evidence_id_repair_count": sum(
            1
            for task in task_reports
            if task.get("analysis_run_status") == "completed_repaired"
            or any("Unknown evidence_id" in str(note) for note in task.get("validation_notes", []))
        ),
        "fastcontext_attempted_count": sum(1 for task in task_reports if _fastcontext_attempted(task)),
        "fastcontext_completed_count": sum(
            1 for task in task_reports if task.get("fastcontext_status") == "completed"
        ),
        "fastcontext_error_count": sum(
            1 for task in task_reports if str(task.get("fastcontext_status", "")).startswith("failed")
        ),
        "final_select_count": final_counts[assessment_rules.VERDICT_SELECT],
        "final_inspect_count": final_counts[assessment_rules.VERDICT_INSPECT],
        "final_reject_count": final_counts[assessment_rules.VERDICT_REJECT],
        "final_insufficient_evidence_count": final_counts[assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE],
        "stale_fastcontext_reuse_count": sum(
            1 for task in task_reports if task.get("stale_fastcontext_reused")
        ),
    }


def _passes_threshold(metrics: Mapping[str, Any], threshold: Mapping[str, Any]) -> bool:
    return (
        float(metrics["verdict_match_rate"]) >= float(threshold["verdict_match_rate"])
        and int(metrics["stale_fastcontext_reuse_count"]) == 0
        and int(metrics["fastcontext_error_count"]) <= int(threshold["max_fastcontext_error_count"])
    )


def _insufficient_evidence_count(task_reports: Sequence[Mapping[str, Any]]) -> int:
    return sum(
        1
        for task in task_reports
        if task.get("final_verdict") == assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE
    )


def _threshold(raw: Any) -> dict[str, Any]:
    threshold = raw if isinstance(raw, dict) else {}
    return {
        "verdict_match_rate": float(threshold.get("verdict_match_rate", 1.0)),
        "max_fastcontext_error_count": int(threshold.get("max_fastcontext_error_count", 1)),
    }


def _failure_examples(task_reports: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    failures = []
    for task in task_reports:
        reasons = task.get("failure_reasons")
        if reasons:
            failures.append(
                {
                    "id": task["id"],
                    "expected_final_verdicts": task["expected_final_verdicts"],
                    "final_verdict": task.get("final_verdict"),
                    "failure_reasons": reasons,
                    "error": task.get("error"),
                }
            )
    return failures


def _threshold_notes(metrics: Mapping[str, Any]) -> list[str]:
    notes = [
        "This suite uses mocked Gemma/FastContext responses; use it for deterministic assessor calibration.",
    ]
    if int(metrics["fastcontext_error_count"]):
        notes.append(
            "FastContext error cases should still complete when deterministic evidence is available."
        )
    return notes


def _failure_reasons(
    *,
    error: str | None,
    verdict_match: bool,
    stale_fastcontext_reused: bool,
) -> list[str]:
    reasons: list[str] = []
    if error:
        reasons.append("assessment_error")
    if not verdict_match:
        reasons.append("verdict_mismatch")
    if stale_fastcontext_reused:
        reasons.append("stale_fastcontext_evidence_reused")
    return reasons


def _latest_assessment_run_status() -> str:
    row = catalog.get_connection().execute(
        """
        SELECT status
        FROM analysis_runs
        WHERE stage_name = 'reuse-assess'
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row[0]) if row else ""


def _stale_fastcontext_reused(result: Any, task: Mapping[str, Any]) -> bool:
    if not bool(task.get("expect_no_prior_fastcontext_reuse")) or result is None:
        return False
    for item in getattr(result, "evidence_ledger", []):
        origins = item.get("origins") if isinstance(item, dict) else None
        if isinstance(origins, list) and "fastcontext" in origins:
            return True
    return False


def _policy(
    task: Mapping[str, Any],
    deterministic_only: bool,
) -> Literal["auto", "always", "never"]:
    if deterministic_only:
        return "never"
    policy = str(task.get("fastcontext_policy", "never"))
    if policy in {"auto", "always", "never"}:
        return cast(Literal["auto", "always", "never"], policy)
    return "never"


def _fastcontext_attempted(task: Mapping[str, Any]) -> bool:
    status = str(task.get("fastcontext_status", ""))
    return (
        status in {"completed", "failed", "failed_with_existing"}
        or int(task.get("fastcontext_call_count", 0)) > 0
    )


def _validate_task(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"Assessment eval task {index} must be an object.")
    task_id = str(raw.get("id", "")).strip()
    task_text = str(raw.get("task", "")).strip()
    expected = _string_list(raw.get("expected_final_verdicts"))
    if not task_id or not task_text or not expected:
        raise ValueError(f"Assessment eval task {index} requires id, task, and expected_final_verdicts.")
    return {
        "id": task_id,
        "task": task_text,
        "expected_final_verdicts": expected,
        "candidate": raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {},
        "gemma_sequence": _string_list(raw.get("gemma_sequence")) or ["clear_reusable"],
        "fastcontext": raw.get("fastcontext") if isinstance(raw.get("fastcontext"), dict) else {},
        "fastcontext_policy": str(raw.get("fastcontext_policy", "never")),
        "max_evidence_rounds": int(raw.get("max_evidence_rounds", 1)),
        "force": bool(raw.get("force", True)),
        "repeat_cached": bool(raw.get("repeat_cached", False)),
        "prior_fastcontext_refinement": raw.get("prior_fastcontext_refinement"),
        "expect_no_prior_fastcontext_reuse": bool(raw.get("expect_no_prior_fastcontext_reuse", False)),
        "notes": str(raw.get("notes", "")),
    }


def _suite_path(suite: str) -> Path:
    candidate = Path(suite)
    if candidate.exists():
        return candidate
    filename = SUITE_ALIASES.get(suite, suite)
    path = GOLDEN_DIR / filename
    if path.exists():
        return path
    raise ValueError(f"Unknown assessment eval suite '{suite}'.")


def _license(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    spdx = str(value)
    return {"spdx_id": spdx} if spdx else None


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


def _run_id(suite_id: str, label: str | None) -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{_safe_label(suite_id)}_{_safe_label(label)}"


def _rate(value: int, total: int) -> float:
    return round(value / total, 4) if total else 0.0


@contextmanager
def _temporary_catalog_home(home: Path) -> Iterator[None]:
    old_home = os.environ.get("SOURCE_SCOUT_HOME")
    catalog.reset_connection()
    os.environ["SOURCE_SCOUT_HOME"] = str(home)
    catalog.reset_connection()
    try:
        yield
    finally:
        catalog.reset_connection()
        if old_home is None:
            os.environ.pop("SOURCE_SCOUT_HOME", None)
        else:
            os.environ["SOURCE_SCOUT_HOME"] = old_home
        catalog.reset_connection()
