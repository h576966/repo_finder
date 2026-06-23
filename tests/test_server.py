import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from source_scout import catalog, server
from source_scout.models import LocalExploreResult, RateLimitError
from source_scout.server import _build_search_query, _format_error, _parse_url


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(tmp_path / ".source_scout"))
    catalog.reset_connection()
    yield
    catalog.reset_connection()


def test_parse_url_slug():
    owner, repo = _parse_url("psf/requests")
    assert owner == "psf"
    assert repo == "requests"


def test_parse_url_full_github():
    owner, repo = _parse_url("https://github.com/psf/requests")
    assert owner == "psf"
    assert repo == "requests"


def test_parse_url_with_trailing_slash():
    owner, repo = _parse_url("psf/requests/")
    assert owner == "psf"
    assert repo == "requests"


def test_parse_url_invalid():
    with pytest.raises(ToolError, match="Invalid repo reference"):
        _parse_url("not-a-repo")


def test_parse_url_empty():
    with pytest.raises(ToolError):
        _parse_url("")


def test_parse_url_single_word():
    with pytest.raises(ToolError):
        _parse_url("justone")


def test_build_search_query_basic():
    query = _build_search_query("fastapi backend", None, None, None, None)
    assert "fastapi backend" in query
    assert "archived:false" in query


def test_build_search_query_with_language():
    query = _build_search_query("task", "Python", None, None, None)
    assert "language:Python" in query


def test_build_search_query_with_min_stars():
    query = _build_search_query("task", None, 100, None, None)
    assert "stars:>=100" in query


def test_build_search_query_with_license():
    query = _build_search_query("task", None, None, None, "mit")
    assert "license:mit" in query


def test_build_search_query_with_max_age_days():
    query = _build_search_query("task", None, None, 30, None)
    assert "pushed:>=" in query


def test_build_search_query_all_filters():
    query = _build_search_query("api", "TypeScript", 50, 90, "apache-2.0")
    assert "api" in query
    assert "language:TypeScript" in query
    assert "stars:>=50" in query
    assert "license:apache-2.0" in query
    assert "pushed:>=" in query
    assert "archived:false" in query


def test_format_error_rate_limit():
    exc = RateLimitError("Rate limited", retry_after=30)
    result = _format_error(exc)
    parsed = json.loads(result)
    assert parsed["error"] == "Rate limited"
    assert parsed["recoverable"] is True
    assert parsed["retry_after"] == 30


def test_format_error_generic():
    exc = RuntimeError("Something went wrong")
    result = _format_error(exc)
    parsed = json.loads(result)
    assert parsed["error"] == "Something went wrong"
    assert parsed["recoverable"] is False
    assert parsed["retry_after"] is None


@pytest.mark.asyncio
async def test_explore_local_code_tool_is_read_only_and_ephemeral(monkeypatch, tmp_path: Path) -> None:
    async def fake_explore_local_project(
        task: str,
        project_path: str,
        max_turns: int = 6,
    ) -> LocalExploreResult:
        assert task == "Find MCP tools"
        assert project_path == str(tmp_path)
        assert max_turns == 2
        return LocalExploreResult(
            task=task,
            project_path=str(tmp_path),
            model_id="fastcontext-1.0-4b-rl",
            prompt_version="fastcontext-refine-v1",
            schema_version="fastcontext-evidence-v1",
            analyzer_version="fastcontext-harness-v1",
            status="completed",
            evidence_paths=["src/source_scout/server.py:1-20"],
            notes=["MCP tools are registered here."],
            tool_trace=[],
        )

    monkeypatch.setattr(server.fastcontext, "explore_local_project", fake_explore_local_project)

    tools = await server.mcp.get_tools()
    assert "explore_local_code" in tools
    assert tools["explore_local_code"].annotations.readOnlyHint is True

    result = await server.explore_local_code.fn(
        "Find MCP tools",
        str(tmp_path),
        max_turns=2,
    )

    assert result.evidence_paths == ["src/source_scout/server.py:1-20"]
    assert catalog.get_connection().execute("SELECT COUNT(*) FROM reuse_outcomes").fetchone()[0] == 0
    assert catalog.get_connection().execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_assess_reusable_code_tool_is_registered_read_only_and_returns_cli_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class Result:
        candidate_id = "asset-1"
        final_verdict = "select"
        reuse_score = 0.91

    async def fake_assess_candidate(**kwargs: Any) -> Result:
        calls.append(kwargs)
        return Result()

    monkeypatch.setattr(server.assessor, "assess_candidate", fake_assess_candidate)
    monkeypatch.setattr(
        server.assessor,
        "assessment_to_jsonable",
        lambda result: {
            "candidate_id": result.candidate_id,
            "final_verdict": result.final_verdict,
            "reuse_score": result.reuse_score,
        },
    )

    tools = await server.mcp.get_tools()
    assert "assess_reusable_code" in tools
    assert tools["assess_reusable_code"].annotations.readOnlyHint is True

    result = await server.assess_reusable_code.fn(
        "asset-1",
        "Find a reusable route handler",
        fastcontext_policy="never",
        max_evidence_rounds=0,
        force=True,
    )

    assert result == {
        "candidate_id": "asset-1",
        "final_verdict": "select",
        "reuse_score": 0.91,
    }
    assert calls == [
        {
            "candidate_id": "asset-1",
            "task": "Find a reusable route handler",
            "fastcontext_policy": "never",
            "max_evidence_rounds": 0,
            "force": True,
        }
    ]


@pytest.mark.asyncio
async def test_assess_reusable_code_validates_task_policy_and_round_limit() -> None:
    with pytest.raises(ToolError, match="Task description is required"):
        await server.assess_reusable_code.fn("asset-1", "")

    with pytest.raises(ToolError, match="fastcontext_policy must be one of"):
        await server.assess_reusable_code.fn(
            "asset-1",
            "Find reusable code",
            fastcontext_policy="sometimes",
        )

    with pytest.raises(ToolError, match="max_evidence_rounds must be between 0 and 2"):
        await server.assess_reusable_code.fn(
            "asset-1",
            "Find reusable code",
            max_evidence_rounds=3,
        )


@pytest.mark.asyncio
async def test_assess_reusable_code_converts_assessor_errors_to_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_assess_candidate(**kwargs: Any) -> object:
        raise server.assessor.AssessorError("Unknown candidate_id: missing")

    monkeypatch.setattr(server.assessor, "assess_candidate", fake_assess_candidate)

    with pytest.raises(ToolError, match="Unknown candidate_id"):
        await server.assess_reusable_code.fn("missing", "Find reusable code")


def _create_reusable_asset(tmp_path: Path) -> str:
    snapshot_root = tmp_path / "snapshot"
    (snapshot_root / "components").mkdir(parents=True)
    (snapshot_root / "components" / "data-table.tsx").write_text(
        "export function DataTable() { return <table /> }",
        encoding="utf-8",
    )
    repo_id = catalog.upsert_repository(
        {
            "owner": {"login": "owner"},
            "name": "repo",
            "full_name": "owner/repo",
            "html_url": "https://github.com/owner/repo",
            "private": False,
            "archived": False,
            "mirror_url": None,
            "fork": False,
            "is_template": False,
            "language": "TypeScript",
            "size": 10,
            "created_at": "2026-01-15T00:00:00Z",
            "pushed_at": "2026-06-20T12:00:00Z",
            "topics": ["nextjs"],
        },
        "test",
    )
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
    return catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "data-table",
        {
            "entry_paths": ["components/data-table.tsx"],
            "dependency_paths": [],
            "external_dependencies": ["@tanstack/react-table"],
            "evidence_paths": ["components/data-table.tsx:1-1"],
            "reuse_score": 1.0,
            "synthesis": {
                "adaptation_notes": ["Copy the component and wire columns locally."],
                "ui_path_score": 1.0,
                "noise_penalty": 0.0,
                "capability_path_score": 1.0,
            },
        },
    )


@pytest.mark.asyncio
async def test_reuse_tools_carry_task_signature_and_record_outcomes(tmp_path: Path) -> None:
    asset_id = _create_reusable_asset(tmp_path)

    result = await server.find_reusable_code.fn("Find a reusable data table", max_repos=1)

    assert result.task_signature == catalog.task_signature("Find a reusable data table")
    assert result.results[0].candidate_id == asset_id
    assert result.results[0].task_signature == result.task_signature
    assert "get_source_bundle(candidate_id, task_signature)" in result.next_steps[0]

    bundle = await server.get_source_bundle.fn(asset_id, result.task_signature)
    manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
    assert bundle.task_signature == result.task_signature
    assert manifest["task_signature"] == result.task_signature

    recorded = await server.record_reuse_outcome.fn(
        asset_id,
        result.task_signature,
        "selected",
        notes="usable",
    )
    assert recorded.task_signature == result.task_signature
    assert recorded.recorded is True

    rows = catalog.get_connection().execute(
        """
        SELECT task_signature, outcome, notes
        FROM reuse_outcomes
        WHERE asset_id = ?
        ORDER BY recorded_at
        """,
        [asset_id],
    ).fetchall()
    assert (result.task_signature, "returned", None) in rows
    assert (result.task_signature, "opened_bundle", None) in rows
    assert (result.task_signature, "selected", "usable") in rows


@pytest.mark.asyncio
async def test_reuse_tools_require_task_signature(tmp_path: Path) -> None:
    asset_id = _create_reusable_asset(tmp_path)

    with pytest.raises(ToolError, match="task_signature is required"):
        await server.get_source_bundle.fn(asset_id, "")

    with pytest.raises(ToolError, match="task_signature is required"):
        await server.record_reuse_outcome.fn(asset_id, "", "selected")
