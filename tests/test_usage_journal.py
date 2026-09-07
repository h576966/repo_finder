import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from source_scout import fastcontext, implementation_references, usage_journal
from source_scout.models import FindImplementationReferencesResult


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_reference_abstention_and_codex_feedback_cli(tmp_path, monkeypatch, capsys):
    from source_scout.__main__ import main

    result = implementation_references.find_implementation_references("WebGPU shader compiler")
    report_path = Path(result.usage["report_path"])
    original = report_path.read_bytes()
    record = _read(report_path)
    assert record["result"]["status"] == "abstained"
    assert record["kind"] == "test"
    assert len(record["code_sha256"]) == 64
    assert list(report_path.parent.glob("feedback-*.json")) == []

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "feedback",
            "--report",
            str(report_path),
            "--outcome",
            "unassessed",
            "--observation",
            "The empty collection cannot establish reference usefulness",
        ],
    )
    main()
    feedback = json.loads(capsys.readouterr().out)
    assert feedback["assessor"] == "codex"
    assert feedback["usage_id"] == record["usage_id"]
    assert feedback["outcome"] == "unassessed"
    assert feedback["evidence"] is None
    assert report_path.read_bytes() == original


def test_feedback_preserves_concurrent_assessments():
    result = implementation_references.find_implementation_references("callback registry")
    path = result.usage["report_path"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        feedback = list(
            pool.map(
                lambda index: usage_journal.write_feedback(path, "unassessed", f"Observation {index}"),
                range(8),
            )
        )
    assert len({item["feedback_path"] for item in feedback}) == 8
    assert len(list(Path(path).parent.glob("feedback-*.json"))) == 8
    assert all(_read(item["feedback_path"])["assessor"] == "codex" for item in feedback)


@pytest.mark.parametrize("outcome,observation", [("excellent", "Reason"), ("helped", " ")])
def test_invalid_feedback_is_rejected(outcome, observation):
    result = implementation_references.find_implementation_references("callback registry")
    with pytest.raises(ValueError):
        usage_journal.write_feedback(result.usage["report_path"], outcome, observation)
    assert not list(Path(result.usage["report_path"]).parent.glob("feedback-*.json"))


def test_feedback_rejects_unrelated_report(tmp_path):
    path = tmp_path / "result.json"
    path.write_text('{"schema_version": "unrelated"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Expected"):
        usage_journal.write_feedback(path, "helped", "Checked source")
    assert not list(tmp_path.glob("feedback-*.json"))


@pytest.mark.asyncio
async def test_disabled_investigation_is_recorded_without_model(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled investigation must not configure a model")

    monkeypatch.setattr(fastcontext.deepseek, "get_config", forbidden)
    result = await fastcontext.explore_local_project("Trace callback registration", tmp_path)
    record = _read(result.usage["report_path"])
    assert result.status == record["result"]["status"] == "disabled"
    assert record["inputs"]["project_path"] == str(tmp_path)
    assert record["result"]["report_path"] is None
    assert not (tmp_path / ".source_scout").exists()


@pytest.mark.asyncio
async def test_cancellation_is_recorded_and_propagated(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(tmp_path / "journal"))

    @usage_journal.journaled
    async def cancelled(task, transport):
        raise asyncio.CancelledError("not copied to the journal")

    with pytest.raises(asyncio.CancelledError):
        await cancelled("Trace callback", transport="secret-value")
    paths = list((tmp_path / "journal" / "usage").glob("*/result.json"))
    assert len(paths) == 1
    record = _read(paths[0])
    assert record["error"] == {"type": "CancelledError"}
    assert record["result"] is None
    assert "secret-value" not in paths[0].read_text()


def test_logging_failure_does_not_replace_result_or_error(tmp_path, monkeypatch):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(blocked))

    @usage_journal.journaled
    def operation(task):
        if task == "fail":
            raise ValueError("original failure")
        return FindImplementationReferencesResult(task=task, status="abstained", results=[])

    result = operation("a question")
    assert result.status == "abstained"
    assert "error" in result.usage
    with pytest.raises(ValueError, match="original failure"):
        operation("fail")
    assert blocked.read_text() == "keep"


def test_evaluation_tag_is_scoped():
    with usage_journal.evaluation_usage():
        evaluated = implementation_references.find_implementation_references("callback registry")
    ordinary = implementation_references.find_implementation_references("callback registry")
    assert _read(evaluated.usage["report_path"])["kind"] == "evaluation"
    assert _read(ordinary.usage["report_path"])["kind"] == "test"


def test_failed_reference_exposes_feedback_path_through_mcp():
    from fastmcp.exceptions import ToolError

    from source_scout import server

    with pytest.raises(ToolError) as error:
        server.find_implementation_references(" ")
    failure = json.loads(str(error.value))
    path = failure["usage"]["report_path"]
    record = _read(path)
    assert record["error"]["type"] == "ImplementationReferenceError"
    feedback = usage_journal.write_feedback(path, "did_not_help", "The task was empty")
    assert feedback["usage_id"] == record["usage_id"]
