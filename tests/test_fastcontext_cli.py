import json
import sys
from pathlib import Path

import pytest

from source_scout import deepseek, fastcontext


def test_refine_evidence_cli_invokes_fastcontext(monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    async def fake_refine_candidate(
        candidate_id: str,
        task: str,
        max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
    ) -> dict[str, object]:
        assert candidate_id == "abc"
        assert task == "Find evidence"
        assert max_turns == 2
        return {"candidate_id": candidate_id, "evidence_paths": ["src/file.ts:1-2"]}

    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine_candidate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source_scout",
            "refine-evidence",
            "--candidate-id",
            "abc",
            "--task",
            "Find evidence",
            "--max-turns",
            "2",
        ],
    )
    main_module.main()
    captured = capsys.readouterr()
    assert '"candidate_id": "abc"' in captured.out


def test_refine_evidence_cli_invokes_suite_batch(monkeypatch, capsys, tmp_path: Path) -> None:
    import source_scout.__main__ as main_module

    output_path = tmp_path / "report.json"

    async def fake_refine_suite(
        suite: str,
        top_k: int,
        label: str | None = None,
        output_path: Path | None = None,
        max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
        limit_tasks: int | None = None,
    ) -> dict[str, object]:
        assert suite == "ui-reuse"
        assert top_k == 2
        assert label == "unit"
        assert output_path == tmp_path / "report.json"
        assert max_turns == 3
        assert limit_tasks == 1
        return {
            "suite_id": "ui-reuse",
            "label": label,
            "metrics": {"candidate_count": 2},
            "scoring_recommendation": {"status": "tie_breaker_ready"},
            "report_path": str(output_path),
        }

    monkeypatch.setattr(fastcontext, "refine_suite", fake_refine_suite)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source_scout",
            "refine-evidence",
            "--suite",
            "ui-reuse",
            "--top-k",
            "2",
            "--label",
            "unit",
            "--output",
            str(output_path),
            "--max-turns",
            "3",
            "--limit-tasks",
            "1",
        ],
    )
    main_module.main()
    captured = capsys.readouterr()
    assert '"suite_id": "ui-reuse"' in captured.out
    assert '"candidate_count": 2' in captured.out


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
            "--use-case", "cross_file_contract",
            "--attempted-local-method", "rg",
            "--attempted-local-method", "direct_read",
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


def test_model_status_cli_exits_nonzero_when_unhealthy(monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    async def unhealthy_status(smoke_test: bool) -> dict[str, object]:
        assert smoke_test is False
        return {"healthy": False, "error_type": "connection"}

    monkeypatch.setattr(main_module, "_api_status", unhealthy_status)
    monkeypatch.setattr(sys, "argv", ["source_scout", "model-status"])

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "connection"


def test_model_status_cli_exits_zero_when_healthy(monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    async def healthy_status(smoke_test: bool) -> dict[str, object]:
        return {"healthy": True, "model_available": True}

    monkeypatch.setattr(main_module, "_api_status", healthy_status)
    monkeypatch.setattr(sys, "argv", ["source_scout", "model-status"])

    main_module.main()

    assert json.loads(capsys.readouterr().out)["healthy"] is True
