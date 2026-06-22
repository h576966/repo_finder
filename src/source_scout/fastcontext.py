import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from . import catalog, lmstudio, path_safety
from .constants import SKIP_DIRS
from .models import LocalExploreResult

PROMPT_VERSION = "fastcontext-refine-v1"
SCHEMA_VERSION = "fastcontext-evidence-v1"
ANALYZER_VERSION = "fastcontext-harness-v1"

DEFAULT_MAX_TURNS = 6
MAX_TOOL_CALLS_PER_TURN = 5
MAX_GLOB_RESULTS = 80
MAX_GREP_RESULTS = 80
MAX_READ_LINES = 160
MAX_READ_FILE_BYTES = 240_000
MAX_GREP_FILE_BYTES = 1_000_000
MAX_CITATION_LINES = 240
MAX_FINAL_CITATION_CHOICES = 24
MAX_FINAL_CITATIONS = 3
MAX_FALLBACK_CITATIONS = 3
MAX_FINAL_FILES = 3
TARGET_FINAL_CITATIONS = 2
FOCUSED_FINAL_CITATION_LINES = 80
RG_TIMEOUT_SECONDS = 10
LOCAL_CONTEXT_FILE_LIMIT = 80
LOCAL_CONTEXT_GREP_LIMIT = 30
LOCAL_EXTRA_SKIP_DIRS = {".next", ".source_scout", "build", "coverage", "dist"}
FASTCONTEXT_STRUCTURED_OUTPUT_ENV = "SOURCE_SCOUT_FASTCONTEXT_STRUCTURED_OUTPUT"
PRIMARY_SOURCE_PREFIXES = ("src/source_scout/", "src/")
NOISY_EVIDENCE_PREFIXES = ("tests/", "docs/", "evals/")
NOISY_EVIDENCE_FILES = {"README.md", "AGENTS.md", "pyproject.toml"}
LOCAL_TASK_STOPWORDS = {
    "actual",
    "and",
    "are",
    "as",
    "before",
    "be",
    "code",
    "find",
    "for",
    "from",
    "into",
    "is",
    "of",
    "or",
    "local",
    "registered",
    "repo",
    "task",
    "that",
    "the",
    "this",
    "to",
    "where",
    "with",
    "working",
}


class FastContextError(RuntimeError):
    pass


class FastContextLoopError(FastContextError):
    def __init__(self, message: str, trajectory: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.trajectory = trajectory


@dataclass(frozen=True)
class FastContextCitation:
    path: str
    start_line: int | None = None
    end_line: int | None = None
    reason: str = ""

    def evidence_path(self) -> str:
        if self.start_line is None:
            return self.path
        end_line = self.end_line if self.end_line is not None else self.start_line
        return f"{self.path}:{self.start_line}-{max(self.start_line, end_line)}"


@dataclass(frozen=True)
class ParsedFastContextResponse:
    tool_calls: list[dict[str, Any]]
    citations: list[FastContextCitation]
    citation_ids: list[str]
    notes: list[str]


@dataclass(frozen=True)
class ObservationSupport:
    files: set[str]
    ranges: dict[str, list[tuple[int, int]]]


@dataclass(frozen=True)
class EvidenceBudgetResult:
    evidence_paths: list[str]
    notes: list[str]
    over_budget: bool
    truncated: bool
    original_count: int
    accepted_count: int
    original_file_count: int
    accepted_file_count: int


@dataclass(frozen=True)
class FastContextLoopResult:
    status: str
    evidence_paths: list[str]
    notes: list[str]
    trajectory: list[dict[str, Any]]


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

    task_sig = catalog.task_signature(task)
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
            {"candidate_id": candidate_id, "task_signature": task_sig, "error": str(exc)},
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

    try:
        loop_result = await _run_tool_loop(
            root=root,
            messages=_local_messages(root, task),
            model_id=config.fastcontext_model,
            config=config,
            max_turns=max_turns,
            transport=transport,
            allow_observation_fallback=True,
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
) -> FastContextLoopResult:
    active_messages = list(messages)
    trajectory: list[dict[str, Any]] = []
    observation_support = ObservationSupport(files=set(), ranges={})
    final_answer_only_next = False
    final_answer_retry_used = False
    budget_retry_used = False
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
            )
            if validation_notes:
                turn_record["validation_notes"] = validation_notes
            if evidence_paths:
                budget_result = _apply_evidence_budget(evidence_paths)
                _record_budget_result(turn_record, budget_result)
                if budget_result.over_budget and not budget_retry_used:
                    active_messages.extend(
                        _budget_feedback_messages(
                            content,
                            observation_support=observation_support,
                            budget_notes=budget_result.notes,
                        )
                    )
                    budget_retry_used = True
                    final_answer_only_next = True
                    continue
                turn_record["final_citations"] = budget_result.evidence_paths
                return FastContextLoopResult(
                    status="completed",
                    evidence_paths=budget_result.evidence_paths,
                    notes=[*parsed.notes, *validation_notes, *budget_result.notes],
                    trajectory=trajectory,
                )

        if tool_calls and allow_tools:
            observations = [
                execute_tool(root, call)
                for call in tool_calls[:MAX_TOOL_CALLS_PER_TURN]
            ]
            observation_support = _merge_observation_support(
                observation_support,
                _observation_support(observations),
            )
            turn_record["tool_observations"] = observations
            if tool_mode_response:
                active_messages.extend(
                    _tool_observation_messages(completion, observations)
                )
            else:
                active_messages.extend(
                    _legacy_observation_messages(content, observations)
                )
            finalization_reason = _finalization_reason(turn, max_turns, observation_support)
            turn_record["finalization_reason"] = finalization_reason
            if finalization_reason:
                active_messages.append(
                    _final_answer_request_message(
                        observation_support,
                        finalization_reason=finalization_reason,
                    )
                )
            elif tool_mode_response:
                active_messages.append(_continue_exploration_message(observation_support))
            final_answer_retry_used = False
            budget_retry_used = False
            final_answer_only_next = finalization_reason is not None
            continue

        if parsed.citation_ids or parsed.citations:
            if (
                not allow_tools
                and observation_support.ranges
                and not final_answer_retry_used
            ):
                active_messages.extend(
                    _validation_feedback_messages(
                        content,
                        turn_record,
                        observation_support=observation_support,
                        final_answer_only=True,
                    )
                )
                final_answer_retry_used = True
                final_answer_only_next = True
            elif not allow_tools and observation_support.ranges and allow_observation_fallback:
                return _fallback_observation_result(
                    observation_support,
                    trajectory,
                    note=(
                        "FastContext final-answer retry did not validate; "
                        "showing supported tool observations only."
                    ),
                )
            else:
                active_messages.extend(
                    _validation_feedback_messages(
                        content,
                        turn_record,
                        observation_support=observation_support,
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
                )
            )
            final_answer_retry_used = True
            final_answer_only_next = True
        elif not allow_tools and observation_support.ranges and allow_observation_fallback:
            return _fallback_observation_result(
                observation_support,
                trajectory,
                note=(
                    "FastContext final-answer retry did not produce citations; "
                    "showing supported tool observations only."
                ),
            )
        else:
            active_messages.extend(
                _final_response_feedback_messages(
                    content,
                    observation_support=observation_support,
                    final_answer_only=False,
                )
            )
            final_answer_only_next = False

    fallback_evidence = _evidence_from_trajectory(trajectory) or _evidence_from_observation_support(
        observation_support
    )
    if fallback_evidence:
        fallback_budget = _apply_evidence_budget(
            fallback_evidence,
            max_citations=MAX_FALLBACK_CITATIONS,
            max_files=MAX_FALLBACK_CITATIONS,
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
        )


def _fastcontext_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "Read a UTF-8 text file under the workspace root by line range.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to the workspace root. Do not shorten it.",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "1-based start line. Defaults to 1.",
                            "minimum": 1,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum lines to read.",
                            "minimum": 1,
                            "maximum": MAX_READ_LINES,
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Glob",
                "description": "List files under the workspace root using ripgrep-style glob patterns.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Directory relative to the workspace root. Defaults to '.'.",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern such as '**/*.ts' or 'src/**/*.tsx'.",
                        },
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "Grep",
                "description": "Search text under the workspace root with ripgrep-compatible options.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {
                            "type": "string",
                            "description": "Directory or file path relative to the workspace root.",
                        },
                        "glob": {"type": "string"},
                        "output_mode": {
                            "type": "string",
                            "enum": ["content", "files", "files_with_matches", "count"],
                        },
                        "-A": {"type": "integer", "minimum": 0, "maximum": 20},
                        "-B": {"type": "integer", "minimum": 0, "maximum": 20},
                        "-C": {"type": "integer", "minimum": 0, "maximum": 20},
                        "-n": {"type": "boolean"},
                        "-i": {"type": "boolean"},
                        "type": {"type": "string"},
                        "head_limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_GREP_RESULTS,
                        },
                        "multiline": {"type": "boolean"},
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        },
    ]


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


def _legacy_observation_messages(
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


def _continue_exploration_message(observation_support: ObservationSupport) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "Tool observations are available, but there is not enough strong citation support yet. "
            "Continue using Read, Glob, or Grep to gather focused file/line evidence. If you are "
            "already certain, you may return final_answer JSON with 1-3 citation_ids, ideally "
            f"{TARGET_FINAL_CITATIONS}, from the observed choices below.\n\n"
            f"{_observed_citation_choices_text(observation_support)}"
        ),
    }


def _final_answer_request_message(
    observation_support: ObservationSupport,
    *,
    feedback: str | None = None,
    finalization_reason: str | None = None,
) -> dict[str, str]:
    choices_text = _observed_citation_choices_text(observation_support)
    feedback_text = f"\n\nValidation feedback:\n{feedback}" if feedback else ""
    reason_text = f"\n\nFinalization reason: {finalization_reason}" if finalization_reason else ""
    return {
        "role": "user",
        "content": (
            "Tool observations are now available. Do not call tools on this turn. "
            "Return final_answer JSON only. Prefer citation_ids from the observed choices below, "
            "for example {\"final_answer\":{\"citation_ids\":[\"C1\"],\"notes\":[\"why\"]}}. "
            f"Choose 1-{MAX_FINAL_CITATIONS} citation IDs, ideally {TARGET_FINAL_CITATIONS}. "
            "Use the smallest set that directly answers the task. Do not include background, "
            "supporting, test, docs, or broad ranges unless they are necessary. "
            "Choose only from the observed citation choices below. "
            "Use exact relative paths and exact path:start-end line ranges. Do not cite directories, "
            "wildcards, globs, or shortened paths such as /source_scout/src, source_scout/src, "
            "evals/*.py, or src/**. Prefer src/source_scout choices over tests, docs, and evals "
            "unless the task explicitly asks for tests or documentation.\n\n"
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
                feedback=(
                    f"{feedback} Retry once using only exact observed path:start-end choices."
                ),
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
        ),
    ]


def _observed_citation_choices_text(support: ObservationSupport) -> str:
    choice_items = _observed_citation_choice_items(support)
    if not choice_items:
        files = "\n".join(
            f"- {path}"
            for path in sorted(support.files, key=_evidence_path_sort_key)[:MAX_FINAL_CITATION_CHOICES]
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
) -> list[str]:
    return [
        citation.evidence_path()
        for _choice_id, citation in _observed_citation_choice_items(support, limit=limit)
    ]


def _observed_citation_choice_items(
    support: ObservationSupport,
    limit: int = MAX_FINAL_CITATION_CHOICES,
) -> list[tuple[str, FastContextCitation]]:
    choices: list[tuple[str, FastContextCitation]] = []
    for path in sorted(support.ranges, key=_evidence_path_sort_key):
        for start, end in _merge_ranges(support.ranges[path]):
            choice_id = f"C{len(choices) + 1}"
            choices.append((choice_id, FastContextCitation(path=path, start_line=start, end_line=end)))
            if len(choices) >= limit:
                return choices
    return choices


def _observed_citation_choice_map(support: ObservationSupport) -> dict[str, FastContextCitation]:
    return {
        choice_id: citation
        for choice_id, citation in _observed_citation_choice_items(support)
    }


def _apply_evidence_budget(
    evidence_paths: list[str],
    *,
    max_citations: int = MAX_FINAL_CITATIONS,
    max_files: int = MAX_FINAL_FILES,
) -> EvidenceBudgetResult:
    unique_paths = sorted(set(evidence_paths), key=_evidence_citation_sort_key)
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
            "Citation budget applied: "
            f"accepted {len(accepted)} citations across {accepted_file_count} files."
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


def _evidence_citation_sort_key(evidence_path: str) -> tuple[int, str, int, str]:
    path = _citation_path(evidence_path)
    start_line = 0
    match = re.match(r".+?:(?P<start>\d+)(?:-\d+)?$", evidence_path)
    if match:
        start_line = int(match.group("start"))
    priority, normalized = _evidence_path_sort_key(path)
    return priority, normalized, start_line, evidence_path


def _finalization_reason(
    turn: int,
    max_turns: int,
    support: ObservationSupport,
) -> str | None:
    choices = _observed_citation_choice_items(support)
    primary_choices = [
        citation
        for _choice_id, citation in choices
        if _is_primary_source_path(citation.path)
    ]
    focused_primary_count = sum(
        1 for citation in primary_choices if _is_focused_citation(citation)
    )
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


def _fallback_observation_result(
    support: ObservationSupport,
    trajectory: list[dict[str, Any]],
    *,
    note: str,
) -> FastContextLoopResult:
    budget_result = _apply_evidence_budget(
        _evidence_from_observation_support(support),
        max_citations=MAX_FALLBACK_CITATIONS,
        max_files=MAX_FALLBACK_CITATIONS,
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


def _evidence_path_sort_key(path: str) -> tuple[int, str]:
    normalized = path.replace("\\", "/")
    if _is_primary_source_path(normalized):
        return (0, normalized)
    if _is_noisy_evidence_path(normalized):
        return (2, normalized)
    return (1, normalized)


def _is_primary_source_path(path: str) -> bool:
    return path.replace("\\", "/").startswith(PRIMARY_SOURCE_PREFIXES)


def _is_noisy_evidence_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized in NOISY_EVIDENCE_FILES or normalized.startswith(NOISY_EVIDENCE_PREFIXES)


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


def _match_sort_key(match: dict[str, Any]) -> tuple[int, str, int]:
    path = str(match.get("path", ""))
    line = _optional_int(match.get("start_line") or match.get("line")) or 0
    priority, normalized = _evidence_path_sort_key(path)
    return priority, normalized, line


def _tool_observation_content(observation: dict[str, Any]) -> str:
    if observation.get("ok") and isinstance(observation.get("text"), str):
        return str(observation["text"])
    return json.dumps(observation, sort_keys=True)


def _structured_output_enabled() -> bool:
    raw = os.environ.get(FASTCONTEXT_STRUCTURED_OUTPUT_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


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


def execute_tool(root: Path, call: dict[str, Any]) -> dict[str, Any]:
    tool = _canonical_tool_name(_tool_name(call))
    args = _tool_args(call)
    try:
        if args.get("arguments_error") or call.get("arguments_error"):
            raise FastContextError(str(args.get("arguments_error") or call["arguments_error"]))
        if tool == "Read":
            result = read_file(
                root,
                str(args.get("path", "")),
                offset=_optional_int(args.get("offset") or args.get("start") or args.get("start_line")),
                limit=_optional_int(args.get("limit")),
                end=_optional_int(args.get("end") or args.get("end_line")),
            )
        elif tool == "Glob":
            result = glob_paths(
                root,
                str(args.get("pattern") or args.get("glob") or "**/*"),
                directory=str(args.get("directory") or "."),
            )
        elif tool == "Grep":
            result = grep_paths(
                root,
                str(args.get("pattern", "")),
                file_glob=str(args["glob"]) if args.get("glob") else None,
                search_path=str(args.get("path") or "."),
                output_mode=str(args.get("output_mode") or "content"),
                before_context=_optional_int(args.get("-B")) or 0,
                after_context=_optional_int(args.get("-A")) or 0,
                context=_optional_int(args.get("-C")) or 0,
                line_numbers=bool(args.get("-n", True)),
                ignore_case=bool(args.get("-i", False)),
                file_type=str(args["type"]) if args.get("type") else None,
                head_limit=_optional_int(args.get("head_limit")) or MAX_GREP_RESULTS,
                multiline=bool(args.get("multiline", False)),
            )
        else:
            raise FastContextError(f"Unsupported tool: {tool}")
        return {
            "tool_call_id": call.get("id"),
            "tool": tool,
            "args": args,
            "ok": True,
            "result": result,
            "text": _tool_result_text(tool, result),
        }
    except Exception as exc:
        return {
            "tool_call_id": call.get("id"),
            "tool": tool,
            "args": args,
            "ok": False,
            "error": _tool_error_text(root, exc),
        }


def _tool_error_text(root: Path, exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if any(term in lowered for term in ["escapes", "absolute", "relative", "does not exist"]):
        return (
            f"{message} Use paths relative to {root.resolve()}, for example "
            "src/source_scout/server.py, not /source_scout/src/source_scout/server.py."
        )
    return message


def read_file(
    root: Path,
    rel_path: str,
    start: int | None = None,
    offset: int | None = None,
    limit: int | None = None,
    end: int | None = None,
) -> dict[str, Any]:
    path, safe_rel = _resolve_under_root(root, rel_path)
    if not path.is_file():
        raise FastContextError(f"READ target is not a file: {safe_rel}")
    if path.stat().st_size > MAX_READ_FILE_BYTES:
        raise FastContextError(f"READ target is too large: {safe_rel}")

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return {"path": safe_rel, "start_line": 1, "end_line": 0, "content": "", "line_count": 0}

    start_line = min(max(1, offset or start or 1), len(lines))
    read_limit = min(max(1, limit or MAX_READ_LINES), MAX_READ_LINES)
    end_line = min(len(lines), end or start_line + read_limit - 1)
    if end_line - start_line + 1 > MAX_READ_LINES:
        end_line = start_line + MAX_READ_LINES - 1
    selected = lines[start_line - 1 : end_line]
    content = "\n".join(
        f"{line_number}|{line}"
        for line_number, line in enumerate(selected, start=start_line)
    )
    return {
        "path": safe_rel,
        "start_line": start_line,
        "end_line": end_line,
        "content": content,
        "line_count": len(lines),
    }


def glob_paths(
    root: Path,
    pattern: str,
    limit: int = MAX_GLOB_RESULTS,
    directory: str = ".",
) -> dict[str, Any]:
    safe_pattern = _safe_glob_pattern(root, pattern)
    directory_path, safe_directory = _resolve_directory(root, directory)
    rg_matches = _rg_glob(root, directory_path, safe_pattern, limit)
    if rg_matches is not None:
        return {
            "directory": safe_directory,
            "pattern": safe_pattern,
            "matches": rg_matches[:limit],
            "truncated": len(rg_matches) >= limit,
            "backend": "rg",
        }
    matches: list[str] = []
    for path in _iter_files(root, file_glob=safe_pattern):
        if len(matches) >= limit:
            break
        if not _path_is_under(path, directory_path):
            continue
        matches.append(_relative_path(root, path))
    matches.sort(key=_evidence_path_sort_key)
    return {
        "directory": safe_directory,
        "pattern": safe_pattern,
        "matches": matches,
        "truncated": len(matches) >= limit,
        "backend": "python",
    }


def grep_paths(
    root: Path,
    pattern: str,
    file_glob: str | None = None,
    limit: int = MAX_GREP_RESULTS,
    search_path: str = ".",
    output_mode: str = "content",
    before_context: int = 0,
    after_context: int = 0,
    context: int = 0,
    line_numbers: bool = True,
    ignore_case: bool = False,
    file_type: str | None = None,
    head_limit: int | None = None,
    multiline: bool = False,
) -> dict[str, Any]:
    if not pattern.strip():
        raise FastContextError("GREP requires a non-empty pattern.")
    effective_limit = min(max(1, head_limit or limit), MAX_GREP_RESULTS)
    search_root, safe_search_path = _resolve_search_path(root, search_path)
    rg_result = _rg_grep(
        root=root,
        search_path=search_root,
        safe_search_path=safe_search_path,
        pattern=pattern,
        file_glob=file_glob,
        limit=effective_limit,
        output_mode=output_mode,
        before_context=before_context,
        after_context=after_context,
        context=context,
        line_numbers=line_numbers,
        ignore_case=ignore_case,
        file_type=file_type,
        multiline=multiline,
    )
    if rg_result is not None:
        return rg_result
    try:
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(pattern, flags=flags)
        regex_mode = True
    except re.error:
        compiled = re.compile(re.escape(pattern), flags=re.IGNORECASE if ignore_case else 0)
        regex_mode = False

    candidates = [
        path for path in _grep_candidate_files(root, file_glob)
        if _path_is_under(path, search_root)
    ]
    candidates.sort(key=lambda path: _evidence_path_sort_key(_relative_path(root, path)))
    matches: list[dict[str, Any]] = []
    for path in candidates:
        if len(matches) >= effective_limit:
            break
        if path.stat().st_size > MAX_GREP_FILE_BYTES:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel_path = _relative_path(root, path)
        file_matched = False
        for line_number, line in enumerate(lines, start=1):
            if not compiled.search(line):
                continue
            file_matched = True
            if output_mode in {"files", "files_with_matches"}:
                matches.append({"path": rel_path})
                break
            if output_mode == "count":
                continue
            start_line = max(1, line_number - (context or before_context))
            end_line = min(len(lines), line_number + (context or after_context))
            matches.append(
                {
                    "path": rel_path,
                    "line": line_number,
                    "start_line": start_line,
                    "end_line": end_line,
                    "citation": f"{rel_path}:{line_number}-{line_number}",
                    "text": line.strip()[:220],
                }
            )
            if len(matches) >= effective_limit:
                break
        if output_mode == "count" and file_matched:
            count = sum(1 for line in lines if compiled.search(line))
            matches.append({"path": rel_path, "count": count})

    return {
        "path": safe_search_path,
        "pattern": pattern,
        "glob": file_glob,
        "regex_mode": regex_mode,
        "matches": matches,
        "truncated": len(matches) >= effective_limit,
        "output_mode": output_mode,
        "backend": "python",
    }


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
        candidate.repo_id in task["expected_repo_ids"]
        or candidate.repo_id in task["acceptable_repo_ids"]
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
    candidates = [
        candidate
        for task in task_reports
        for candidate in task["candidates"]
    ]
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
        if candidate["refinement_status"] == "completed"
        and int(candidate["refined_evidence_count"]) > 0
    ]
    refined_path_constraint_failures = sum(
        1
        for candidate in label_matches
        if candidate["refinement_status"] == "completed"
        and not candidate["refined_path_constraint_ok"]
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
        ) if deterministic_evidence_total else 0.0,
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
                f"Context JSON:\n{json.dumps(context, sort_keys=True)}\n\n"
                f"Exploration query:\n{query}"
            ),
        },
    ]


def _local_messages(root: Path, task: str) -> list[dict[str, str]]:
    context = {
        "mode": "local-project-exploration",
        "project_path": str(root),
        "absolute_workspace_root": str(root),
        "task": task.strip(),
        "seed_context": _local_seed_context(root, task),
        "rules": [
            "Read-only exploration only.",
            "Do not execute project code.",
            "Return file paths relative to project_path.",
            "Use the absolute workspace root only to understand scope; do not shorten paths.",
            "Use relative tool paths like src/source_scout/server.py, not shortened pseudo-absolute paths.",
            "Treat seed_context.likely_source_files as ordered; inspect the first relevant "
            "entries before broad search.",
            "Prefer primary source tree files over docs, generated, build, vendor, sample, and fixture code.",
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
                "when the ordered hints are insufficient, then narrow down. Prefer primary "
                "source tree files over docs, generated output, build output, "
                "vendored code, samples, and fixtures unless the task asks for those. If the task names "
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


def _local_seed_context(root: Path, task: str) -> dict[str, Any]:
    files = glob_paths(root, "**/*", limit=LOCAL_CONTEXT_FILE_LIMIT)
    pattern = _task_grep_pattern(task)
    matches: list[dict[str, Any]] = []
    if pattern:
        matches = grep_paths(root, pattern, limit=LOCAL_CONTEXT_GREP_LIMIT)["matches"]
    terms = _task_terms(task)
    return {
        "likely_source_files": _likely_source_files(root, terms, matches),
        "known_files_sample": files["matches"],
        "known_files_truncated": files["truncated"],
        "initial_grep_pattern": pattern,
        "initial_grep_matches": matches,
    }


def _task_grep_pattern(task: str) -> str:
    return "|".join(re.escape(term) for term in _task_terms(task)[:14])


def _task_terms(task: str) -> list[str]:
    raw_terms = [
        raw_term.lower().replace("-", "_")
        for raw_term in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{1,}", task)
    ]
    terms: list[str] = []

    def add_term(term: str) -> None:
        if term not in terms:
            terms.append(term)

    for term in raw_terms:
        if len(term) >= 3 and term not in LOCAL_TASK_STOPWORDS:
            add_term(term)
            if term.endswith("s") and len(term) > 4:
                add_term(term[:-1])
    for left, right in zip(raw_terms, raw_terms[1:]):
        joined = f"{left}{right}"
        if len(joined) >= 5 and left not in LOCAL_TASK_STOPWORDS and right not in LOCAL_TASK_STOPWORDS:
            add_term(joined)
    return terms


def _likely_source_files(
    root: Path,
    terms: list[str],
    grep_matches: list[dict[str, Any]],
    limit: int = 18,
) -> list[str]:
    scores: dict[str, int] = {}
    term_set = set(terms)
    for match in grep_matches:
        path = match.get("path")
        if isinstance(path, str):
            priority, _ = _evidence_path_sort_key(path)
            scores[path] = scores.get(path, 0) + (3 if priority == 0 else 1)

    for path in _iter_files(root):
        rel_path = _relative_path(root, path)
        searchable = rel_path.lower().replace("-", "_")
        stem = path.stem.lower().replace("-", "_")
        score = sum(5 for term in term_set if term in searchable or term in stem)
        score += _task_file_bonus(rel_path, term_set)
        if {"cli", "command", "commands"} & term_set and path.name in {"__main__.py", "cli.py"}:
            score += 6
        if {"mcp", "tool", "tools", "server"} & term_set and path.name in {"server.py"}:
            score += 5
        if (
            {"model", "models", "result", "results", "shape", "shapes"} & term_set
            and path.name == "models.py"
        ):
            score += 6
        if score:
            if rel_path.startswith(PRIMARY_SOURCE_PREFIXES):
                score += 2
            scores[rel_path] = scores.get(rel_path, 0) + score

    ranked = sorted(
        scores,
        key=lambda path: (
            _seed_path_priority(path, term_set),
            -scores[path],
            _evidence_path_sort_key(path)[1],
        ),
    )
    return ranked[:limit]


def _task_file_bonus(rel_path: str, term_set: set[str]) -> int:
    normalized = rel_path.replace("\\", "/")
    bonus = 0
    if normalized == "src/source_scout/pipeline.py":
        if {"scout", "freshness", "created", "pushed", "query", "queries"} & term_set:
            bonus += 14
        if {
            "qualification",
            "rejects",
            "reject",
            "archived",
            "forked",
            "template",
            "mirror",
            "oversized",
            "docs_only",
            "vendor_heavy",
        } & term_set:
            bonus += 14
    if normalized == "src/source_scout/constants.py" and {
        "freshness",
        "created",
        "pushed",
        "size",
        "stale",
    } & term_set:
        bonus += 10
    if normalized == "src/source_scout/catalog.py" and {
        "catalog",
        "assets",
        "asset",
        "searched",
        "scored",
        "search",
        "score",
        "gemma",
        "profile",
        "capability",
        "intent",
    } & term_set:
        bonus += 100
    if normalized == "src/source_scout/profiler.py" and {"gemma", "profile", "profiler"} & term_set:
        bonus += 8
    if normalized == "src/source_scout/ranker.py" and {
        "ranker",
        "ranking",
        "scoring",
        "score",
        "factors",
        "factor",
        "legacy",
    } & term_set:
        bonus += 100
    if normalized == "src/source_scout/server.py" and {
        "mcp",
        "tool",
        "tools",
        "server",
        "exposed",
        "read_only",
    } & term_set:
        bonus += 14
    if normalized == "src/source_scout/bundles.py" and {
        "bundle",
        "bundles",
        "opened_bundle",
        "source",
    } & term_set:
        if {"source", "created", "opened_bundle", "recorded"} & term_set:
            bonus += 100
        else:
            bonus += 20
    if normalized == "src/source_scout/models.py" and {
        "dataclass",
        "dataclasses",
        "model",
        "models",
        "result",
        "results",
        "shape",
        "shapes",
        "candidate",
        "bundle",
        "outcome",
        "explore_local",
    } & term_set:
        bonus += 100
    if normalized == "src/source_scout/fastcontext.py" and {
        "fastcontext",
        "explore_local",
        "exploration",
        "structured",
        "output",
        "schema",
        "read_only",
    } & term_set:
        bonus += 12
    if normalized == "src/source_scout/lmstudio.py" and {
        "lm",
        "lmstudio",
        "structured",
        "output",
        "schema",
        "json",
    } & term_set:
        bonus += 10
    if normalized == "README.md" and {"documentation", "docs", "readme", "usage"} & term_set:
        bonus += 18
    if normalized == "AGENTS.md" and {"documentation", "docs", "agents", "usage"} & term_set:
        bonus += 12
    return bonus


def _seed_path_priority(path: str, term_set: set[str]) -> int:
    if {"documentation", "docs", "readme", "usage"} & term_set:
        normalized = path.replace("\\", "/")
        if normalized in {"README.md", "AGENTS.md"} or normalized.startswith("docs/"):
            return -1
    return _evidence_path_sort_key(path)[0]


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
                    _canonical_tool_name(_tool_name(call))
                    for call in tool_calls
                    if isinstance(call, dict)
                ] if isinstance(tool_calls, list) else [],
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
        task_signature=task_signature,
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
) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    if parsed.citation_ids:
        id_paths, id_notes = _validated_citation_id_paths(
            root,
            parsed.citation_ids,
            observation_support,
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
) -> tuple[list[str], list[str]]:
    id_map = _observed_citation_choice_map(observation_support)
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
            f"Skipped citation beyond EOF: {safe_rel}:{start_line}-{end_line} "
            f"(file has {line_count} lines)"
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
) -> list[str]:
    evidence: list[str] = []
    for path, path_ranges in sorted(support.ranges.items()):
        merged = _merge_ranges(path_ranges)
        for start, end in merged:
            evidence.append(f"{path}:{start}-{end}")
            if len(evidence) >= limit:
                return evidence
    if evidence:
        return evidence
    return sorted(support.files)[:limit]


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
        match.group(0).upper()
        for match in re.finditer(r"\bC\d+\b", text, flags=re.IGNORECASE)
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


def _parse_call_args(body: str) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for match in re.finditer(
        r"(?P<key>\w+)\s*=\s*(?P<value>\"[^\"]*\"|'[^']*'|\d+)",
        body,
        flags=re.DOTALL,
    ):
        value = match.group("value").strip()
        if value.isdigit():
            args[match.group("key")] = int(value)
        else:
            args[match.group("key")] = value.strip("\"'")
    return args


def _first_quoted(value: str) -> str | None:
    match = re.search(r"\"([^\"]+)\"|'([^']+)'", value)
    if not match:
        return None
    return str(match.group(1) or match.group(2))


def _tool_name(call: dict[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"]).upper()
    return str(call.get("tool") or call.get("name") or "").upper()


def _tool_args(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function")
    raw_args: Any = call.get("args") or call.get("arguments")
    if isinstance(function, dict) and function.get("arguments") is not None:
        raw_args = function["arguments"]
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return _parse_call_args(raw_args)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _canonical_tool_name(name: str) -> str:
    normalized = name.strip().upper()
    if normalized == "READ":
        return "Read"
    if normalized == "GLOB":
        return "Glob"
    if normalized == "GREP":
        return "Grep"
    return name


def _tool_result_text(tool: str, result: dict[str, Any]) -> str:
    if tool == "Read":
        path = str(result.get("path", ""))
        start = int(result.get("start_line", 1))
        end = int(result.get("end_line", start))
        return f"```{path}:{start}-{end}\n{result.get('content', '')}\n```"
    if tool == "Glob":
        matches = result.get("matches")
        if isinstance(matches, list):
            return "\n".join(str(match) for match in matches)
    if tool == "Grep":
        matches = result.get("matches")
        if isinstance(matches, list):
            lines = []
            for match in matches:
                if not isinstance(match, dict):
                    continue
                path = str(match.get("path", ""))
                line = match.get("line")
                text = str(match.get("text", ""))
                if line is None:
                    lines.append(path)
                else:
                    lines.append(f"{path}:{line}:{text}")
            return "\n".join(lines)
    return json.dumps(result, sort_keys=True)


def _rg_glob(
    root: Path,
    directory_path: Path,
    pattern: str,
    limit: int,
) -> list[str] | None:
    rg = shutil.which("rg")
    if rg is None:
        return None
    safe_directory = _relative_path(root, directory_path) if directory_path != root.resolve() else "."
    command = [
        rg,
        "--files",
        safe_directory,
        "--glob",
        pattern,
        *_rg_skip_globs(),
    ]
    completed = _run_rg(root, command)
    if completed is None:
        return None
    if completed.returncode not in {0, 1}:
        return None
    matches = [
        _normalize_rg_path(line)
        for line in completed.stdout.splitlines()
        if line.strip()
    ]
    safe_matches = [
        path
        for path in matches
        if _is_safe_relative_result(root, path)
    ]
    return sorted(safe_matches, key=_evidence_path_sort_key)[:limit]


def _rg_grep(
    *,
    root: Path,
    search_path: Path,
    safe_search_path: str,
    pattern: str,
    file_glob: str | None,
    limit: int,
    output_mode: str,
    before_context: int,
    after_context: int,
    context: int,
    line_numbers: bool,
    ignore_case: bool,
    file_type: str | None,
    multiline: bool,
) -> dict[str, Any] | None:
    rg = shutil.which("rg")
    if rg is None:
        return None
    safe_glob = _safe_glob_pattern(root, file_glob) if file_glob else None
    search_arg = safe_search_path or "."
    command = [rg, "--color", "never", "--no-heading", "--with-filename"]
    if line_numbers:
        command.append("--line-number")
    if ignore_case:
        command.append("--ignore-case")
    if multiline:
        command.append("--multiline")
    if output_mode in {"files", "files_with_matches"}:
        command.append("--files-with-matches")
    elif output_mode == "count":
        command.append("--count")
    if context:
        command.extend(["-C", str(max(0, context))])
    else:
        if before_context:
            command.extend(["-B", str(max(0, before_context))])
        if after_context:
            command.extend(["-A", str(max(0, after_context))])
    if safe_glob:
        command.extend(["--glob", safe_glob])
    if file_type:
        command.extend(["--type", file_type])
    command.extend([*_rg_skip_globs(), pattern, search_arg])
    completed = _run_rg(root, command)
    if completed is None:
        return None
    if completed.returncode not in {0, 1}:
        return None
    matches = _parse_rg_output(
        root=root,
        output=completed.stdout,
        output_mode=output_mode,
        limit=limit,
    )
    return {
        "path": safe_search_path,
        "pattern": pattern,
        "glob": safe_glob,
        "regex_mode": True,
        "matches": matches,
        "truncated": len(matches) >= limit,
        "output_mode": output_mode,
        "backend": "rg",
        "searched_path": _relative_path(root, search_path) if search_path != root.resolve() else ".",
    }


def _run_rg(root: Path, command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=RG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _rg_skip_globs() -> list[str]:
    args: list[str] = []
    for dirname in sorted(SKIP_DIRS | LOCAL_EXTRA_SKIP_DIRS):
        args.extend(["--glob", f"!{dirname}/**"])
    return args


def _parse_rg_output(
    *,
    root: Path,
    output: str,
    output_mode: str,
    limit: int,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    scan_limit = max(limit, limit * 5)
    for raw_line in output.splitlines():
        if len(matches) >= scan_limit:
            break
        line = raw_line.strip()
        if not line:
            continue
        if output_mode in {"files", "files_with_matches"}:
            path = _normalize_rg_path(line)
            if _is_safe_relative_result(root, path):
                matches.append({"path": path})
            continue
        if output_mode == "count":
            path, raw_count = _split_rg_pair(line)
            if path and _is_safe_relative_result(root, path):
                counts[path] = counts.get(path, 0) + (_optional_int(raw_count) or 0)
            continue
        parsed = _parse_rg_content_line(line)
        if parsed is None:
            continue
        path, line_number, text = parsed
        if not _is_safe_relative_result(root, path):
            continue
        matches.append(
            {
                "path": path,
                "line": line_number,
                "start_line": line_number,
                "end_line": line_number,
                "citation": f"{path}:{line_number}-{line_number}",
                "text": text[:220],
            }
        )
    if output_mode == "count":
        matches.extend(
            {"path": path, "count": count}
            for path, count in sorted(counts.items())
        )
    return sorted(matches, key=_match_sort_key)[:limit]


def _split_rg_pair(line: str) -> tuple[str, str]:
    if ":" not in line:
        return _normalize_rg_path(line), ""
    path, value = line.rsplit(":", 1)
    return _normalize_rg_path(path), value


def _parse_rg_content_line(line: str) -> tuple[str, int, str] | None:
    match = re.match(r"(?P<path>.*?)(?P<sep>[:-])(?P<line>\d+)(?P=sep)(?P<text>.*)", line)
    if match is None:
        return None
    line_number = _optional_int(match.group("line"))
    if line_number is None:
        return None
    return _normalize_rg_path(match.group("path")), line_number, match.group("text").strip()


def _normalize_rg_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("./")


def _is_safe_relative_result(root: Path, rel_path: str) -> bool:
    return path_safety.is_safe_relative_result(
        root,
        rel_path,
        extra_skip_dirs=LOCAL_EXTRA_SKIP_DIRS,
    )


def _resolve_under_root(root: Path, rel_path: str) -> tuple[Path, str]:
    try:
        return path_safety.resolve_under_root(
            root,
            rel_path,
            extra_skip_dirs=LOCAL_EXTRA_SKIP_DIRS,
        )
    except path_safety.PathSafetyError as exc:
        raise FastContextError(str(exc)) from exc


def _relative_path(root: Path, path: Path) -> str:
    return path_safety.relative_path(root, path)


def _normalize_workspace_reference(
    root: Path,
    value: str,
    *,
    strip_line: bool = True,
    allow_glob: bool = False,
) -> str:
    return path_safety.normalize_workspace_reference(
        root,
        value,
        strip_line=strip_line,
        allow_glob=allow_glob,
    )


def _workspace_suffix_reference(
    root: Path,
    cleaned: str,
    *,
    allow_glob: bool,
) -> str | None:
    return path_safety.workspace_suffix_reference(root, cleaned, allow_glob=allow_glob)


def _workspace_suffix_target_exists(
    root: Path,
    suffix: str,
    *,
    allow_glob: bool,
) -> bool:
    return path_safety.workspace_suffix_target_exists(root, suffix, allow_glob=allow_glob)


def _has_glob_meta(value: str) -> bool:
    return path_safety.has_glob_meta(value)


def _safe_glob_pattern(root: Path, pattern: str) -> str:
    try:
        return path_safety.safe_glob_pattern(root, pattern)
    except path_safety.PathSafetyError as exc:
        raise FastContextError(str(exc)) from exc


def _resolve_directory(root: Path, directory: str) -> tuple[Path, str]:
    cleaned = _normalize_workspace_reference(root.resolve(), directory) or "."
    if cleaned == ".":
        return root.resolve(), "."
    path, safe_rel = _resolve_under_root(root, cleaned)
    if not path.is_dir():
        raise FastContextError(f"Glob directory is not a directory: {safe_rel}")
    return path, safe_rel


def _resolve_search_path(root: Path, search_path: str) -> tuple[Path, str]:
    cleaned = _normalize_workspace_reference(root.resolve(), search_path) or "."
    if cleaned == ".":
        return root.resolve(), "."
    path, safe_rel = _resolve_under_root(root, cleaned)
    if not path.exists():
        raise FastContextError(f"Grep path does not exist: {safe_rel}")
    return path, safe_rel


def _path_is_under(path: Path, root: Path) -> bool:
    return path_safety.path_is_under(path, root)


def _should_skip_path(root: Path, path: Path) -> bool:
    return path_safety.should_skip_path(
        root,
        path,
        extra_skip_dirs=LOCAL_EXTRA_SKIP_DIRS,
    )


def _grep_candidate_files(root: Path, file_glob: str | None) -> list[Path]:
    safe_pattern = _safe_glob_pattern(root, file_glob) if file_glob else None
    return list(_iter_files(root, file_glob=safe_pattern))


def _iter_files(root: Path, file_glob: str | None = None) -> list[Path]:
    root_resolved = root.resolve()
    matches: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root_resolved):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in SKIP_DIRS and dirname not in LOCAL_EXTRA_SKIP_DIRS
        ]
        current_path = Path(current_root)
        for filename in filenames:
            path = current_path / filename
            if _should_skip_path(root_resolved, path):
                continue
            rel_path = _relative_path(root_resolved, path)
            if file_glob and not PurePosixPath(rel_path).match(file_glob):
                continue
            matches.append(path)
    return sorted(matches, key=lambda path: _evidence_path_sort_key(_relative_path(root_resolved, path)))


def _strip_line_suffix(path: str) -> str:
    return path_safety.strip_line_suffix(path)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_label(label: str | None) -> str:
    if not label:
        return ""
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in label)
