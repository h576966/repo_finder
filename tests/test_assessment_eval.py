from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from source_scout import assessment_eval, catalog


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(tmp_path / ".source_scout"))
    catalog.reset_connection()
    yield
    catalog.reset_connection()


def test_assessment_smoke_suite_loads_by_alias() -> None:
    suite = assessment_eval.load_suite("assessment-smoke")

    assert suite["suite_id"] == "assessment-smoke"
    assert len(suite["tasks"]) >= 6
    assert suite["tasks"][0]["expected_final_verdicts"]


def test_assessment_suite_rejects_missing_expected_verdicts() -> None:
    with pytest.raises(ValueError, match="expected_final_verdicts"):
        assessment_eval.validate_suite(
            {
                "suite_id": "bad",
                "tasks": [{"id": "bad", "task": "Assess something"}],
            }
        )


@pytest.mark.asyncio
async def test_run_assessment_eval_reports_expected_metrics(tmp_path: Path) -> None:
    output_path = tmp_path / "assessment-report.json"

    report = await assessment_eval.run_assessment_eval(
        "assessment-smoke",
        label="unit",
        output_path=output_path,
    )

    assert report["passed"] is True
    assert output_path.exists()
    metrics = report["metrics"]
    assert metrics["assessment_count"] == 8
    assert metrics["completed_count"] == 8
    assert metrics["cache_hit_count"] == 1
    assert metrics["verdict_match_rate"] == 1.0
    assert metrics["unknown_evidence_id_repair_count"] == 1
    assert metrics["fastcontext_attempted_count"] == 2
    assert metrics["fastcontext_completed_count"] == 1
    assert metrics["fastcontext_error_count"] == 1
    assert metrics["stale_fastcontext_reuse_count"] == 0
    assert report["failure_examples"] == []
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["metrics"]["assessment_count"] == 8


@pytest.mark.asyncio
async def test_assessment_eval_supports_deterministic_only_mode(tmp_path: Path) -> None:
    report = await assessment_eval.run_assessment_eval(
        "assessment-smoke",
        label="deterministic",
        output_path=tmp_path / "deterministic.json",
        deterministic_only=True,
    )

    assert report["deterministic_only"] is True
    assert report["metrics"]["fastcontext_attempted_count"] == 0


def test_eval_assess_cli_prints_summary(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path) -> None:
    import source_scout.__main__ as main_module

    output_path = tmp_path / "cli-report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "eval-assess",
            "--suite",
            "assessment-smoke",
            "--label",
            "cli",
            "--output",
            str(output_path),
        ],
    )

    main_module.main()

    captured = capsys.readouterr()
    assert '"suite_id": "assessment-smoke"' in captured.out
    assert '"assessment_count": 8' in captured.out
    assert output_path.exists()
