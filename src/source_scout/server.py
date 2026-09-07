"""Three focused operations; no catalog workflow or tool synonyms on normal MCP."""

import asyncio
import math
import os
from collections.abc import Awaitable
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .exploration_policy import ExplorationUseCase, LocalMethod
from .failures import failure_from_exception
from .fastcontext_constants import DEFAULT_MAX_TURNS
from .investigation_anchors import InvestigationAnchor
from .models import LocalExploreResult

INSTRUCTIONS = (
    "Codex owns reasoning, edits and verification. Use rg/direct reads and Serena directly. "
    "Use investigate_code for a concrete unresolved source relation after local navigation. "
    "Implementation References searches only explicitly selected personal/curated sources. "
    "Citations are navigation; read the source and assess it yourself. No mandatory tool chain. "
    "After using a result, Codex records a brief assessment with the local CLI: "
    "source-scout feedback --report <usage.report_path> "
    "--outcome helped|partly_helped|did_not_help|unassessed "
    "--observation <what helped or was missing> [--evidence <source/test evidence>]. "
    "Do not infer usefulness from completed status alone or ask the user to supply routine feedback."
)
investigator_mcp = FastMCP("SourceScoutInvestigation", instructions=INSTRUCTIONS)
reference_mcp = FastMCP("SourceScoutReferences", instructions=INSTRUCTIONS)
DEFAULT_MCP_DEADLINE_SECONDS = 270.0
INVESTIGATOR_MCP_TOOL_NAMES = ("investigate_code",)
REFERENCE_MCP_TOOL_NAMES = ("find_implementation_references", "get_implementation_reference")
DEFAULT_MCP_TOOL_NAMES = (*INVESTIGATOR_MCP_TOOL_NAMES, *REFERENCE_MCP_TOOL_NAMES)


def create_server(profile: str = "default") -> FastMCP:
    if profile in {"investigator", "sidecar"}:
        return investigator_mcp
    if profile == "references":
        return reference_mcp
    if profile != "default":
        raise ValueError("The reuse profile is retired. Use default, investigator or references.")
    server = FastMCP("SourceScout", instructions=INSTRUCTIONS)
    server.mount(investigator_mcp)
    server.mount(reference_mcp)
    return server


@reference_mcp.tool(
    description="Find up to three Implementation References with source evidence in selected repositories.",
    annotations={"readOnlyHint": True},
)
def find_implementation_references(
    task: Annotated[str, Field(min_length=1, max_length=2000)],
    target_project_path: str | None = None,
    max_results: Annotated[int, Field(ge=1, le=3)] = 3,
) -> dict[str, Any]:
    from .implementation_references import find_implementation_references, reference_to_jsonable

    try:
        return reference_to_jsonable(
            find_implementation_references(
                task, target_project_path=target_project_path, max_results=max_results
            )
        )
    except (ValueError, OSError) as exc:
        raise _structured_tool_error(exc, stage="references") from exc


@reference_mcp.tool(
    description="Read bounded whole-line context and Git-blob provenance for one Implementation Reference.",
    annotations={"readOnlyHint": True},
)
def get_implementation_reference(
    reference_id: Annotated[str, Field(min_length=1, max_length=128)],
    task: Annotated[str, Field(max_length=2000)] = "",
    target_project_path: str | None = None,
) -> dict[str, Any]:
    from .implementation_references import get_implementation_reference, reference_to_jsonable

    try:
        return reference_to_jsonable(
            get_implementation_reference(reference_id, task=task, target_project_path=target_project_path)
        )
    except (ValueError, OSError) as exc:
        raise _structured_tool_error(exc, stage="references") from exc


def _mcp_deadline_seconds() -> float:
    raw = os.environ.get("SOURCE_SCOUT_MCP_DEADLINE_SECONDS")
    if not raw:
        return DEFAULT_MCP_DEADLINE_SECONDS
    try:
        value = float(raw)
        return max(0.01, value) if math.isfinite(value) else DEFAULT_MCP_DEADLINE_SECONDS
    except ValueError:
        return DEFAULT_MCP_DEADLINE_SECONDS


async def _with_mcp_deadline[T](awaitable: Awaitable[T]) -> T:
    async with asyncio.timeout(_mcp_deadline_seconds()):
        return await awaitable


def _structured_tool_error(exc: Exception, *, stage: str) -> ToolError:
    return ToolError(failure_from_exception(exc, stage=stage).to_json())


@investigator_mcp.tool(
    description=(
        "Read-only investigation with DeepSeek of unresolved cross-file contracts, indirect runtime "
        "flow, ambiguous ownership or concrete architecture relations AFTER rg/direct reads or Serena "
        "navigation. Not general code search, symbol lookup, review or test verification. Selective "
        "policy requires an approved use_case, concrete reason and attempted_local_methods. "
        "Returns bounded source citations with explicit missing context; full journal stays local."
    ),
    annotations={"readOnlyHint": True},
)
async def investigate_code(
    task: Annotated[
        str,
        Field(description="Natural language coding task or investigation goal"),
    ],
    source_root: Annotated[
        str,
        Field(description="Absolute or relative path to the local project root to explore"),
    ],
    max_turns: Annotated[
        int,
        Field(description="Maximum FastContext exploration turns", ge=1, le=12),
    ] = DEFAULT_MAX_TURNS,
    reason: Annotated[str, Field(description="Concrete reason rg/Serena did not suffice")] = "",
    use_case: Annotated[
        ExplorationUseCase | None,
        Field(
            description=(
                "Required in selective mode: cross_file_contract = known definition/reference chain but "
                "unresolved inter-file contract; indirect_runtime_flow = callbacks/registration/DI/events "
                "defeat direct navigation; ambiguous_ownership = local search found multiple plausible "
                "owners; architecture_trace = concrete relation unresolved across several subsystems."
            )
        ),
    ] = None,
    attempted_local_methods: Annotated[
        list[LocalMethod] | None,
        Field(description="Local methods already attempted; at least one required in selective mode"),
    ] = None,
    anchors: Annotated[list[InvestigationAnchor] | None, Field(max_length=6)] = None,
) -> LocalExploreResult:
    from . import deepseek, fastcontext

    if not task.strip():
        raise _structured_tool_error(ValueError("Task description is required."), stage="validation")
    if not source_root.strip():
        raise _structured_tool_error(ValueError("source_root is required."), stage="validation")
    try:
        return await _with_mcp_deadline(
            fastcontext.explore_local_project(
                task=task,
                project_path=source_root,
                max_turns=max_turns,
                reason=reason,
                use_case=use_case,
                attempted_local_methods=attempted_local_methods,
                anchors=anchors,
                deadline_seconds=max(0.001, _mcp_deadline_seconds() - 5.0),
            )
        )
    except (fastcontext.FastContextError, deepseek.ModelError, ValueError, OSError, TimeoutError) as exc:
        raise _structured_tool_error(exc, stage="exploration") from exc


mcp = create_server()
