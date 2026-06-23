import sys
from pathlib import Path

import pytest

from source_scout import cli_status, fastcontext, lmstudio
from tests.fastcontext_helpers import isolated_catalog

pytestmark = pytest.mark.usefixtures(isolated_catalog.__name__)

def test_fastcontext_status_cli_prints_json(monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    async def fake_status(
        start_server: bool,
        smoke_test: bool,
        load_model: bool = False,
        context_length: int = 65_536,
        gpu: str = "max",
    ) -> dict[str, object]:
        assert start_server is True
        assert smoke_test is True
        assert load_model is True
        assert context_length == 65536
        assert gpu == "max"
        return {"reachable": True, "fastcontext_available": True}

    monkeypatch.setattr(main_module, "_fastcontext_status", fake_status)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "fastcontext-status",
            "--start-server",
            "--smoke-test",
            "--load-model",
        ],
    )
    main_module.main()
    captured = capsys.readouterr()
    assert '"fastcontext_available": true' in captured.out


@pytest.mark.asyncio
async def test_fastcontext_status_loads_model_when_requested(monkeypatch) -> None:
    import source_scout.__main__ as main_module

    loaded = False

    async def fake_validate_models(config: lmstudio.LMStudioConfig) -> dict[str, object]:
        return {
            "base_url": config.base_url,
            "models": [config.fastcontext_model],
            "gemma_model": config.gemma_model,
            "fastcontext_model": config.fastcontext_model,
            "gemma_available": False,
            "fastcontext_available": True,
        }

    def fake_model_inventory(config: lmstudio.LMStudioConfig) -> dict[str, object]:
        return {
            "downloaded_models": [config.fastcontext_model],
            "loaded_models": [config.fastcontext_model] if loaded else [],
            "configured_models": {
                "gemma": {
                    "model_id": config.gemma_model,
                    "downloaded": False,
                    "loaded": False,
                    "loaded_detail": None,
                },
                "fastcontext": {
                    "model_id": config.fastcontext_model,
                    "downloaded": True,
                    "loaded": loaded,
                    "loaded_detail": {"contextLength": 65536} if loaded else None,
                },
            },
        }

    def fake_load_fastcontext_model(
        config: lmstudio.LMStudioConfig,
        context_length: int,
        gpu: str,
    ) -> dict[str, object]:
        nonlocal loaded
        assert context_length == 65536
        assert gpu == "max"
        loaded = True
        return {"loaded": True, "model_id": config.fastcontext_model}

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(lmstudio, "validate_models", fake_validate_models)
    monkeypatch.setattr(lmstudio, "model_inventory", fake_model_inventory)
    monkeypatch.setattr(lmstudio, "load_fastcontext_model", fake_load_fastcontext_model)
    monkeypatch.setattr(cli_status.asyncio, "sleep", fake_sleep)

    result = await main_module._fastcontext_status(
        start_server=False,
        smoke_test=False,
        load_model=True,
        context_length=65536,
        gpu="max",
    )

    configured = result["configured_models"]
    assert result["load_model"]["loaded"] is True
    assert configured["fastcontext"]["loaded"] is True
    assert configured["fastcontext"]["api_listed"] is True


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
            "source-scout",
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
            "source-scout",
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
    ) -> object:
        assert task == "Find MCP tools"
        assert project_path == str(tmp_path)
        assert max_turns == 2
        assert trace_path == str(tmp_path / "trace.json")
        return fastcontext.LocalExploreResult(
            task=task,
            project_path=str(tmp_path),
            model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
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
            "source-scout",
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
        ],
    )
    main_module.main()
    captured = capsys.readouterr()
    assert "src/source_scout/server.py:1-20" in captured.out
    assert "MCP tools are registered here." in captured.out
