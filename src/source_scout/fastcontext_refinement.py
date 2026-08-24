from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import catalog_assessments, catalog_assets, catalog_core, catalog_search, deepseek
from .fastcontext_constants import (
    ANALYZER_VERSION,
    DEFAULT_MAX_TURNS,
    MAX_FINAL_CITATIONS,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    TARGET_FINAL_CITATIONS,
)
from .fastcontext_tools import _safe_label
from .fastcontext_types import FastContextError, FastContextLoopResult

RunToolLoop = Callable[..., Awaitable[FastContextLoopResult]]
EnsureAvailable = Callable[..., Awaitable[None]]
RefineCandidate = Callable[..., Awaitable[dict[str, Any]]]

async def refine_candidate(
    candidate_id: str,
    task: str,
    max_turns: int = DEFAULT_MAX_TURNS,
    transport: httpx.AsyncBaseTransport | None = None,
    validate_model: bool = False,
    task_signature_override: str | None = None,
    *,
    run_tool_loop: RunToolLoop,
    ensure_available: EnsureAvailable,
) -> dict[str, Any]:
    if not task.strip():
        raise FastContextError("task is required.")

    asset = catalog_assets.get_asset_detail(candidate_id)
    if asset is None:
        raise FastContextError(f"Unknown candidate_id: {candidate_id}")

    config = deepseek.get_config()
    snapshot_root = Path(str(asset["snapshot_path"]))
    if not snapshot_root.exists() or not snapshot_root.is_dir():
        raise FastContextError(f"Snapshot path does not exist: {snapshot_root}")

    query_sig = catalog_assessments.task_signature(task)
    task_sig = task_signature_override or query_sig
    query = _build_query(asset, task)

    try:
        if validate_model:
            await ensure_available(config, transport=transport)
        loop_result = await run_tool_loop(
            root=snapshot_root,
            messages=_messages(asset, query),
            model_id=config.model_id,
            config=config,
            max_turns=max_turns,
            transport=transport,
            allow_observation_fallback=False,
        )
        return _store_refinement(
            asset=asset,
            candidate_id=candidate_id,
            task_signature=task_sig,
            query_signature=query_sig,
            model_id=config.model_id,
            query=query,
            evidence_paths=loop_result.evidence_paths,
            notes=loop_result.notes,
            trajectory=loop_result.trajectory,
        )
    except Exception as exc:
        catalog_core.record_analysis_run(
            "fastcontext-refine",
            "failed",
            {
                "candidate_id": candidate_id,
                "task_signature": task_sig,
                "query_signature": query_sig,
                "error": str(exc),
            },
            repo_id=str(asset["repo_id"]),
            snapshot_id=str(asset["snapshot_id"]),
            model_id=config.model_id,
            prompt_version=PROMPT_VERSION,
            analyzer_version=ANALYZER_VERSION,
        )
        raise


async def refine_suite(
    suite: str,
    top_k: int,
    label: str | None = None,
    output_path: Path | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    limit_tasks: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    refine_candidate_func: RefineCandidate,
) -> dict[str, Any]:
    if top_k < 1:
        raise FastContextError("top_k must be at least 1.")
    if limit_tasks is not None and limit_tasks < 1:
        raise FastContextError("limit_tasks must be at least 1.")

    from . import eval_runner

    loaded_suite = eval_runner.load_suite(suite)
    suite_id = str(loaded_suite["suite_id"])
    config = deepseek.get_config()
    tasks = list(loaded_suite["tasks"])
    if limit_tasks is not None:
        tasks = tasks[:limit_tasks]

    task_reports = []
    for task in tasks:
        task_reports.append(
            await _refine_suite_task(
                task=task,
                top_k=top_k,
                max_turns=max_turns,
                transport=transport,
                refine_candidate_func=refine_candidate_func,
            )
        )

    metrics = _batch_metrics(task_reports)
    report_path = output_path or default_refinement_report_path(suite_id, label)
    report = {
        "suite_id": suite_id,
        "description": loaded_suite.get("description", ""),
        "label": label,
        "top_k": top_k,
        "max_turns": max_turns,
        "model_id": config.model_id,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "timestamp": datetime.now(UTC).isoformat(),
        "metrics": metrics,
        "scoring_recommendation": _scoring_recommendation(metrics),
        "tasks": task_reports,
        "report_path": str(report_path),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    catalog_core.record_analysis_run(
        "fastcontext-batch-refine",
        "completed" if int(metrics["failed_refinements"]) == 0 else "completed_with_failures",
        {
            "suite_id": suite_id,
            "label": label,
            "top_k": top_k,
            "max_turns": max_turns,
            "metrics": metrics,
            "report_path": str(report_path),
        },
        model_id=config.model_id,
        prompt_version=PROMPT_VERSION,
        analyzer_version=ANALYZER_VERSION,
    )
    return report


def default_refinement_report_path(suite_id: str, label: str | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{_safe_label(label)}" if label else ""
    return catalog_core.ensure_home() / "fastcontext_runs" / suite_id / f"{timestamp}{suffix}.json"


async def _refine_suite_task(
    *,
    task: dict[str, Any],
    top_k: int,
    max_turns: int,
    transport: httpx.AsyncBaseTransport | None,
    refine_candidate_func: RefineCandidate,
) -> dict[str, Any]:
    candidates = catalog_search.search_assets(str(task["task"]), max_repos=top_k)
    candidate_reports = []
    for rank, candidate in enumerate(candidates, start=1):
        report = _deterministic_candidate_report(task, candidate, rank)
        try:
            refinement = await refine_candidate_func(
                candidate_id=candidate.candidate_id,
                task=str(task["task"]),
                max_turns=max_turns,
                transport=transport,
                validate_model=False,
            )
            refined_paths = [str(path) for path in refinement["evidence_paths"]]
            report.update(
                {
                    "refinement_status": "completed",
                    "refinement_id": refinement["refinement_id"],
                    "analysis_run_id": refinement["analysis_run_id"],
                    "refined_evidence_paths": refined_paths,
                    "refined_evidence_count": len(refined_paths),
                    "refined_path_constraint_ok": _path_terms_ok(
                        refined_paths,
                        task["required_path_terms_any"],
                    ),
                    "refined_notes": refinement.get("notes", []),
                }
            )
        except Exception as exc:
            report.update(
                {
                    "refinement_status": "failed",
                    "refinement_error": str(exc),
                    "refined_evidence_paths": [],
                    "refined_evidence_count": 0,
                    "refined_path_constraint_ok": False,
                    "refined_notes": [],
                }
            )
        candidate_reports.append(report)

    return {
        "id": task["id"],
        "task": task["task"],
        "capability": task["capability"],
        "task_signature": catalog_assessments.task_signature(str(task["task"])),
        "expected_repo_ids": task["expected_repo_ids"],
        "acceptable_repo_ids": task["acceptable_repo_ids"],
        "required_path_terms_any": task["required_path_terms_any"],
        "required_dependencies_any": task["required_dependencies_any"],
        "candidate_count": len(candidate_reports),
        "completed_refinements": sum(
            1 for candidate in candidate_reports if candidate["refinement_status"] == "completed"
        ),
        "failed_refinements": sum(
            1 for candidate in candidate_reports if candidate["refinement_status"] == "failed"
        ),
        "candidates": candidate_reports,
    }


def _deterministic_candidate_report(task: dict[str, Any], candidate: Any, rank: int) -> dict[str, Any]:
    label_match = (
        candidate.repo_id in task["expected_repo_ids"] or candidate.repo_id in task["acceptable_repo_ids"]
    )
    deterministic_paths = [str(path) for path in candidate.evidence_paths]
    return {
        "rank": rank,
        "candidate_id": candidate.candidate_id,
        "repo_id": candidate.repo_id,
        "capability": candidate.capability,
        "score": candidate.score,
        "label_match": label_match,
        "entry_paths": candidate.entry_paths,
        "external_dependencies": candidate.external_dependencies,
        "deterministic_evidence_paths": deterministic_paths,
        "deterministic_evidence_count": len(deterministic_paths),
        "deterministic_path_constraint_ok": _path_terms_ok(
            candidate.entry_paths + candidate.evidence_paths,
            task["required_path_terms_any"],
        ),
        "dependency_constraint_ok": _dependencies_ok(
            candidate.external_dependencies,
            task["required_dependencies_any"],
        ),
    }


def _batch_metrics(task_reports: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [candidate for task in task_reports for candidate in task["candidates"]]
    total_candidates = len(candidates)
    completed = sum(1 for candidate in candidates if candidate["refinement_status"] == "completed")
    failed = total_candidates - completed
    deterministic_evidence_total = sum(
        int(candidate["deterministic_evidence_count"]) for candidate in candidates
    )
    refined_evidence_total = sum(int(candidate["refined_evidence_count"]) for candidate in candidates)
    label_matches = [candidate for candidate in candidates if candidate["label_match"]]
    refined_label_matches = [
        candidate
        for candidate in label_matches
        if candidate["refinement_status"] == "completed" and int(candidate["refined_evidence_count"]) > 0
    ]
    refined_path_constraint_failures = sum(
        1
        for candidate in label_matches
        if candidate["refinement_status"] == "completed" and not candidate["refined_path_constraint_ok"]
    )
    top_1_refined = sum(
        1
        for task in task_reports
        if task["candidates"]
        and task["candidates"][0]["label_match"]
        and task["candidates"][0]["refinement_status"] == "completed"
        and int(task["candidates"][0]["refined_evidence_count"]) > 0
    )
    return {
        "task_count": len(task_reports),
        "candidate_count": total_candidates,
        "completed_refinements": completed,
        "failed_refinements": failed,
        "refinement_success_rate": round(completed / total_candidates, 4) if total_candidates else 0.0,
        "label_match_count": len(label_matches),
        "refined_label_match_count": len(refined_label_matches),
        "top_1_label_matches_with_refined_evidence": top_1_refined,
        "refined_path_constraint_failures": refined_path_constraint_failures,
        "deterministic_evidence_paths_total": deterministic_evidence_total,
        "refined_evidence_paths_total": refined_evidence_total,
        "evidence_compaction_ratio": round(
            refined_evidence_total / deterministic_evidence_total,
            4,
        )
        if deterministic_evidence_total
        else 0.0,
    }


def _scoring_recommendation(metrics: dict[str, Any]) -> dict[str, str]:
    if int(metrics["candidate_count"]) == 0:
        return {
            "status": "not_ready",
            "reason": "No candidates were refined.",
            "next_step": "Refresh deterministic evidence before using FastContext for scoring.",
        }
    if int(metrics["failed_refinements"]) > 0:
        return {
            "status": "not_ready",
            "reason": "Some FastContext refinements failed.",
            "next_step": "Fix prompt/runtime failures before wiring refined evidence into scoring.",
        }
    if float(metrics["refinement_success_rate"]) < 0.9:
        return {
            "status": "not_ready",
            "reason": "Refinement coverage is below 90%.",
            "next_step": "Run more batch refinements and inspect failure modes.",
        }
    if int(metrics["refined_path_constraint_failures"]) > 0:
        return {
            "status": "cautious",
            "reason": "Some labeled candidates produced refined evidence that missed required path terms.",
            "next_step": "Use refined evidence only as a tie-breaker until path constraints are stable.",
        }
    return {
        "status": "tie_breaker_ready",
        "reason": "FastContext refined all candidates with task-linked citations.",
        "next_step": (
            "Use refined evidence as a small tie-breaker or confidence boost for already-shortlisted "
            "candidates, not as a replacement for deterministic gates."
        ),
    }


def _path_terms_ok(paths: list[str], required_terms: list[str]) -> bool:
    if not required_terms:
        return True
    searchable = " ".join(paths).lower()
    return any(term.lower() in searchable for term in required_terms)


def _dependencies_ok(dependencies: list[str], required_dependencies: list[str]) -> bool:
    if not required_dependencies:
        return True
    available = {dependency.lower() for dependency in dependencies}
    return any(dependency.lower() in available for dependency in required_dependencies)


def _build_query(asset: dict[str, Any], task: str) -> str:
    return (
        f"{task.strip()}\n"
        f"Capability: {asset['capability']}\n"
        "Find the smallest set of source files and line ranges that help inspect the "
        "implementation details for this task. Do not decide, score, or prove whether "
        "the candidate is reusable."
    )


def _messages(asset: dict[str, Any], query: str) -> list[dict[str, str]]:
    context = {
        "repo_id": asset["repo_id"],
        "commit_sha": asset["commit_sha"],
        "capability": asset["capability"],
        "entry_paths": asset["entry_paths"],
        "dependency_paths": asset["dependency_paths"],
        "external_dependencies": asset["external_dependencies"],
        "deterministic_evidence_paths": asset["evidence_paths"],
    }
    return [
        {
            "role": "system",
            "content": (
                "You are FastContext, a read-only repository exploration subagent. "
                "Outcome: return the smallest source evidence set Codex should inspect for the task; "
                "do not decide, score, or prove candidate reuse. Use only Read, Glob, and Grep for "
                "evidence. Never execute code and never suggest edits. Prefer primary source files "
                "over docs, examples, generated output, build output, vendored code, and tests unless "
                "the task asks for those. Use relative paths like src/source_scout/server.py or exact "
                "paths under the workspace root; never shorten paths or use pseudo-absolute paths "
                "like /source_scout/src/source_scout/server.py. Cite only exact line ranges from "
                "successful tool observations. Use native tool calls whenever more evidence is needed. "
                "After enough evidence is observed, stop calling tools and return final_answer. "
                f"Return the smallest useful evidence set: 1-{MAX_FINAL_CITATIONS} citations, "
                f"ideally {TARGET_FINAL_CITATIONS}. Avoid background/supporting ranges unless "
                "they are necessary. When observed citation IDs are provided, prefer citation_ids "
                "over rewriting paths. "
                "When done, return only JSON in this shape: "
                '{"final_answer":{"citation_ids":["C1"],"notes":["short note"]}}. '
                "If citation IDs are unavailable, use evidence objects like "
                '{"path":"relative/file.ts","start_line":1,"end_line":20,"reason":"why this matters"}.'
            ),
        },
        {
            "role": "user",
            "content": (
                f"Context JSON:\n{json.dumps(context, sort_keys=True)}\n\nExploration query:\n{query}"
            ),
        },
    ]


def _store_refinement(
    *,
    asset: dict[str, Any],
    candidate_id: str,
    task_signature: str,
    query_signature: str,
    model_id: str,
    query: str,
    evidence_paths: list[str],
    notes: list[str],
    trajectory: list[dict[str, Any]],
) -> dict[str, Any]:
    refinement_id = catalog_assessments.store_evidence_refinement(
        asset_id=candidate_id,
        repo_id=str(asset["repo_id"]),
        snapshot_id=str(asset["snapshot_id"]),
        task_signature=query_signature,
        parent_task_signature=task_signature,
        capability=str(asset["capability"]),
        model_id=model_id,
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        query=query,
        evidence_paths=evidence_paths,
        notes=notes,
        trajectory=trajectory,
    )
    run_id = catalog_core.record_analysis_run(
        "fastcontext-refine",
        "completed",
        {
            "candidate_id": candidate_id,
            "task_signature": task_signature,
            "query_signature": query_signature,
            "schema_version": SCHEMA_VERSION,
            "refinement_id": refinement_id,
            "evidence_count": len(evidence_paths),
        },
        repo_id=str(asset["repo_id"]),
        snapshot_id=str(asset["snapshot_id"]),
        model_id=model_id,
        prompt_version=PROMPT_VERSION,
        analyzer_version=ANALYZER_VERSION,
    )
    return {
        "candidate_id": candidate_id,
        "task_signature": task_signature,
        "query_signature": query_signature,
        "repo_id": asset["repo_id"],
        "snapshot_id": asset["snapshot_id"],
        "capability": asset["capability"],
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "refinement_id": refinement_id,
        "analysis_run_id": run_id,
        "evidence_paths": evidence_paths,
        "notes": notes,
    }
