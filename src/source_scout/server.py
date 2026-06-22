import json
import os
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from . import bundles, cache, catalog, fastcontext, lmstudio, pattern_extractor, ranker, repo_inspector
from .constants import _now_iso
from .github_client import get_client
from .models import (
    CompareItem,
    CompareResult,
    DeepPatternReport,
    FindReposResult,
    FindReusableCodeResult,
    InspectionResult,
    LocalExploreResult,
    PatternReport,
    QualityReport,
    RateLimitError,
    RecordReuseOutcomeResult,
    RepoStructure,
    RepoSummary,
    SourceBundleResult,
)
from .urls import parse_owner_repo

mcp = FastMCP("RepoFinder")


def _parse_url(url_or_slug: str) -> tuple[str, str]:
    parsed = parse_owner_repo(url_or_slug)
    if parsed is None or not parsed[0] or not parsed[1]:
        raise ToolError(
            f"Invalid repo reference: '{url_or_slug}'. "
            "Use 'owner/repo' or a full GitHub URL."
        )
    return parsed


def _build_search_query(
    task: str,
    language: str | None,
    min_stars: int | None,
    max_age_days: int | None,
    license_filter: str | None,
) -> str:
    parts = [task.strip()]
    if language:
        parts.append(f"language:{language.strip()}")
    if min_stars is not None:
        parts.append(f"stars:>={min_stars}")
    if license_filter:
        parts.append(f"license:{license_filter.strip()}")
    if max_age_days is not None:
        cutoff = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = cutoff - timedelta(days=max_age_days)
        parts.append(f"pushed:>={cutoff.strftime('%Y-%m-%d')}")
    if "archived:" not in " ".join(parts):
        parts.append("archived:false")
    return " ".join(parts)


def _format_error(exc: Exception) -> str:
    if isinstance(exc, RateLimitError):
        return json.dumps({
            "error": str(exc),
            "recoverable": True,
            "retry_after": exc.retry_after,
        })
    return json.dumps({
        "error": str(exc),
        "recoverable": False,
        "retry_after": None,
    })


def _legacy_tools_enabled() -> bool:
    return os.environ.get("SOURCE_SCOUT_ENABLE_LEGACY_TOOLS") == "1"


@mcp.tool(
    annotations={"readOnlyHint": True},
)
async def find_repos_for_task(
    task: Annotated[
        str,
        Field(description="Natural language task description, e.g., 'lightweight FastAPI backend'"),
    ],
    language: Annotated[
        str | None,
        Field(description="Programming language, e.g., Python, TypeScript, Rust"),
    ] = None,
    min_stars: Annotated[int | None, Field(description="Minimum star count", ge=0)] = None,
    max_age_days: Annotated[int | None, Field(description="Max days since last push", ge=1)] = None,
    license_filter: Annotated[
        str | None,
        Field(description="License SPDX identifier, e.g., mit, apache-2.0"),
    ] = None,
    limit: Annotated [
        int, Field(description="Number of results to return (1-10)", ge=1, le=10)
    ] = 10,
) -> FindReposResult:
    if not task.strip():
        raise ToolError("Task description is required.")

    query = _build_search_query(task, language, min_stars, max_age_days, license_filter)
    sort = "stars" if min_stars else "updated"
    cache_key = f"search:{ranker._hash_query(query)}:{sort}"

    cached_result = cache.cache_get(cache_key)
    if cached_result:
        return FindReposResult(
            query=task,
            total_candidates_scored=int(cached_result["total_candidates_scored"]),
            results=[
                RepoSummary(**r) for r in cached_result["results"]
            ],
            cached=True,
            timestamp=str(cached_result["timestamp"]),
        )

    client = get_client()
    try:
        raw_repos = await client.search_repos(query, per_page=30, sort=sort)
    except Exception as exc:
        raise RuntimeError(_format_error(exc))

    if not raw_repos:
        empty_result = FindReposResult(
            query=task,
            total_candidates_scored=0,
            results=[],
            cached=False,
            timestamp=_now_iso(),
        )
        return empty_result

    summaries = ranker.build_repo_summaries(raw_repos, task)
    top = summaries[:limit]

    result = FindReposResult(
        query=task,
        total_candidates_scored=len(raw_repos),
        results=top,
        cached=False,
        timestamp=_now_iso(),
    )

    cache.cache_set(cache_key, {
        "total_candidates_scored": result.total_candidates_scored,
        "results": [{
            "full_name": s.full_name,
            "html_url": s.html_url,
            "description": s.description,
            "language": s.language,
            "stars": s.stars,
            "last_push": s.last_push,
            "score": s.score,
            "verdict": s.verdict,
            "risks": s.risks,
        } for s in result.results],
        "timestamp": result.timestamp,
    }, cache.get_ttl("search"))

    return result


@mcp.tool(
    annotations={"readOnlyHint": True},
)
async def inspect_github_repo(
    repo_url: Annotated[
        str,
        Field(description="GitHub repo URL or 'owner/repo' string"),
    ],
) -> InspectionResult:
    owner, repo = _parse_url(repo_url)
    cache_key = f"repo:{owner}/{repo}"

    cached_result = cache.cache_get(cache_key)
    if cached_result:
        # Rebuild nested dataclasses from cached dicts
        cached_result["structure"] = RepoStructure(
            **cached_result.get("structure", {})
        )
        cached_result["quality"] = QualityReport(
            **cached_result.get("quality", {})
        )
        cached_result["cached"] = True
        return InspectionResult(**cached_result)

    try:
        result = await repo_inspector.inspect_repo(owner, repo)
    except Exception as exc:
        raise RuntimeError(_format_error(exc))

    cache.cache_set(cache_key, {
        "owner": result.owner,
        "repo": result.repo,
        "description": result.description,
        "language": result.language,
        "stars": result.stars,
        "forks": result.forks,
        "open_issues": result.open_issues,
        "license_name": result.license_name,
        "last_push": result.last_push,
        "archived": result.archived,
        "structure": result.structure,
        "quality": result.quality,
        "readme_preview": result.readme_preview,
        "verdict": result.verdict,
        "verdict_reasoning": result.verdict_reasoning,
        "cached": result.cached,
        "timestamp": result.timestamp,
    }, cache.get_ttl("metadata"))

    return result


@mcp.tool(
    annotations={"readOnlyHint": True},
)
async def compare_github_repos(
    repos: Annotated[
        list[str],
        Field(
            description="List of 2-5 GitHub repo URLs or 'owner/repo' strings",
            min_length=2,
            max_length=5,
        ),
    ],
) -> CompareResult:
    if len(repos) < 2 or len(repos) > 5:
        raise ToolError("Provide 2–5 repositories to compare.")

    import asyncio

    async def inspect_one(url: str) -> InspectionResult:
        owner, repo = _parse_url(url)
        return await repo_inspector.inspect_repo(owner, repo)

    try:
        results = await asyncio.gather(*[inspect_one(r) for r in repos])
    except Exception as exc:
        raise RuntimeError(_format_error(exc))

    items: list[CompareItem] = []
    best_score = -1.0
    best_name = ""

    for r in results:
        activity = repo_inspector._evaluate_activity(r.last_push)
        items.append(
            CompareItem(
                full_name=f"{r.owner}/{r.repo}",
                stars=r.stars,
                activity=activity,
                quality_score=r.quality.score,
                license_name=r.license_name,
                verdict=r.verdict,
            )
        )
        score = r.quality.score
        if r.archived:
            score *= 0.5
        if score > best_score:
            best_score = score
            best_name = f"{r.owner}/{r.repo}"

    reasoning = (
        f"Recommended {best_name} "
        f"based on quality score ({best_score:.2f}) and repository health."
    )
    return CompareResult(
        repos=items,
        recommended=best_name,
        reasoning=reasoning,
        cached=False,
        timestamp=_now_iso(),
    )


@mcp.tool(
    annotations={"readOnlyHint": True},
)
async def extract_patterns_from_repo(
    repo_url: Annotated[
        str,
        Field(description="GitHub repo URL or 'owner/repo' string"),
    ],
    focus: Annotated[
        str | None,
        Field(description="Focus area, e.g., 'API design', 'auth', 'data pipeline'"),
    ] = None,
) -> PatternReport:
    owner, repo = _parse_url(repo_url)

    try:
        result = await pattern_extractor.extract_patterns(owner, repo, focus)
    except Exception as exc:
        raise RuntimeError(_format_error(exc))

    return result


@mcp.tool(
    annotations={"readOnlyHint": True},
)
async def deep_inspect_repo(
    repo_url: Annotated[
        str,
        Field(description="GitHub repo URL or 'owner/repo' string"),
    ],
    focus: Annotated[
        str | None,
        Field(description="Focus area, e.g., 'API design', 'auth'"),
    ] = None,
) -> DeepPatternReport:
    owner, repo = _parse_url(repo_url)

    try:
        result = await pattern_extractor.extract_patterns_deep(owner, repo, focus)
    except Exception as exc:
        raise RuntimeError(_format_error(exc))

    return result


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
    ] = 6,
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


if not _legacy_tools_enabled():
    for _tool_name in (
        "find_repos_for_task",
        "inspect_github_repo",
        "compare_github_repos",
        "extract_patterns_from_repo",
        "deep_inspect_repo",
    ):
        mcp.remove_tool(_tool_name)
