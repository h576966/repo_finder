from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from . import (
    assessor,
    bundles,
    catalog,
    fastcontext,
    lmstudio,
)
from .constants import _now_iso
from .models import (
    FindReusableCodeResult,
    LocalExploreResult,
    RecordReuseOutcomeResult,
    SourceBundleResult,
)

mcp = FastMCP("SourceScout")

DEFAULT_MCP_TOOL_NAMES = (
    "find_reusable_code",
    "assess_reusable_code",
    "get_source_bundle",
    "record_reuse_outcome",
    "explore_local_code",
)


@mcp.tool(
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
) -> LocalExploreResult:
    if not task.strip():
        raise ToolError("Task description is required.")
    if not project_path.strip():
        raise ToolError("project_path is required.")
    try:
        return await fastcontext.explore_local_project(
            task=task,
            project_path=project_path,
            max_turns=max_turns,
        )
    except (fastcontext.FastContextError, lmstudio.LMStudioError, OSError) as exc:
        raise ToolError(str(exc))


@mcp.tool()
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
) -> dict[str, Any]:
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
        )
    except (assessor.AssessorError, lmstudio.LMStudioError, OSError, ValueError) as exc:
        raise ToolError(str(exc))
    return assessor.assessment_to_jsonable(result)


@mcp.tool()
async def find_reusable_code(
    task: Annotated[
        str,
        Field(description="Natural language UI reuse task, e.g. 'Next.js data table for admin dashboard'"),
    ],
    project_path: Annotated[
        str | None,
        Field(description="Optional local target project path for future project profiling"),
    ] = None,
    max_repos: Annotated[
        int,
        Field(description="Maximum number of reusable code candidates to return", ge=1, le=5),
    ] = 3,
) -> FindReusableCodeResult:
    if not task.strip():
        raise ToolError("Task description is required.")
    if project_path:
        # Reserved for later project profiling; accepted now so the MCP contract is stable.
        _ = project_path

    results = catalog.search_assets(task, max_repos)
    signature = catalog.task_signature(task)
    for result in results:
        result.task_signature = signature
    for result in results:
        catalog.record_reuse_outcome(
            asset_id=result.candidate_id,
            repo_id=result.repo_id,
            task_signature=signature,
            outcome="returned",
        )

    next_steps = []
    if not results:
        next_steps.append(
            "Run source-scout scout --domain nextjs-ui, qualify, then evidence for the desired capability."
        )
    else:
        next_steps.append(
            "Call get_source_bundle(candidate_id, task_signature) for the most relevant candidate."
        )

    return FindReusableCodeResult(
        task=task,
        task_signature=signature,
        total_candidates=len(results),
        results=results,
        timestamp=_now_iso(),
        next_steps=next_steps,
    )


@mcp.tool()
async def get_source_bundle(
    candidate_id: Annotated[
        str,
        Field(description="Candidate id returned by find_reusable_code"),
    ],
    task_signature: Annotated[
        str,
        Field(description="Task signature returned by find_reusable_code"),
    ],
) -> SourceBundleResult:
    if not task_signature.strip():
        raise ToolError("task_signature is required.")
    result = bundles.create_source_bundle(candidate_id, task_signature)
    catalog.record_reuse_outcome(
        asset_id=candidate_id,
        repo_id=result.repo_id,
        task_signature=task_signature,
        outcome="opened_bundle",
    )
    return result


@mcp.tool()
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
    if not task_signature.strip():
        raise ToolError("task_signature is required.")
    asset = catalog.get_asset_detail(candidate_id)
    if asset is None:
        raise ToolError(f"Unknown candidate_id: {candidate_id}")
    try:
        catalog.record_reuse_outcome(
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
