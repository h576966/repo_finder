import json
import sys
from pathlib import Path

import pytest

from source_scout import deepseek, fastcontext


def test_explore_local_cli_invokes_fastcontext(monkeypatch, capsys, tmp_path: Path) -> None:
    import source_scout.__main__ as main_module

    async def fake_explore_local_project(
        task: str,
        project_path: str | Path = ".",
        max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
        trace_path: str | Path | None = None,
        reason: str = "",
        use_case=None,
        attempted_local_methods=None,
        anchors=None,
    ) -> object:
        assert task == "Find MCP tools"
        assert project_path == str(tmp_path)
        assert max_turns == 2
        assert trace_path == str(tmp_path / "trace.json")
        assert use_case == "cross_file_contract"
        assert attempted_local_methods == ["rg", "direct_read"]
        return fastcontext.LocalExploreResult(
            task=task,
            project_path=str(tmp_path),
            model_id=deepseek.DEEPSEEK_MODEL,
            prompt_version=fastcontext.PROMPT_VERSION,
            schema_version=fastcontext.SCHEMA_VERSION,
            analyzer_version=fastcontext.ANALYZER_VERSION,
            status="completed",
            evidence_paths=["src/source_scout/server.py:1-20"],
            notes=["MCP tools are registered here."],
            tool_trace=[],
        )

    monkeypatch.setattr(fastcontext, "explore_local_project", fake_explore_local_project)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source_scout",
            "explore-local",
            "--task",
            "Find MCP tools",
            "--project-path",
            str(tmp_path),
            "--max-turns",
            "2",
            "--format",
            "text",
            "--trace-path",
            str(tmp_path / "trace.json"),
            "--use-case",
            "cross_file_contract",
            "--attempted-local-method",
            "rg",
            "--attempted-local-method",
            "direct_read",
        ],
    )
    main_module.main()
    captured = capsys.readouterr()
    assert "src/source_scout/server.py:1-20" in captured.out
    assert "MCP tools are registered here." in captured.out


def test_explore_local_cli_failure_is_structured(monkeypatch, capsys, tmp_path: Path) -> None:
    import source_scout.__main__ as main_module

    async def failed_explore(**kwargs: object) -> object:
        raise deepseek.ModelConnectionError("Could not connect to the DeepSeek API.")

    monkeypatch.setattr(fastcontext, "explore_local_project", failed_explore)
    monkeypatch.setattr(
        sys,
        "argv",
        ["source_scout", "explore-local", "--task", "Find code", "--project-path", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.out == ""
    failure = json.loads(captured.err)
    assert failure["error_type"] == "connection"
    assert failure["stage"] == "exploration"
    assert "Traceback" not in captured.err
