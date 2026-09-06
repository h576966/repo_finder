import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from source_scout import deepseek, fastcontext, server
from source_scout.models import LocalExploreResult


@pytest.mark.asyncio
async def test_explore_local_code_tool_is_read_only_and_ephemeral(monkeypatch, tmp_path: Path) -> None:
    async def fake_explore_local_project(
        task: str,
        project_path: str,
        max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
        reason: str = "",
        deadline_seconds: float | None = None,
        use_case=None,
        attempted_local_methods=None,
        anchors=None,
    ) -> LocalExploreResult:
        assert task == "Find MCP tools"
        assert project_path == str(tmp_path)
        assert max_turns == 2
        assert use_case == "cross_file_contract"
        assert attempted_local_methods == ["rg"]
        return LocalExploreResult(
            task=task,
            project_path=str(tmp_path),
            model_id="deepseek-v4-flash",
            prompt_version="fastcontext-refine-v2",
            schema_version="fastcontext-evidence-v1",
            analyzer_version="fastcontext-harness-v1",
            status="completed",
            evidence_paths=["src/source_scout/server.py:1-20"],
            notes=["MCP tools are registered here."],
            tool_trace=[],
        )

    monkeypatch.setattr(fastcontext, "explore_local_project", fake_explore_local_project)

    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert "investigate_code" in tools
    assert tools["investigate_code"].annotations.readOnlyHint is True
    assert "AFTER rg/direct reads or Serena" in str(tools["investigate_code"].description)

    result = await server.investigate_code(
        "Find MCP tools",
        str(tmp_path),
        max_turns=2,
        use_case="cross_file_contract",
        attempted_local_methods=["rg"],
    )

    assert result.evidence_paths == ["src/source_scout/server.py:1-20"]


@pytest.mark.asyncio
async def test_explore_local_code_returns_structured_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def slow_explore(**kwargs: Any) -> LocalExploreResult:
        await asyncio.sleep(1)
        raise AssertionError("unreachable")

    monkeypatch.setenv("SOURCE_SCOUT_MCP_DEADLINE_SECONDS", "0.01")
    monkeypatch.setattr(fastcontext, "explore_local_project", slow_explore)

    with pytest.raises(ToolError) as exc_info:
        await server.investigate_code("Find code", str(tmp_path))

    failure = json.loads(str(exc_info.value))
    assert failure["error_type"] == "timeout"
    assert failure["stage"] == "exploration"
    assert failure["retryable"] is True


@pytest.mark.asyncio
async def test_explore_local_code_returns_structured_connection_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def failed_explore(**kwargs: Any) -> LocalExploreResult:
        raise deepseek.ModelConnectionError("Could not connect to the DeepSeek API.")

    monkeypatch.setattr(fastcontext, "explore_local_project", failed_explore)

    with pytest.raises(ToolError) as exc_info:
        await server.investigate_code("Find code", str(tmp_path))

    failure = json.loads(str(exc_info.value))
    assert failure["error_type"] == "connection"
    assert failure["stage"] == "exploration"
    assert failure["retryable"] is True
