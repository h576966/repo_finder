import asyncio
import math
import os
from collections.abc import Awaitable
from typing import Annotated, Any, Literal, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from . import deepseek, fastcontext
from .cli_status import _api_status, _error_status
from .constants import _now_iso
from .exploration_policy import ExplorationUseCase, LocalMethod
from .failures import FailureDetails, failure_from_exception
from .models import (
    FindReusableCodeResult,
    FindReuseReferencesResult,
    LocalExploreResult,
    RecordReuseOutcomeResult,
    ReuseContextResult,
    SourceBundleResult,
)
from .target_profile import TargetProfileError, build_target_profile

mcp = FastMCP(
    "SourceScout",
    instructions=(
        "Codex owns reasoning, edits and verification. Use rg for exact text and Serena for symbols. "
        "Run source-scout check in the terminal. Use the remote investigator only for unresolved "
        "cross-file contracts, indirect runtime flow, ambiguous ownership or architecture relations "
        "after local navigation. Supply use_case, attempted_local_methods and a concrete reason. "
        "Project policy controls access. Citations are navigation, not proof of completion."
    ),
)
reuse_mcp = FastMCP("SourceScoutReuse")
reference_mcp = FastMCP(
    "SourceScoutReferences",
    instructions=(
        "Deterministic implementation references. Find returns at most three evidence-backed local "
        "matches; context revalidates exact commit-pinned bytes. Codex decides whether and how to "
        "adapt ideas."
    ),
)
DEFAULT_MCP_DEADLINE_SECONDS = 270.0
_T = TypeVar("_T")

DEFAULT_MCP_TOOL_NAMES = ("explore_local_code",)
REUSE_MCP_TOOL_NAMES = (
    *DEFAULT_MCP_TOOL_NAMES,
    "model_status",
    "find_reusable_code",
    "assess_reusable_code",
    "get_source_bundle",
    "record_reuse_outcome",
)
REFERENCE_MCP_TOOL_NAMES = ("find_reuse_references", "get_reuse_context")


def create_server(profile: str = "sidecar") -> FastMCP:
    if profile == "sidecar":
        return mcp
    if profile == "references":
        return reference_mcp
    if profile != "reuse":
        raise ValueError("Unknown MCP profile; use sidecar, references or reuse.")
    server = FastMCP("SourceScoutReuse", instructions="Explicit legacy catalog reuse profile.")
    server.mount(mcp)
    server.mount(reuse_mcp)
    return server


@reference_mcp.tool(
    description=(
        "Find up to three deterministic matches in the explicitly added personal/curated reference "
        "collection. Uses source, paths, identifiers, manifests and absolute relevance evidence. "
        "Does not use models, network discovery, capability ontologies, assessments or bundles."
    ),
    annotations={"readOnlyHint": True},
)
def find_reuse_references(
    task: Annotated[str, Field(description="Concrete implementation pattern to find")],
    project_path: Annotated[
        str | None,
        Field(description="Optional target project for advisory deterministic compatibility facts"),
    ] = None,
    max_results: Annotated[int, Field(description="Maximum results", ge=1, le=3)] = 3,
) -> FindReuseReferencesResult:
    from .reuse_references import ReuseReferenceError
    from .reuse_references import find_reuse_references as find

    try:
        return find(task, project_path=project_path, max_results=max_results)
    except (ReuseReferenceError, TargetProfileError, OSError) as exc:
        raise ToolError(str(exc)) from exc


@reference_mcp.tool(
    description=(
        "Return bounded, exact line ranges and hashes from a commit-pinned reference returned by "
        "find_reuse_references. Revalidates snapshot commit and bytes; no assessment or bundle required."
    ),
    annotations={"readOnlyHint": True},
)
def get_reuse_context(
    candidate_id: Annotated[str, Field(description="Stable candidate ID returned by find_reuse_references")],
    task: Annotated[str, Field(description="Task used to select relevant line windows")] = "",
    project_path: Annotated[
        str | None,
        Field(description="Optional target project for advisory deterministic compatibility facts"),
    ] = None,
) -> ReuseContextResult:
    from .reuse_references import ReuseReferenceError
    from .reuse_references import get_reuse_context as get_context

    try:
        return get_context(candidate_id, task=task, project_path=project_path)
    except (ReuseReferenceError, TargetProfileError, OSError) as exc:
        raise ToolError(str(exc)) from exc


def _mcp_deadline_seconds() -> float:
    raw = os.environ.get("SOURCE_SCOUT_MCP_DEADLINE_SECONDS")
    if not raw:
        return DEFAULT_MCP_DEADLINE_SECONDS
    try:
        value = float(raw)
        return max(0.01, value) if math.isfinite(value) else DEFAULT_MCP_DEADLINE_SECONDS
    except ValueError:
        return DEFAULT_MCP_DEADLINE_SECONDS


async def _with_mcp_deadline(awaitable: Awaitable[_T]) -> _T:
    async with asyncio.timeout(_mcp_deadline_seconds()):
        return await awaitable


def _structured_tool_error(exc: Exception, *, stage: str) -> ToolError:
    return ToolError(failure_from_exception(exc, stage=stage).to_json())


@reuse_mcp.tool(
    description="Read-only health and optional smoke checks for the configured Source Scout model.",
    annotations={"readOnlyHint": True},
)
async def model_status(
    smoke_test: Annotated[
        bool,
        Field(description="Run assessment and exploration smoke requests in addition to model listing"),
    ] = False,
) -> dict[str, object]:
    try:
        return await _with_mcp_deadline(_api_status(smoke_test))
    except TimeoutError as exc:
        failure = failure_from_exception(exc, stage="model_status")
        return _error_status(deepseek.get_config(), failure)


@mcp.tool(
    description=(
        "Read-only investigation with DeepSeek of unresolved cross-file contracts, indirect runtime "
        "flow, ambiguous ownership or concrete architecture relations AFTER rg/direct reads or Serena "
        "navigation. Not general code search, symbol lookup, review or test verification. Selective "
        "policy requires an approved use_case, concrete reason and attempted_local_methods. "
        "Returns bounded source citations with explicit missing context; full journal stays local."
    ),
    annotations={"readOnlyHint": True},
)
async def explore_local_code(
    task: Annotated[
        str,
        Field(description="Natural language coding task or investigation goal"),
    ],
    project_path: Annotated[
        str,
        Field(description="Absolute or relative path to the local project root to explore"),
    ],
    max_turns: Annotated[
        int,
        Field(description="Maximum FastContext exploration turns", ge=1, le=12),
    ] = fastcontext.DEFAULT_MAX_TURNS,
    reason: Annotated[str, Field(description="Concrete reason rg/Serena did not suffice")] = "",
    use_case: Annotated[
        ExplorationUseCase | None,
        Field(description=(
            "Required in selective mode: cross_file_contract = known definition/reference chain but "
            "unresolved inter-file contract; indirect_runtime_flow = callbacks/registration/DI/events "
            "defeat direct navigation; ambiguous_ownership = local search found multiple plausible "
            "owners; architecture_trace = concrete relation unresolved across several subsystems."
        )),
    ] = None,
    attempted_local_methods: Annotated[
        list[LocalMethod] | None,
        Field(description="Local methods already attempted; at least one required in selective mode"),
    ] = None,
) -> LocalExploreResult:
    if not task.strip():
        raise _structured_tool_error(ValueError("Task description is required."), stage="validation")
    if not project_path.strip():
        raise _structured_tool_error(ValueError("project_path is required."), stage="validation")
    try:
        return await _with_mcp_deadline(
            fastcontext.explore_local_project(
                task=task,
                project_path=project_path,
                max_turns=max_turns,
                reason=reason,
                use_case=use_case,
                attempted_local_methods=attempted_local_methods,
                deadline_seconds=max(0.001, _mcp_deadline_seconds() - 5.0),
            )
        )
    except (fastcontext.FastContextError, deepseek.ModelError, OSError, TimeoutError) as exc:
        raise _structured_tool_error(exc, stage="exploration") from exc


@reuse_mcp.tool(
    description=(
        "Assess a reusable-code candidate for a task. May write local assessment cache and analysis "
        "run metadata in the Source Scout catalog."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def assess_reusable_code(
    candidate_id: Annotated[
        str,
        Field(description="Candidate id returned by find_reusable_code"),
    ],
    task: Annotated[
        str,
        Field(description="Natural language reuse task to assess the candidate against"),
    ],
    fastcontext_policy: Annotated[
        Literal["auto", "always", "never"],
        Field(description="One of: auto, always, never"),
    ] = "auto",
    max_evidence_rounds: Annotated[
        int,
        Field(description="Maximum focused FastContext evidence rounds", ge=0, le=2),
    ] = 1,
    force: Annotated[
        bool,
        Field(description="Bypass cached assessments and force a fresh assessment"),
    ] = False,
    project_path: Annotated[
        str | None,
        Field(description="Optional local target project path for deterministic compatibility profiling"),
    ] = None,
) -> dict[str, Any]:
    from . import assessor

    if not candidate_id.strip():
        raise ToolError("candidate_id is required.")
    if not task.strip():
        raise ToolError("Task description is required.")
    if fastcontext_policy not in {"auto", "always", "never"}:
        raise ToolError("fastcontext_policy must be one of: auto, always, never.")
    if max_evidence_rounds < 0 or max_evidence_rounds > 2:
        raise ToolError("max_evidence_rounds must be between 0 and 2.")

    try:
        result = await assessor.assess_candidate(
            candidate_id=candidate_id,
            task=task,
            fastcontext_policy=fastcontext_policy,
            max_evidence_rounds=max_evidence_rounds,
            force=force,
            project_path=project_path,
        )
    except (assessor.AssessorError, deepseek.ModelError, OSError, ValueError) as exc:
        if isinstance(exc, assessor.AssessorError):
            raise ToolError(
                FailureDetails("validation", "assessment", str(exc), retryable=False).to_json()
            ) from exc
        raise _structured_tool_error(exc, stage="assessment") from exc
    return assessor.assessment_to_jsonable(result)


@reuse_mcp.tool(
    description=(
        "Find reusable code candidates from the local Source Scout catalog. Records a local "
        "'returned' reuse outcome for candidates it returns."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def find_reusable_code(
    task: Annotated[
        str,
        Field(description="Natural language reuse task, e.g. 'Next.js data table for admin dashboard'"),
    ],
    project_path: Annotated[
        str | None,
        Field(description="Optional local target project path for deterministic compatibility profiling"),
    ] = None,
    max_repos: Annotated[
        int,
        Field(description="Maximum number of reusable code candidates to return", ge=1, le=5),
    ] = 3,
) -> FindReusableCodeResult:
    from . import catalog_assessments, catalog_search

    if not task.strip():
        raise ToolError("Task description is required.")
    try:
        profile = build_target_profile(project_path) if project_path and project_path.strip() else None
    except TargetProfileError as exc:
        raise ToolError(str(exc)) from exc

    results = catalog_search.search_assets(task, max_repos, target_profile=profile)
    profile_fingerprint = profile.fingerprint if profile is not None else ""
    signature = catalog_assessments.task_signature(task, profile_fingerprint)
    for result in results:
        result.task_signature = signature
    for result in results:
        catalog_assessments.record_reuse_outcome(
            asset_id=result.candidate_id,
            repo_id=result.repo_id,
            task_signature=signature,
            outcome="returned",
        )

    next_steps = []
    if not results:
        next_steps.append(
            "Run source-scout scout --domain personal-code, qualify, then evidence --domain personal-code."
        )
    else:
        next_steps.append(
            "Call assess_reusable_code(candidate_id, task) for the strongest candidates, using the "
            "same project_path when one was provided. Then call get_source_bundle(assessment_id) "
            "for a select or inspect assessment."
        )

    return FindReusableCodeResult(
        task=task,
        task_signature=signature,
        total_candidates=len(results),
        results=results,
        timestamp=_now_iso(),
        next_steps=next_steps,
        target_profile_fingerprint=profile_fingerprint,
    )


@reuse_mcp.tool(
    description=(
        "Create a task-specific local source bundle for a candidate and record an 'opened_bundle' "
        "reuse outcome in the local Source Scout catalog."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def get_source_bundle(
    assessment_id: Annotated[
        str,
        Field(description="Assessment id returned by assess_reusable_code"),
    ],
) -> SourceBundleResult:
    from . import bundles, catalog_assessments

    if not assessment_id.strip():
        raise ToolError("assessment_id is required.")
    result = bundles.create_source_bundle(assessment_id)
    catalog_assessments.record_reuse_outcome(
        asset_id=result.candidate_id,
        repo_id=result.repo_id,
        task_signature=result.task_signature,
        outcome="opened_bundle",
    )
    return result


@reuse_mcp.tool(
    description="Record a local reuse outcome for a candidate and task signature.",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def record_reuse_outcome(
    candidate_id: Annotated[
        str,
        Field(description="Candidate id returned by find_reusable_code"),
    ],
    task_signature: Annotated[
        str,
        Field(description="Task signature returned by find_reusable_code"),
    ],
    outcome: Annotated[
        str,
        Field(
            description=(
                "One of: returned, opened_bundle, selected, integrated_successfully, "
                "rejected_irrelevant, rejected_too_coupled, rejected_low_quality"
            ),
        ),
    ],
    notes: Annotated[
        str | None,
        Field(description="Optional notes about why the candidate succeeded or failed"),
    ] = None,
) -> RecordReuseOutcomeResult:
    from . import catalog_assessments, catalog_assets

    if not task_signature.strip():
        raise ToolError("task_signature is required.")
    asset = catalog_assets.get_asset_detail(candidate_id)
    if asset is None:
        raise ToolError(f"Unknown candidate_id: {candidate_id}")
    try:
        catalog_assessments.record_reuse_outcome(
            asset_id=candidate_id,
            repo_id=str(asset["repo_id"]),
            task_signature=task_signature,
            outcome=outcome,
            notes=notes,
        )
    except ValueError as exc:
        raise ToolError(str(exc))
    return RecordReuseOutcomeResult(
        candidate_id=candidate_id,
        task_signature=task_signature,
        outcome=outcome,
        recorded=True,
        timestamp=_now_iso(),
    )
