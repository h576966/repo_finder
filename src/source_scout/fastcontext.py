import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import catalog, fastcontext_prompts, fastcontext_routing, lmstudio
from . import fastcontext_tools as fastcontext_tooling
from .fastcontext_constants import (
    ANALYZER_VERSION,
    DEFAULT_FASTCONTEXT_SEED,
    DEFAULT_MAX_TURNS,
    FASTCONTEXT_SEED_ENV,
    FASTCONTEXT_STRUCTURED_OUTPUT_ENV,
    FOCUSED_FINAL_CITATION_LINES,
    MAX_CITATION_LINES,
    MAX_FALLBACK_CITATIONS,
    MAX_FINAL_CITATION_CHOICES,
    MAX_FINAL_CITATIONS,
    MAX_FINAL_FILES,
    MAX_TOOL_CALLS_PER_TURN,
    PRIORITY_OBSERVATION_PATH_LIMIT,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    TARGET_FINAL_CITATIONS,
)
from .fastcontext_types import (
    EvidenceBudgetResult,
    FastContextCitation,
    FastContextError,
    FastContextLoopError,
    FastContextLoopResult,
    ObservationSupport,
    ParsedFastContextResponse,
)
from .models import LocalExploreResult

execute_tool = fastcontext_tooling.execute_tool
glob_paths = fastcontext_tooling.glob_paths
grep_paths = fastcontext_tooling.grep_paths
read_file = fastcontext_tooling.read_file
_canonical_tool_name = fastcontext_tooling._canonical_tool_name
_evidence_path_sort_key = fastcontext_tooling._evidence_path_sort_key
_fastcontext_tools = fastcontext_prompts.fastcontext_tool_schemas
_first_quoted = fastcontext_tooling._first_quoted
_has_glob_meta = fastcontext_tooling._has_glob_meta
_is_noisy_evidence_path = fastcontext_tooling._is_noisy_evidence_path
_is_primary_source_path = fastcontext_tooling._is_primary_source_path
_iter_files = fastcontext_tooling._iter_files
_match_sort_key = fastcontext_tooling._match_sort_key
_optional_int = fastcontext_tooling._optional_int
_parse_call_args = fastcontext_tooling._parse_call_args
_relative_path = fastcontext_tooling._relative_path
_resolve_under_root = fastcontext_tooling._resolve_under_root
_rg_skip_globs = fastcontext_tooling._rg_skip_globs
_safe_label = fastcontext_tooling._safe_label
_tool_args = fastcontext_tooling._tool_args
_tool_name = fastcontext_tooling._tool_name
_generic_local_task_file_bonus = fastcontext_routing._generic_local_task_file_bonus
_likely_source_files = fastcontext_routing._likely_source_files
_local_seed_context = fastcontext_routing._local_seed_context
_seed_path_priority = fastcontext_routing._seed_path_priority
_seed_priority_paths = fastcontext_routing._seed_priority_paths
_task_family_path_bonus = fastcontext_routing._task_family_path_bonus
_task_family_routing = fastcontext_routing._task_family_routing
_task_file_bonus = fastcontext_routing._task_file_bonus
_task_grep_pattern = fastcontext_routing._task_grep_pattern
_task_terms = fastcontext_routing._task_terms

__all__ = [
    "ANALYZER_VERSION",
    "DEFAULT_MAX_TURNS",
    "FastContextError",
    "FastContextLoopError",
    "MAX_FINAL_CITATIONS",
    "MAX_FINAL_FILES",
    "PROMPT_VERSION",
    "SCHEMA_VERSION",
    "execute_tool",
    "explore_local_project",
    "glob_paths",
    "grep_paths",
    "parse_fastcontext_response",
    "read_file",
    "refine_candidate",
    "refine_suite",
    "smoke_test",
]


async def ensure_fastcontext_available(
    config: lmstudio.LMStudioConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    active = config or lmstudio.get_config()
    status = await lmstudio.validate_models(active, transport=transport)
    if not status["fastcontext_available"]:
        raise lmstudio.LMStudioError(
            f"Configured FastContext model '{active.fastcontext_model}' is not available in LM Studio."
        )


async def smoke_test(
    config: lmstudio.LMStudioConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    active = config or lmstudio.get_config()
    await ensure_fastcontext_available(active, transport=transport)
    content = await _chat_fastcontext(
        model_id=active.fastcontext_model,
        messages=[
            {
                "role": "system",
                "content": "Return only valid JSON.",
            },
            {
                "role": "user",
                "content": 'Return exactly {"ok": true}.',
            },
        ],
        config=active,
        transport=transport,
        max_tokens=100,
        temperature=0.0,
    )
    return lmstudio.parse_json_content(content)


async def refine_candidate(
    candidate_id: str,
    task: str,
    max_turns: int = DEFAULT_MAX_TURNS,
    transport: httpx.AsyncBaseTransport | None = None,
    validate_model: bool = True,
    task_signature_override: str | None = None,
) -> dict[str, Any]:
    if not task.strip():
        raise FastContextError("task is required.")

    asset = catalog.get_asset_detail(candidate_id)
    if asset is None:
        raise FastContextError(f"Unknown candidate_id: {candidate_id}")

    config = lmstudio.get_config()
    snapshot_root = Path(str(asset["snapshot_path"]))
    if not snapshot_root.exists() or not snapshot_root.is_dir():
        raise FastContextError(f"Snapshot path does not exist: {snapshot_root}")

    query_sig = catalog.task_signature(task)
    task_sig = task_signature_override or query_sig
    query = _build_query(asset, task)

    try:
        if validate_model:
            await ensure_fastcontext_available(config, transport=transport)
        loop_result = await _run_tool_loop(
            root=snapshot_root,
            messages=_messages(asset, query),
            model_id=config.fastcontext_model,
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
            model_id=config.fastcontext_model,
            query=query,
            evidence_paths=loop_result.evidence_paths,
            notes=loop_result.notes,
            trajectory=loop_result.trajectory,
        )
    except Exception as exc:
        catalog.record_analysis_run(
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
            model_id=config.fastcontext_model,
            prompt_version=PROMPT_VERSION,
            analyzer_version=ANALYZER_VERSION,
        )
        raise


async def explore_local_project(
    task: str,
    project_path: str | Path = ".",
    max_turns: int = DEFAULT_MAX_TURNS,
    transport: httpx.AsyncBaseTransport | None = None,
    validate_model: bool = True,
    trace_path: str | Path | None = None,
) -> LocalExploreResult:
    if not task.strip():
        raise FastContextError("task is required.")

    root = Path(project_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FastContextError(f"project_path must be an existing directory: {project_path}")

    config = lmstudio.get_config()
    if validate_model:
        await ensure_fastcontext_available(config, transport=transport)

    seed_context = _local_seed_context(root, task)
    priority_paths = _seed_priority_paths(seed_context)
    try:
        loop_result = await _run_tool_loop(
            root=root,
            messages=_local_messages(root, task, seed_context=seed_context),
            model_id=config.fastcontext_model,
            config=config,
            max_turns=max_turns,
            transport=transport,
            allow_observation_fallback=True,
            priority_paths=priority_paths,
        )
    except FastContextLoopError as exc:
        if trace_path is not None:
            write_trace(trace_path, root=root, task=task, trajectory=exc.trajectory)
        raise
    if trace_path is not None:
        write_trace(trace_path, root=root, task=task, trajectory=loop_result.trajectory)
    return LocalExploreResult(
        task=task.strip(),
        project_path=str(root),
        model_id=config.fastcontext_model,
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        analyzer_version=ANALYZER_VERSION,
        status=loop_result.status,
        evidence_paths=loop_result.evidence_paths,
        notes=loop_result.notes,
        tool_trace=_tool_trace_summary(loop_result.trajectory),
    )


async def refine_suite(
    suite: str,
    top_k: int,
    label: str | None = None,
    output_path: Path | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    limit_tasks: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    if top_k < 1:
        raise FastContextError("top_k must be at least 1.")
    if limit_tasks is not None and limit_tasks < 1:
        raise FastContextError("limit_tasks must be at least 1.")

    from . import eval_runner

    loaded_suite = eval_runner.load_suite(suite)
    suite_id = str(loaded_suite["suite_id"])
    config = lmstudio.get_config()
    await ensure_fastcontext_available(config, transport=transport)

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
        "model_id": config.fastcontext_model,
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
    catalog.record_analysis_run(
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
        model_id=config.fastcontext_model,
        prompt_version=PROMPT_VERSION,
        analyzer_version=ANALYZER_VERSION,
    )
    return report


def default_refinement_report_path(suite_id: str, label: str | None = None) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{_safe_label(label)}" if label else ""
    return catalog.ensure_home() / "fastcontext_runs" / suite_id / f"{timestamp}{suffix}.json"


def write_trace(
    trace_path: str | Path,
    *,
    root: Path,
    task: str,
    trajectory: list[dict[str, Any]],
) -> Path:
    path = Path(trace_path).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task": task.strip(),
        "project_path": str(root),
        "model_id": lmstudio.get_config().fastcontext_model,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "analyzer_version": ANALYZER_VERSION,
        "timestamp": datetime.now(UTC).isoformat(),
        "trajectory": trajectory,
    }
    resolved.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return resolved


async def _run_tool_loop(
    *,
    root: Path,
    messages: list[dict[str, Any]],
    model_id: str,
    config: lmstudio.LMStudioConfig,
    max_turns: int,
    transport: httpx.AsyncBaseTransport | None,
    allow_observation_fallback: bool = False,
    priority_paths: list[str] | None = None,
) -> FastContextLoopResult:
    active_messages = list(messages)
    active_priority_paths = priority_paths or []
    trajectory: list[dict[str, Any]] = []
    observation_support = ObservationSupport(files=set(), ranges={})
    final_answer_only_next = False
    final_answer_retry_used = False
    budget_retry_used = False
    priority_retry_used = False
    for turn in range(1, max(1, max_turns) + 1):
        allow_tools = not final_answer_only_next
        completion = await _chat_fastcontext_completion(
            model_id=model_id,
            messages=active_messages,
            config=config,
            transport=transport,
            max_tokens=3000,
            temperature=0.0,
            allow_tools=allow_tools,
        )
        content = completion.content
        parsed = parse_fastcontext_response(content)
        tool_calls = _tool_calls_from_completion(completion) or parsed.tool_calls
        tool_mode_response = bool(completion.tool_calls)
        turn_record: dict[str, Any] = {
            "turn": turn,
            "model_response": content,
            "finish_reason": completion.finish_reason,
            "tools_enabled": allow_tools,
            "tool_calls": tool_calls,
            "final_citations": [citation.evidence_path() for citation in parsed.citations],
            "selected_citation_ids": parsed.citation_ids,
        }
        trajectory.append(turn_record)

        if parsed.citation_ids or parsed.citations:
            evidence_paths, validation_notes = _validated_response_evidence_paths(
                root,
                parsed,
                observation_support,
                priority_paths=active_priority_paths,
            )
            if validation_notes:
                turn_record["validation_notes"] = validation_notes
            if evidence_paths:
                budget_result = _apply_evidence_budget(
                    evidence_paths,
                    priority_paths=active_priority_paths,
                )
                _record_budget_result(turn_record, budget_result)
                if budget_result.over_budget and not budget_retry_used and turn < max_turns:
                    active_messages.extend(
                        _budget_feedback_messages(
                            content,
                            observation_support=observation_support,
                            budget_notes=budget_result.notes,
                            priority_paths=active_priority_paths,
                        )
                    )
                    budget_retry_used = True
                    final_answer_only_next = True
                    continue
                priority_notes = _priority_omission_notes(
                    budget_result.evidence_paths,
                    observation_support,
                    active_priority_paths,
                )
                if priority_notes:
                    turn_record.setdefault("validation_notes", []).extend(priority_notes)
                    if not priority_retry_used:
                        active_messages.extend(
                            _priority_feedback_messages(
                                content,
                                observation_support=observation_support,
                                priority_notes=priority_notes,
                                priority_paths=active_priority_paths,
                            )
                        )
                        priority_retry_used = True
                        final_answer_only_next = True
                        continue
                    if allow_observation_fallback:
                        priority_result = _completed_priority_observation_result(
                            observation_support,
                            trajectory,
                            note=(
                                "Accepted observed task-priority citations after final-answer "
                                "retry omitted the observed priority path."
                            ),
                            priority_paths=active_priority_paths,
                            turn_record=turn_record,
                            prefix_notes=[
                                *parsed.notes,
                                *validation_notes,
                                *priority_notes,
                            ],
                        )
                        if priority_result is not None:
                            return priority_result
                turn_record["final_citations"] = budget_result.evidence_paths
                return FastContextLoopResult(
                    status="completed",
                    evidence_paths=budget_result.evidence_paths,
                    notes=[*parsed.notes, *validation_notes, *budget_result.notes],
                    trajectory=trajectory,
                )

        if tool_calls and allow_tools:
            observations = [execute_tool(root, call) for call in tool_calls[:MAX_TOOL_CALLS_PER_TURN]]
            observation_support = _merge_observation_support(
                observation_support,
                _observation_support(observations),
            )
            turn_record["tool_observations"] = observations
            if tool_mode_response:
                active_messages.extend(_tool_observation_messages(completion, observations))
            else:
                active_messages.extend(_fallback_observation_messages(content, observations))
            finalization_reason = _finalization_reason(
                turn,
                max_turns,
                observation_support,
                priority_paths=active_priority_paths,
            )
            turn_record["finalization_reason"] = finalization_reason
            if finalization_reason:
                active_messages.append(
                    _final_answer_request_message(
                        observation_support,
                        finalization_reason=finalization_reason,
                        priority_paths=active_priority_paths,
                    )
                )
            elif tool_mode_response:
                active_messages.append(
                    _continue_exploration_message(
                        observation_support,
                        priority_paths=active_priority_paths,
                    )
                )
            final_answer_retry_used = False
            budget_retry_used = False
            priority_retry_used = False
            final_answer_only_next = finalization_reason is not None
            continue

        if parsed.citation_ids or parsed.citations:
            if not allow_tools and observation_support.ranges and not final_answer_retry_used:
                active_messages.extend(
                    _validation_feedback_messages(
                        content,
                        turn_record,
                        observation_support=observation_support,
                        final_answer_only=True,
                        priority_paths=active_priority_paths,
                    )
                )
                final_answer_retry_used = True
                final_answer_only_next = True
            elif not allow_tools and observation_support.ranges and allow_observation_fallback:
                priority_result = _completed_priority_observation_result(
                    observation_support,
                    trajectory,
                    note=(
                        "Accepted observed task-priority citations after final-answer retry did not validate."
                    ),
                    priority_paths=active_priority_paths,
                )
                if priority_result is not None:
                    return priority_result
                return _fallback_observation_result(
                    observation_support,
                    trajectory,
                    note=(
                        "FastContext final-answer retry did not validate; "
                        "showing supported tool observations only."
                    ),
                    priority_paths=active_priority_paths,
                )
            else:
                active_messages.extend(
                    _validation_feedback_messages(
                        content,
                        turn_record,
                        observation_support=observation_support,
                        priority_paths=active_priority_paths,
                    )
                )
                final_answer_only_next = False
            continue

        if tool_calls and not allow_tools:
            turn_record.setdefault("validation_notes", []).append(
                "Model returned tool calls during final-answer-only turn; reopening tools."
            )

        if not allow_tools and observation_support.ranges and not final_answer_retry_used:
            active_messages.extend(
                _final_response_feedback_messages(
                    content,
                    observation_support=observation_support,
                    final_answer_only=True,
                    priority_paths=active_priority_paths,
                )
            )
            final_answer_retry_used = True
            final_answer_only_next = True
        elif not allow_tools and observation_support.ranges and allow_observation_fallback:
            priority_result = _completed_priority_observation_result(
                observation_support,
                trajectory,
                note=(
                    "Accepted observed task-priority citations after final-answer "
                    "retry did not produce citations."
                ),
                priority_paths=active_priority_paths,
            )
            if priority_result is not None:
                return priority_result
            return _fallback_observation_result(
                observation_support,
                trajectory,
                note=(
                    "FastContext final-answer retry did not produce citations; "
                    "showing supported tool observations only."
                ),
                priority_paths=active_priority_paths,
            )
        else:
            active_messages.extend(
                _final_response_feedback_messages(
                    content,
                    observation_support=observation_support,
                    final_answer_only=False,
                    priority_paths=active_priority_paths,
                )
            )
            final_answer_only_next = False

    fallback_evidence = _evidence_from_observation_support(
        observation_support,
        priority_paths=active_priority_paths,
    ) or _evidence_from_trajectory(
        trajectory,
    )
    if fallback_evidence:
        fallback_budget = _apply_evidence_budget(
            fallback_evidence,
            max_citations=MAX_FALLBACK_CITATIONS,
            max_files=MAX_FALLBACK_CITATIONS,
            priority_paths=active_priority_paths,
        )
        trajectory.append(
            {
                "turn": max(1, max_turns) + 1,
                "model_response": "",
                "finish_reason": "max_turn_observation_fallback",
                "tools_enabled": False,
                "tool_calls": [],
                "tool_observations": [],
                "final_citations": fallback_budget.evidence_paths,
                "selected_citation_ids": [],
                "finalization_reason": "max_turn_observation_fallback",
                "citation_budget": _budget_trace(fallback_budget),
                "validation_notes": [
                    "FastContext reached max_turns without a final answer; using supported tool observations."
                ],
            }
        )
        if allow_observation_fallback:
            priority_result = _completed_priority_observation_result(
                observation_support,
                trajectory,
                note=(
                    "Accepted observed task-priority citations after max_turns without a valid final answer."
                ),
                priority_paths=active_priority_paths,
            )
            if priority_result is not None:
                return priority_result
            return FastContextLoopResult(
                status="fallback_observations",
                evidence_paths=fallback_budget.evidence_paths,
                notes=[
                    "FastContext reached max_turns without a valid final answer; "
                    "showing supported tool observations only.",
                    *fallback_budget.notes,
                ],
                trajectory=trajectory,
            )
    raise FastContextLoopError(
        "FastContext did not return usable evidence before max_turns.",
        trajectory,
    )


async def _chat_fastcontext_completion(
    *,
    model_id: str,
    messages: list[dict[str, Any]],
    config: lmstudio.LMStudioConfig,
    transport: httpx.AsyncBaseTransport | None,
    max_tokens: int,
    temperature: float,
    allow_tools: bool = True,
) -> lmstudio.LMStudioChatCompletion:
    if not allow_tools:
        return await lmstudio.chat_completion(
            model_id=model_id,
            messages=messages,
            config=config,
            transport=transport,
            max_tokens=max_tokens,
            temperature=temperature,
            seed=_fastcontext_seed(),
        )
    try:
        return await lmstudio.chat_completion(
            model_id=model_id,
            messages=messages,
            config=config,
            transport=transport,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=_fastcontext_tools(),
            tool_choice="auto",
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            seed=_fastcontext_seed(),
        )
    except lmstudio.LMStudioError:
        content = await _chat_fastcontext(
            model_id=model_id,
            messages=messages,
            config=config,
            transport=transport,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return lmstudio.LMStudioChatCompletion(
            content=content,
            tool_calls=[],
            finish_reason="fallback_content",
            message={"role": "assistant", "content": content},
            raw={},
        )


async def _chat_fastcontext(
    *,
    model_id: str,
    messages: list[dict[str, Any]],
    config: lmstudio.LMStudioConfig,
    transport: httpx.AsyncBaseTransport | None,
    max_tokens: int,
    temperature: float,
) -> str:
    response_format = _fastcontext_response_format() if _structured_output_enabled() else None
    try:
        return await lmstudio.chat_text(
            model_id=model_id,
            messages=messages,
            config=config,
            transport=transport,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
            seed=_fastcontext_seed(),
        )
    except lmstudio.LMStudioError:
        if response_format is None:
            raise
        return await lmstudio.chat_text(
            model_id=model_id,
            messages=messages,
            config=config,
            transport=transport,
            max_tokens=max_tokens,
            temperature=temperature,
            seed=_fastcontext_seed(),
        )


def _tool_calls_from_completion(
    completion: lmstudio.LMStudioChatCompletion,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for tool_call in completion.tool_calls:
        calls.append(
            {
                "id": tool_call.id,
                "tool": _canonical_tool_name(tool_call.name),
                "args": tool_call.arguments,
                "raw": tool_call.raw,
                "arguments_error": tool_call.arguments_error,
            }
        )
    return calls


def _tool_observation_messages(
    completion: lmstudio.LMStudioChatCompletion,
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": completion.content or None,
        "tool_calls": [call.raw for call in completion.tool_calls],
    }
    tool_messages = [
        {
            "role": "tool",
            "tool_call_id": str(observation.get("tool_call_id") or ""),
            "content": _tool_observation_content(observation),
        }
        for observation in observations
        if observation.get("tool_call_id")
    ]
    return [assistant_message, *tool_messages]


def _fallback_observation_messages(
    content: str,
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {"role": "assistant", "content": content},
        {
            "role": "user",
            "content": (
                "Tool observations JSON:\n"
                f"{json.dumps(observations, sort_keys=True)}\n\n"
                "Continue. Return either more tool_calls JSON or final_answer JSON."
            ),
        },
    ]


def _continue_exploration_message(
    observation_support: ObservationSupport,
    *,
    priority_paths: list[str] | None = None,
) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "Tool observations are available, but there is not enough strong citation support yet. "
            "Continue using Read, Glob, or Grep to gather focused file/line evidence. If you are "
            "already certain, you may return final_answer JSON with 1-3 citation_ids, ideally "
            f"{TARGET_FINAL_CITATIONS}, from the observed choices below.\n\n"
            f"{_priority_paths_text(priority_paths)}"
            f"{_observed_citation_choices_text(observation_support, priority_paths=priority_paths)}"
        ),
    }


def _final_answer_request_message(
    observation_support: ObservationSupport,
    *,
    feedback: str | None = None,
    finalization_reason: str | None = None,
    priority_paths: list[str] | None = None,
) -> dict[str, str]:
    choices_text = _observed_citation_choices_text(
        observation_support,
        priority_paths=priority_paths,
    )
    feedback_text = f"\n\nValidation feedback:\n{feedback}" if feedback else ""
    reason_text = f"\n\nFinalization reason: {finalization_reason}" if finalization_reason else ""
    return {
        "role": "user",
        "content": (
            "Tool observations are now available. Do not call tools on this turn. "
            "Return final_answer JSON only. Prefer citation_ids from the observed choices below, "
            'for example {"final_answer":{"citation_ids":["C1"],"notes":["why"]}}. '
            f"Choose 1-{MAX_FINAL_CITATIONS} citation IDs, ideally {TARGET_FINAL_CITATIONS}. "
            "Use the smallest set that directly answers the task. Do not include background, "
            "supporting, test, docs, or broad ranges unless they are necessary. "
            "Choose only from the observed citation choices below. "
            "Use exact relative paths and exact path:start-end line ranges. Do not cite directories, "
            "wildcards, globs, or shortened paths such as /source_scout/src, source_scout/src, "
            "evals/*.py, or src/**. Prefer src/source_scout choices over tests, docs, and evals "
            "unless the task explicitly asks for tests or documentation.\n\n"
            f"{_priority_paths_text(priority_paths)}"
            f"{choices_text}"
            f"{reason_text}"
            f"{feedback_text}"
        ),
    }


def _validation_feedback_messages(
    content: str,
    turn_record: dict[str, Any],
    *,
    observation_support: ObservationSupport,
    final_answer_only: bool = False,
    priority_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    feedback = (
        "Those citations did not validate against the project root or successful tool "
        "observations:\n"
        f"{json.dumps(turn_record.get('validation_notes', []), sort_keys=True)}"
    )
    if final_answer_only:
        return [
            {"role": "assistant", "content": content},
            _final_answer_request_message(
                observation_support,
                feedback=(
                    f"{feedback}\n\nRetry once without tools. Choose only exact observed "
                    "path:start-end choices from the list."
                ),
                priority_paths=priority_paths,
            ),
        ]
    return [
        {"role": "assistant", "content": content},
        {
            "role": "user",
            "content": (
                f"{feedback}\n\n"
                "Use Glob, Grep, or Read to find real relative paths and supported line ranges, "
                "then return final_answer JSON."
            ),
        },
    ]


def _final_response_feedback_messages(
    content: str,
    *,
    observation_support: ObservationSupport,
    final_answer_only: bool,
    priority_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    feedback = (
        "That final response did not contain usable exact citations. "
        "Glob-style or directory answers are not valid evidence."
    )
    if final_answer_only:
        return [
            {"role": "assistant", "content": content},
            _final_answer_request_message(
                observation_support,
                feedback=(f"{feedback} Retry once using only exact observed path:start-end choices."),
                priority_paths=priority_paths,
            ),
        ]
    return [
        {"role": "assistant", "content": content},
        {
            "role": "user",
            "content": (
                f"{feedback} Use Read, Glob, or Grep again only if more evidence is needed, "
                "then return final_answer JSON with exact path:start-end evidence paths."
            ),
        },
    ]


def _budget_feedback_messages(
    content: str,
    *,
    observation_support: ObservationSupport,
    budget_notes: list[str],
    priority_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    feedback = (
        "The final answer selected too many citations:\n"
        f"{json.dumps(budget_notes, sort_keys=True)}\n\n"
        f"Retry once without tools. Choose only the strongest 1-{MAX_FINAL_CITATIONS} "
        f"observed citation IDs, ideally {TARGET_FINAL_CITATIONS}. Prefer the smallest set "
        "that directly answers the task. Do not include background, test, docs, or supporting "
        "ranges unless they are necessary."
    )
    return [
        {"role": "assistant", "content": content},
        _final_answer_request_message(
            observation_support,
            feedback=feedback,
            finalization_reason="citation_budget_retry",
            priority_paths=priority_paths,
        ),
    ]


def _priority_feedback_messages(
    content: str,
    *,
    observation_support: ObservationSupport,
    priority_notes: list[str],
    priority_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    feedback = (
        "The final answer skipped an observed task-priority path:\n"
        f"{json.dumps(priority_notes, sort_keys=True)}\n\n"
        "Retry once without tools. Choose citation IDs from the observed priority path "
        "when that path answers the task. Do not choose lower-priority supporting ranges "
        "instead of the priority source."
    )
    return [
        {"role": "assistant", "content": content},
        _final_answer_request_message(
            observation_support,
            feedback=feedback,
            finalization_reason="priority_path_retry",
            priority_paths=priority_paths,
        ),
    ]


def _observed_citation_choices_text(
    support: ObservationSupport,
    *,
    priority_paths: list[str] | None = None,
) -> str:
    choice_items = _observed_citation_choice_items(support, priority_paths=priority_paths)
    if not choice_items:
        files = "\n".join(
            f"- {path}"
            for path in sorted(
                support.files,
                key=lambda path: _prioritized_path_sort_key(path, priority_paths),
            )[:MAX_FINAL_CITATION_CHOICES]
        )
        if files:
            return (
                "Observed files without line ranges:\n"
                f"{files}\n\n"
                "No valid line ranges have been observed yet. Exact line ranges are required."
            )
        return "Observed citation choices:\n- none"
    formatted = "\n".join(
        f"- {choice_id}: {citation.evidence_path()} ({_citation_choice_label(citation)})"
        for choice_id, citation in choice_items
    )
    return f"Observed citation choices:\n{formatted}"


def _observed_citation_choices(
    support: ObservationSupport,
    limit: int = MAX_FINAL_CITATION_CHOICES,
    priority_paths: list[str] | None = None,
) -> list[str]:
    return [
        citation.evidence_path()
        for _choice_id, citation in _observed_citation_choice_items(
            support,
            limit=limit,
            priority_paths=priority_paths,
        )
    ]


def _observed_citation_choice_items(
    support: ObservationSupport,
    limit: int = MAX_FINAL_CITATION_CHOICES,
    priority_paths: list[str] | None = None,
) -> list[tuple[str, FastContextCitation]]:
    choices: list[tuple[str, FastContextCitation]] = []
    for path in sorted(
        support.ranges,
        key=lambda path: _prioritized_path_sort_key(path, priority_paths),
    ):
        for start, end in sorted(_merge_ranges(support.ranges[path]), key=_range_sort_key):
            choice_id = f"C{len(choices) + 1}"
            choices.append((choice_id, FastContextCitation(path=path, start_line=start, end_line=end)))
            if len(choices) >= limit:
                return choices
    return choices


def _observed_citation_choice_map(
    support: ObservationSupport,
    *,
    priority_paths: list[str] | None = None,
) -> dict[str, FastContextCitation]:
    return {
        choice_id: citation
        for choice_id, citation in _observed_citation_choice_items(
            support,
            priority_paths=priority_paths,
        )
    }


def _priority_paths_text(priority_paths: list[str] | None = None) -> str:
    paths = [path for path in priority_paths or [] if path][:PRIORITY_OBSERVATION_PATH_LIMIT]
    if not paths:
        return ""
    formatted = "\n".join(f"- {path}" for path in paths)
    return (
        "Task-priority paths from deterministic seed context:\n"
        f"{formatted}\n"
        "Prefer observed citations from these paths when they answer the task.\n\n"
    )


def _observed_priority_paths(
    support: ObservationSupport,
    priority_paths: list[str] | None = None,
) -> list[str]:
    observed = {path.replace("\\", "/") for path in support.ranges}
    paths: list[str] = []
    for priority_path in (priority_paths or [])[:PRIORITY_OBSERVATION_PATH_LIMIT]:
        normalized = priority_path.replace("\\", "/")
        if normalized in observed:
            paths.append(normalized)
    return paths


def _required_observed_priority_path(
    support: ObservationSupport,
    priority_paths: list[str] | None = None,
) -> str:
    observed = _observed_priority_paths(support, priority_paths)
    return observed[0] if observed else ""


def _priority_omission_notes(
    evidence_paths: list[str],
    support: ObservationSupport,
    priority_paths: list[str] | None = None,
) -> list[str]:
    required_path = _required_observed_priority_path(support, priority_paths)
    if not required_path:
        return []
    selected_paths = _citation_files(evidence_paths)
    if required_path in selected_paths:
        return []
    return [f"Final answer omitted observed task-priority path: {required_path}"]


def _priority_observation_evidence_paths(
    support: ObservationSupport,
    priority_paths: list[str] | None = None,
) -> list[str]:
    required_path = _required_observed_priority_path(support, priority_paths)
    if not required_path:
        return []
    return [
        evidence_path
        for evidence_path in _observed_citation_choices(
            support,
            priority_paths=priority_paths,
        )
        if _citation_path(evidence_path) == required_path
    ][:MAX_FINAL_CITATIONS]


def _apply_evidence_budget(
    evidence_paths: list[str],
    *,
    max_citations: int = MAX_FINAL_CITATIONS,
    max_files: int = MAX_FINAL_FILES,
    priority_paths: list[str] | None = None,
) -> EvidenceBudgetResult:
    unique_paths = sorted(
        set(evidence_paths),
        key=lambda path: _evidence_citation_sort_key(path, priority_paths),
    )
    original_count = len(unique_paths)
    original_file_count = len(_citation_files(unique_paths))
    over_budget = original_count > max_citations or original_file_count > max_files
    accepted = unique_paths[:max_citations]
    accepted_file_count = len(_citation_files(accepted))
    truncated = accepted != unique_paths
    notes: list[str] = []
    if over_budget:
        notes.append(
            "Citation budget exceeded: "
            f"{original_count} citations across {original_file_count} files; "
            f"maximum is {max_citations} citations across {max_files} files."
        )
    if truncated:
        notes.append(
            f"Citation budget applied: accepted {len(accepted)} citations across {accepted_file_count} files."
        )
    return EvidenceBudgetResult(
        evidence_paths=accepted,
        notes=notes,
        over_budget=over_budget,
        truncated=truncated,
        original_count=original_count,
        accepted_count=len(accepted),
        original_file_count=original_file_count,
        accepted_file_count=accepted_file_count,
    )


def _record_budget_result(
    turn_record: dict[str, Any],
    budget_result: EvidenceBudgetResult,
) -> None:
    turn_record["citation_budget"] = _budget_trace(budget_result)
    if budget_result.notes:
        turn_record.setdefault("validation_notes", []).extend(budget_result.notes)


def _budget_trace(budget_result: EvidenceBudgetResult) -> dict[str, Any]:
    return {
        "original_count": budget_result.original_count,
        "accepted_count": budget_result.accepted_count,
        "original_file_count": budget_result.original_file_count,
        "accepted_file_count": budget_result.accepted_file_count,
        "over_budget": budget_result.over_budget,
        "truncated": budget_result.truncated,
    }


def _citation_files(evidence_paths: list[str]) -> set[str]:
    return {_citation_path(path) for path in evidence_paths if _citation_path(path)}


def _citation_path(evidence_path: str) -> str:
    match = re.match(r"(?P<path>.+?):\d+(?:-\d+)?$", evidence_path)
    if match:
        return match.group("path")
    return evidence_path


def _evidence_citation_sort_key(
    evidence_path: str,
    priority_paths: list[str] | None = None,
) -> tuple[int, int, str, int, int, str]:
    path = _citation_path(evidence_path)
    start_line = 0
    end_line = 0
    match = re.match(r".+?:(?P<start>\d+)(?:-\d+)?$", evidence_path)
    if match:
        start_line = int(match.group("start"))
        end_match = re.match(r".+?:\d+-(?P<end>\d+)$", evidence_path)
        end_line = int(end_match.group("end")) if end_match else start_line
    priority, normalized = _evidence_path_sort_key(path)
    priority_index = _priority_path_index(path, priority_paths)
    broad_penalty, _range_start, _range_end = _range_sort_key((start_line, end_line or start_line))
    return priority_index, priority, normalized, broad_penalty, start_line, evidence_path


def _prioritized_path_sort_key(
    path: str,
    priority_paths: list[str] | None = None,
) -> tuple[int, int, str]:
    priority, normalized = _evidence_path_sort_key(path)
    return _priority_path_index(path, priority_paths), priority, normalized


def _priority_path_index(path: str, priority_paths: list[str] | None = None) -> int:
    normalized = path.replace("\\", "/")
    for index, priority_path in enumerate(priority_paths or []):
        if normalized == priority_path.replace("\\", "/"):
            return index
    return 10_000


def _finalization_reason(
    turn: int,
    max_turns: int,
    support: ObservationSupport,
    *,
    priority_paths: list[str] | None = None,
) -> str | None:
    choices = _observed_citation_choice_items(support)
    if (
        priority_paths
        and not _has_priority_observation(support, priority_paths)
        and turn < max(1, max_turns - 1)
    ):
        return None
    primary_choices = [citation for _choice_id, citation in choices if _is_primary_source_path(citation.path)]
    focused_primary_count = sum(1 for citation in primary_choices if _is_focused_citation(citation))
    if len(primary_choices) >= 2:
        return "enough_primary_source_ranges"
    if turn >= max(1, max_turns - 1):
        return "last_available_turn"
    if len(choices) >= 3:
        if not primary_choices and turn < max(2, max_turns - 2):
            return None
        if len(primary_choices) == 1 and focused_primary_count == 0 and turn < max(2, max_turns - 2):
            return None
        return "enough_observed_ranges"
    return None


def _has_priority_observation(
    support: ObservationSupport,
    priority_paths: list[str] | None = None,
) -> bool:
    return bool(_observed_priority_paths(support, priority_paths))


def _fallback_observation_result(
    support: ObservationSupport,
    trajectory: list[dict[str, Any]],
    *,
    note: str,
    priority_paths: list[str] | None = None,
) -> FastContextLoopResult:
    budget_result = _apply_evidence_budget(
        _evidence_from_observation_support(support, priority_paths=priority_paths),
        max_citations=MAX_FALLBACK_CITATIONS,
        max_files=MAX_FALLBACK_CITATIONS,
        priority_paths=priority_paths,
    )
    evidence = budget_result.evidence_paths
    trajectory.append(
        {
            "turn": int(trajectory[-1].get("turn", 0)) + 1 if trajectory else 1,
            "model_response": "",
            "finish_reason": "final_answer_retry_observation_fallback",
            "tools_enabled": False,
            "tool_calls": [],
            "tool_observations": [],
            "final_citations": evidence,
            "selected_citation_ids": [],
            "finalization_reason": "supported_observation_fallback",
            "citation_budget": _budget_trace(budget_result),
            "validation_notes": [note, *budget_result.notes],
        }
    )
    return FastContextLoopResult(
        status="fallback_observations",
        evidence_paths=evidence,
        notes=[note, *budget_result.notes],
        trajectory=trajectory,
    )


def _completed_priority_observation_result(
    support: ObservationSupport,
    trajectory: list[dict[str, Any]],
    *,
    note: str,
    priority_paths: list[str] | None = None,
    turn_record: dict[str, Any] | None = None,
    prefix_notes: list[str] | None = None,
) -> FastContextLoopResult | None:
    priority_evidence = _priority_observation_evidence_paths(
        support,
        priority_paths,
    )
    if not priority_evidence:
        return None
    budget_result = _apply_evidence_budget(
        priority_evidence,
        priority_paths=priority_paths,
    )
    evidence = budget_result.evidence_paths
    if not evidence:
        return None
    if turn_record is not None:
        _record_budget_result(turn_record, budget_result)
        turn_record["final_citations"] = evidence
        turn_record.setdefault("validation_notes", []).append(note)
    else:
        trajectory.append(
            {
                "turn": int(trajectory[-1].get("turn", 0)) + 1 if trajectory else 1,
                "model_response": "",
                "finish_reason": "priority_observation_completion",
                "tools_enabled": False,
                "tool_calls": [],
                "tool_observations": [],
                "final_citations": evidence,
                "selected_citation_ids": [],
                "finalization_reason": "supported_priority_observation",
                "citation_budget": _budget_trace(budget_result),
                "validation_notes": [note, *budget_result.notes],
            }
        )
    return FastContextLoopResult(
        status="completed",
        evidence_paths=evidence,
        notes=[*(prefix_notes or []), note, *budget_result.notes],
        trajectory=trajectory,
    )


def _citation_choice_label(citation: FastContextCitation) -> str:
    if _is_primary_source_path(citation.path):
        if _is_focused_citation(citation):
            return "primary source, focused"
        return "primary source, broad"
    if _is_noisy_evidence_path(citation.path):
        return "supporting/noisy"
    if _is_focused_citation(citation):
        return "supporting, focused"
    return "supporting, broad"


def _is_focused_citation(citation: FastContextCitation) -> bool:
    span = _citation_line_span(citation)
    return span is not None and span <= FOCUSED_FINAL_CITATION_LINES


def _citation_line_span(citation: FastContextCitation) -> int | None:
    if citation.start_line is None:
        return None
    end_line = citation.end_line if citation.end_line is not None else citation.start_line
    if end_line < citation.start_line:
        return None
    return end_line - citation.start_line + 1


def _tool_observation_content(observation: dict[str, Any]) -> str:
    if observation.get("ok") and isinstance(observation.get("text"), str):
        return str(observation["text"])
    return json.dumps(observation, sort_keys=True)


def _structured_output_enabled() -> bool:
    raw = os.environ.get(FASTCONTEXT_STRUCTURED_OUTPUT_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _fastcontext_seed() -> int | None:
    raw = os.environ.get(FASTCONTEXT_SEED_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_FASTCONTEXT_SEED
    normalized = raw.strip().lower()
    if normalized in {"none", "off", "false", "random"}:
        return None
    try:
        return int(normalized)
    except ValueError:
        return DEFAULT_FASTCONTEXT_SEED


def _fastcontext_response_format() -> dict[str, Any]:
    citation_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["path"],
        "additionalProperties": True,
    }
    tool_call_schema = {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": ["READ", "GLOB", "GREP"]},
            "args": {"type": "object", "additionalProperties": True},
        },
        "required": ["tool", "args"],
        "additionalProperties": True,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "fastcontext_response",
            "schema": {
                "type": "object",
                "properties": {
                    "tool_calls": {
                        "type": "array",
                        "items": tool_call_schema,
                    },
                    "final_answer": {
                        "type": "object",
                        "properties": {
                            "citation_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "evidence": {
                                "type": "array",
                                "items": citation_schema,
                            },
                            "notes": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "additionalProperties": True,
                    },
                    "ok": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
        },
    }


def parse_fastcontext_response(content: str) -> ParsedFastContextResponse:
    try:
        parsed = lmstudio.parse_json_content(content)
    except lmstudio.LMStudioError:
        return ParsedFastContextResponse(
            tool_calls=_parse_function_style_tool_calls(content),
            citations=_parse_final_answer_citations(content),
            citation_ids=_parse_final_answer_citation_ids(content),
            notes=[],
        )

    return ParsedFastContextResponse(
        tool_calls=_extract_tool_calls(parsed),
        citations=_extract_citations(parsed),
        citation_ids=_extract_citation_ids(parsed),
        notes=_extract_notes(parsed),
    )


async def _refine_suite_task(
    *,
    task: dict[str, Any],
    top_k: int,
    max_turns: int,
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    candidates = catalog.search_assets(str(task["task"]), max_repos=top_k)
    candidate_reports = []
    for rank, candidate in enumerate(candidates, start=1):
        report = _deterministic_candidate_report(task, candidate, rank)
        try:
            refinement = await refine_candidate(
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
        "task_signature": catalog.task_signature(str(task["task"])),
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
                "Never execute code and never suggest edits. Use the provided Read, Glob, and Grep "
                "tools for evidence. Prefer primary source files over docs, examples, generated output, "
                "build output, vendored code, and tests unless the task asks for those. Do not shorten "
                "paths. On Windows, use relative paths like src/source_scout/server.py or exact paths "
                "under the workspace root; never use shortened pseudo-absolute paths like "
                "/source_scout/src/source_scout/server.py. Cite only files and exact line ranges that "
                "came from successful tool observations. "
                "If native tool calling is unavailable, request tools as JSON like "
                '{"tool_calls":[{"tool":"Grep","args":{"pattern":"symbol","glob":"**/*.ts"}}]}. '
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


def _local_messages(
    root: Path,
    task: str,
    *,
    seed_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    active_seed_context = seed_context or _local_seed_context(root, task)
    context = {
        "mode": "local-project-exploration",
        "project_path": str(root),
        "absolute_workspace_root": str(root),
        "task": task.strip(),
        "seed_context": active_seed_context,
        "rules": [
            "Read-only exploration only.",
            "Do not execute project code.",
            "Return file paths relative to project_path.",
            "Use the absolute workspace root only to understand scope; do not shorten paths.",
            "Use relative tool paths like src/source_scout/server.py, not shortened pseudo-absolute paths.",
            "Treat seed_context.likely_source_files as ordered; inspect the first relevant "
            "entries before broad search.",
            "Use seed_context.priority_file_matches as starting line anchors for Read offsets "
            "when they are present.",
            "Prefer primary source tree files over docs, generated, build, vendor, sample, and fixture code "
            "unless seed_context.task_type says the task is about tests, evals, fixtures, docs, CLI, or MCP.",
            "If the task names a file path, inspect that exact file first.",
            "Only cite files and line ranges that appeared in successful tool observations.",
            "After enough evidence is observed, stop calling tools and return final_answer.",
            "Return compact, relevant line ranges for Codex to inspect before editing.",
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "You are FastContext, a read-only local repository exploration subagent. "
                "Use the provided Read, Glob, and Grep tools. The user context includes the absolute "
                "workspace root; do not shorten or invent paths. Treat seed_context.likely_source_files "
                "as ordered and inspect the first relevant entries before broad search. Start broad only "
                "when the ordered hints are insufficient, then narrow down. Use "
                "seed_context.priority_file_matches as line anchors for Read offsets before "
                "reading module headers. Prefer primary "
                "source tree files over docs, generated output, build output, "
                "vendored code, samples, and fixtures unless seed_context.task_type says the task asks "
                "for tests, evals, fixtures, docs, CLI, or MCP. If the task names "
                "a file, inspect that exact file first. On Windows, use relative paths like "
                "src/source_scout/server.py or exact paths under the workspace root; never use shortened "
                "pseudo-absolute paths like /source_scout/src/source_scout/server.py. Cite only files and "
                "exact line ranges that appeared in successful tool observations. If native tool calling "
                "is unavailable, request tools "
                "as JSON like "
                '{"tool_calls":[{"tool":"Grep","args":{"pattern":"symbol","glob":"**/*.ts"}}]}. '
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
                f"Context JSON:\n{json.dumps(context, sort_keys=True)}\n\n"
                f"Explore this local project for task:\n{task.strip()}"
            ),
        },
    ]


def _tool_trace_summary(trajectory: list[dict[str, Any]]) -> list[dict[str, object]]:
    summary: list[dict[str, object]] = []
    for turn in trajectory:
        tool_calls = turn.get("tool_calls", [])
        observations = turn.get("tool_observations", [])
        final_citations = turn.get("final_citations", [])
        selected_citation_ids = turn.get("selected_citation_ids", [])
        validation_notes = turn.get("validation_notes", [])
        citation_budget = turn.get("citation_budget", {})
        summary.append(
            {
                "turn": int(turn.get("turn", 0)),
                "tools_enabled": bool(turn.get("tools_enabled", False)),
                "tool_calls": [
                    _canonical_tool_name(_tool_name(call)) for call in tool_calls if isinstance(call, dict)
                ]
                if isinstance(tool_calls, list)
                else [],
                "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
                "observation_count": len(observations) if isinstance(observations, list) else 0,
                "final_citations": final_citations if isinstance(final_citations, list) else [],
                "selected_citation_ids": selected_citation_ids
                if isinstance(selected_citation_ids, list)
                else [],
                "finalization_reason": str(turn.get("finalization_reason") or ""),
                "citation_budget": citation_budget if isinstance(citation_budget, dict) else {},
                "validation_notes": validation_notes if isinstance(validation_notes, list) else [],
            }
        )
    return summary


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
    refinement_id = catalog.store_evidence_refinement(
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
    run_id = catalog.record_analysis_run(
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


def _validated_evidence_paths(
    root: Path,
    citations: list[FastContextCitation],
    observation_support: ObservationSupport | None = None,
) -> tuple[list[str], list[str]]:
    evidence_paths: list[str] = []
    notes: list[str] = []
    for citation in citations:
        shape_note = _citation_shape_note(citation)
        if shape_note is not None:
            notes.append(shape_note)
            continue
        try:
            path, safe_rel = _resolve_under_root(root, citation.path)
        except FastContextError as exc:
            notes.append(f"Skipped invalid citation '{citation.evidence_path()}': {exc}")
            continue
        if not path.is_file():
            notes.append(f"Skipped missing citation file: {safe_rel}")
            continue
        normalized = FastContextCitation(
            path=safe_rel,
            start_line=citation.start_line,
            end_line=citation.end_line,
            reason=citation.reason,
        )
        line_note = _line_validation_note(path, safe_rel, normalized)
        if line_note is not None:
            notes.append(line_note)
            continue
        if observation_support is not None and observation_support.files:
            support_note = _support_validation_note(safe_rel, normalized, observation_support)
            if support_note is not None:
                notes.append(support_note)
                continue
        evidence_paths.append(normalized.evidence_path())
    return sorted(set(evidence_paths)), notes


def _validated_response_evidence_paths(
    root: Path,
    parsed: ParsedFastContextResponse,
    observation_support: ObservationSupport,
    *,
    priority_paths: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    if parsed.citation_ids:
        id_paths, id_notes = _validated_citation_id_paths(
            root,
            parsed.citation_ids,
            observation_support,
            priority_paths=priority_paths,
        )
        notes.extend(id_notes)
        if id_paths:
            return id_paths, notes
    if parsed.citations:
        raw_paths, raw_notes = _validated_evidence_paths(
            root,
            parsed.citations,
            observation_support=observation_support,
        )
        notes.extend(raw_notes)
        return raw_paths, notes
    return [], notes


def _validated_citation_id_paths(
    root: Path,
    citation_ids: list[str],
    observation_support: ObservationSupport,
    *,
    priority_paths: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    id_map = _observed_citation_choice_map(
        observation_support,
        priority_paths=priority_paths,
    )
    citations: list[FastContextCitation] = []
    notes: list[str] = []
    for citation_id in citation_ids:
        normalized = citation_id.strip().upper()
        citation = id_map.get(normalized)
        if citation is None:
            notes.append(f"Skipped unknown citation_id: {citation_id}")
            continue
        citations.append(citation)
    evidence_paths, validation_notes = _validated_evidence_paths(
        root,
        citations,
        observation_support=observation_support,
    )
    notes.extend(validation_notes)
    return evidence_paths, notes


def _citation_shape_note(citation: FastContextCitation) -> str | None:
    path = citation.path.strip()
    if _has_glob_meta(path):
        return f"Skipped wildcard or glob citation: {citation.evidence_path()}"
    if citation.start_line is None or citation.end_line is None:
        return f"Skipped citation without exact line range: {citation.evidence_path()}"
    if path.endswith(("/", "\\")):
        return f"Skipped directory citation: {citation.evidence_path()}"
    return None


def _line_validation_note(path: Path, safe_rel: str, citation: FastContextCitation) -> str | None:
    if citation.start_line is None:
        return None
    start_line = citation.start_line
    end_line = citation.end_line if citation.end_line is not None else start_line
    if start_line <= 0 or end_line <= 0:
        return f"Skipped citation with non-positive line range: {safe_rel}:{start_line}-{end_line}"
    if end_line < start_line:
        return f"Skipped citation with reversed line range: {safe_rel}:{start_line}-{end_line}"
    if end_line - start_line + 1 > MAX_CITATION_LINES:
        return f"Skipped overly broad citation: {safe_rel}:{start_line}-{end_line}"
    line_count = _line_count(path)
    if start_line > line_count or end_line > line_count:
        return (
            f"Skipped citation beyond EOF: {safe_rel}:{start_line}-{end_line} (file has {line_count} lines)"
        )
    return None


def _support_validation_note(
    safe_rel: str,
    citation: FastContextCitation,
    support: ObservationSupport,
) -> str | None:
    if safe_rel not in support.files:
        return f"Skipped unsupported citation file from final answer: {citation.evidence_path()}"
    if citation.start_line is None:
        return None
    ranges = support.ranges.get(safe_rel, [])
    if not ranges:
        return f"Skipped citation without observed line support: {citation.evidence_path()}"
    start_line = citation.start_line
    end_line = citation.end_line if citation.end_line is not None else start_line
    if any(start <= end_line and start_line <= end for start, end in ranges):
        return None
    return f"Skipped citation outside observed line ranges: {citation.evidence_path()}"


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError as exc:
        raise FastContextError(f"Could not read citation file: {path}") from exc


def _observation_support(observations: list[dict[str, Any]]) -> ObservationSupport:
    files: set[str] = set()
    ranges: dict[str, list[tuple[int, int]]] = {}
    for observation in observations:
        if not observation.get("ok"):
            continue
        result = observation.get("result")
        if not isinstance(result, dict):
            continue
        tool = str(observation.get("tool", ""))
        if tool == "Read":
            path = result.get("path")
            if isinstance(path, str):
                files.add(path)
                start = _optional_int(result.get("start_line"))
                end = _optional_int(result.get("end_line"))
                if start is not None and end is not None and end >= start:
                    ranges.setdefault(path, []).append((start, end))
            continue
        matches = result.get("matches")
        if not isinstance(matches, list):
            continue
        for match in matches:
            if isinstance(match, str):
                files.add(match)
                continue
            if not isinstance(match, dict):
                continue
            path = match.get("path")
            if not isinstance(path, str):
                continue
            files.add(path)
            start = _optional_int(match.get("start_line") or match.get("line"))
            end = _optional_int(match.get("end_line") or match.get("line"))
            if start is not None and end is not None and end >= start:
                ranges.setdefault(path, []).append((start, end))
    return ObservationSupport(files=files, ranges=ranges)


def _merge_observation_support(
    current: ObservationSupport,
    incoming: ObservationSupport,
) -> ObservationSupport:
    files = set(current.files)
    files.update(incoming.files)
    ranges = {path: list(path_ranges) for path, path_ranges in current.ranges.items()}
    for path, path_ranges in incoming.ranges.items():
        ranges.setdefault(path, []).extend(path_ranges)
    return ObservationSupport(files=files, ranges=ranges)


def _evidence_from_observation_support(
    support: ObservationSupport,
    limit: int = 5,
    priority_paths: list[str] | None = None,
) -> list[str]:
    evidence: list[str] = []
    for path, path_ranges in sorted(
        support.ranges.items(),
        key=lambda item: _prioritized_path_sort_key(item[0], priority_paths),
    ):
        merged = sorted(_merge_ranges(path_ranges), key=_range_sort_key)
        for start, end in merged:
            evidence.append(f"{path}:{start}-{end}")
            if len(evidence) >= limit:
                return evidence
    if evidence:
        return evidence
    return sorted(
        support.files,
        key=lambda path: _prioritized_path_sort_key(path, priority_paths),
    )[:limit]


def _evidence_from_trajectory(
    trajectory: list[dict[str, Any]],
    limit: int = 5,
) -> list[str]:
    evidence: list[str] = []
    seen: set[str] = set()
    for turn in reversed(trajectory):
        observations = turn.get("tool_observations", [])
        if not isinstance(observations, list):
            continue
        for observation in reversed(observations):
            if not isinstance(observation, dict) or not observation.get("ok"):
                continue
            for citation in _citations_from_observation(observation):
                if citation in seen:
                    continue
                seen.add(citation)
                evidence.append(citation)
                if len(evidence) >= limit:
                    return list(reversed(evidence))
    return list(reversed(evidence))


def _citations_from_observation(observation: dict[str, Any]) -> list[str]:
    result = observation.get("result")
    if not isinstance(result, dict):
        return []
    tool = str(observation.get("tool", ""))
    if tool == "Read":
        path = result.get("path")
        start = _optional_int(result.get("start_line"))
        end = _optional_int(result.get("end_line"))
        if not isinstance(path, str) or start is None or end is None or end < start:
            return []
        capped_end = min(end, start + 79)
        return [f"{path}:{start}-{capped_end}"]
    matches = result.get("matches")
    if not isinstance(matches, list):
        return []
    citations: list[str] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        path = match.get("path")
        start = _optional_int(match.get("start_line") or match.get("line"))
        end = _optional_int(match.get("end_line") or match.get("line"))
        if not isinstance(path, str) or start is None or end is None or end < start:
            continue
        citations.append(f"{path}:{start}-{min(end, start + 79)}")
    return citations


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if start <= 0 or end < start:
            continue
        capped_end = min(end, start + MAX_CITATION_LINES - 1)
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, capped_end))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (
                previous_start,
                min(max(previous_end, capped_end), previous_start + MAX_CITATION_LINES - 1),
            )
    return merged


def _range_sort_key(path_range: tuple[int, int]) -> tuple[int, int, int]:
    start, end = path_range
    line_count = max(0, end - start + 1)
    broad_penalty = 1 if line_count > FOCUSED_FINAL_CITATION_LINES else 0
    return broad_penalty, start, end


def _extract_tool_calls(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    raw_calls = parsed.get("tool_calls") or parsed.get("tools") or parsed.get("actions") or []
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if not isinstance(raw_calls, list):
        return []
    calls: list[dict[str, Any]] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            continue
        tool = _tool_name(raw_call)
        if tool in {"READ", "GLOB", "GREP"}:
            calls.append({"tool": tool, "args": _tool_args(raw_call)})
    return calls


def _extract_citations(parsed: dict[str, Any]) -> list[FastContextCitation]:
    final_answer = parsed.get("final_answer") or parsed.get("evidence") or parsed.get("citations")
    evidence: Any
    if isinstance(final_answer, dict):
        evidence = final_answer.get("evidence") or final_answer.get("citations") or []
    else:
        evidence = final_answer
    return _citations_from_value(evidence)


def _extract_citation_ids(parsed: dict[str, Any]) -> list[str]:
    final_answer = parsed.get("final_answer")
    raw_ids: Any
    if isinstance(final_answer, dict):
        raw_ids = final_answer.get("citation_ids") or final_answer.get("evidence_ids") or []
    else:
        raw_ids = parsed.get("citation_ids") or parsed.get("evidence_ids") or []
    return _citation_ids_from_value(raw_ids)


def _extract_notes(parsed: dict[str, Any]) -> list[str]:
    final_answer = parsed.get("final_answer")
    notes = final_answer.get("notes") if isinstance(final_answer, dict) else parsed.get("notes")
    if not isinstance(notes, list):
        return []
    return [str(note) for note in notes]


def _citations_from_value(value: Any) -> list[FastContextCitation]:
    if isinstance(value, str):
        return _parse_citation_lines(value)
    if not isinstance(value, list):
        return []
    citations: list[FastContextCitation] = []
    for item in value:
        if isinstance(item, str):
            citations.extend(_parse_citation_lines(item))
        elif isinstance(item, dict):
            path = str(item.get("path") or item.get("file") or "").strip()
            if not path:
                continue
            start_line = _optional_int(item.get("start_line") or item.get("start"))
            end_line = _optional_int(item.get("end_line") or item.get("end"))
            citations.append(
                FastContextCitation(
                    path=path,
                    start_line=start_line,
                    end_line=end_line,
                    reason=str(item.get("reason", "")),
                )
            )
    return citations


def _citation_ids_from_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return _parse_citation_ids(value)
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        ids.extend(_parse_citation_ids(str(item)))
    return _dedupe_preserve_order(ids)


def _parse_final_answer_citations(content: str) -> list[FastContextCitation]:
    block = re.search(
        r"<final_answer>\s*(?P<body>.*?)\s*</final_answer>",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if block:
        return _parse_citation_lines(block.group("body"))
    return _parse_citation_lines(content)


def _parse_final_answer_citation_ids(content: str) -> list[str]:
    block = re.search(
        r"<final_answer>\s*(?P<body>.*?)\s*</final_answer>",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if block:
        return _parse_citation_ids(block.group("body"))
    return _parse_citation_ids(content)


def _parse_citation_ids(text: str) -> list[str]:
    return _dedupe_preserve_order(
        match.group(0).upper() for match in re.finditer(r"\bC\d+\b", text, flags=re.IGNORECASE)
    )


def _dedupe_preserve_order(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _parse_citation_lines(text: str) -> list[FastContextCitation]:
    citations: list[FastContextCitation] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("-*` ")
        if not line:
            continue
        match = re.search(
            r"(?P<path>[\w./\\()[\]@ -]+\.(?:ts|tsx|js|jsx|json|md|css|scss|mjs|cjs))"
            r"[:#L]+(?P<start>\d+)(?:[-:L]+(?P<end>\d+))?",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        citations.append(
            FastContextCitation(
                path=match.group("path").strip(),
                start_line=int(match.group("start")),
                end_line=int(match.group("end")) if match.group("end") else None,
            )
        )
    return citations


def _parse_function_style_tool_calls(content: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for match in re.finditer(
        r"\b(?P<tool>READ|GLOB|GREP)\s*\((?P<body>.*?)\)",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        tool = match.group("tool").upper()
        body = match.group("body")
        args = _parse_call_args(body)
        if tool == "READ" and "path" not in args:
            quoted = _first_quoted(body)
            if quoted:
                args["path"] = quoted
        if tool == "GLOB" and "pattern" not in args:
            quoted = _first_quoted(body)
            if quoted:
                args["pattern"] = quoted
        if tool == "GREP" and "pattern" not in args:
            quoted = _first_quoted(body)
            if quoted:
                args["pattern"] = quoted
        calls.append({"tool": tool, "args": args})
    return calls
