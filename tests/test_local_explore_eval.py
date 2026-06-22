import json
import sys
from pathlib import Path
from typing import Any

import pytest

from repo_finder import catalog, local_explore_eval
from repo_finder.models import LocalExploreResult


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_FINDER_HOME", str(tmp_path / ".repo_finder"))
    catalog.reset_connection()
    yield
    catalog.reset_connection()


def _write_local_project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "foo.py").write_text(
        "\n".join(
            [
                "def target_function():",
                "    return 'target'",
                "",
                "def helper():",
                "    return target_function()",
            ]
        ),
        encoding="utf-8",
    )
    (root / "src" / "extra.py").write_text("target = True\n", encoding="utf-8")


def _suite(project_root: Path) -> dict[str, Any]:
    return local_explore_eval.validate_suite(
        {
            "suite_id": "local-unit",
            "default_project_path": str(project_root),
            "pass_threshold": {
                "path_hit_rate": 1.0,
                "line_overlap_rate": 1.0,
                "max_bad_citations_per_task": 0,
            },
            "tasks": [
                {
                    "id": "find_target",
                    "task": "Find target function",
                    "expected_citations": [
                        {
                            "path": "src/foo.py",
                            "start_line": 1,
                            "end_line": 3,
                        }
                    ],
                    "acceptable_citations": [
                        {
                            "path": "src/extra.py",
                            "start_line": 1,
                            "end_line": 1,
                        }
                    ],
                    "manual_search_terms": ["target"],
                }
            ],
        }
    )


def test_tracked_local_explore_suite_loads_by_alias() -> None:
    suite = local_explore_eval.load_suite("repo-finder")

    assert suite["suite_id"] == "local-explore-repo-finder"
    assert 15 <= len(suite["tasks"]) <= 25
    assert suite["tasks"][0]["expected_citations"]


def test_validate_suite_rejects_missing_expected_citations() -> None:
    with pytest.raises(ValueError, match="expected_citations"):
        local_explore_eval.validate_suite(
            {
                "suite_id": "bad",
                "tasks": [{"id": "bad", "task": "Find code"}],
            }
        )


def test_score_citations_reports_budget_violations(tmp_path: Path) -> None:
    for name in ["a.py", "b.py", "c.py", "d.py"]:
        (tmp_path / name).write_text("line\n", encoding="utf-8")

    scoring = local_explore_eval._score_citations(
        tmp_path,
        [local_explore_eval.ExpectedCitation("a.py", 1, 1)],
        [],
        [
            local_explore_eval.ReturnedCitation("a.py", 1, 1, "a.py:1-1"),
            local_explore_eval.ReturnedCitation("b.py", 1, 1, "b.py:1-1"),
            local_explore_eval.ReturnedCitation("c.py", 1, 1, "c.py:1-1"),
            local_explore_eval.ReturnedCitation("d.py", 1, 1, "d.py:1-1"),
        ],
    )
    metrics = local_explore_eval._metrics(
        [
            {
                "status": "completed",
                "passed": False,
                "any_expected_path_hit": scoring["any_expected_path_hit"],
                "any_line_overlap_hit": scoring["any_line_overlap_hit"],
                "bad_citation_count": scoring["bad_citation_count"],
                "invalid_citation_count": scoring["invalid_citation_count"],
                "manual_search": {"file_count": 1},
                "manual_search_file_reduction": 0.0,
                "duration_seconds": 0.1,
                "tool_call_count": 1,
                "turn_count": 1,
                "tool_trace": [],
                **scoring,
            }
        ]
    )

    assert scoring["over_budget"] is True
    assert scoring["citation_budget_violation_count"] == 2
    assert metrics["over_budget_task_count"] == 1
    assert metrics["citation_budget_violation_count"] == 2


@pytest.mark.asyncio
async def test_evaluate_suite_scores_hits_bad_citations_and_manual_search(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_local_project(tmp_path)
    suite = _suite(tmp_path)

    async def fake_explore_local_project(
        task: str,
        project_path: str | Path = ".",
        max_turns: int = 6,
    ) -> LocalExploreResult:
        assert task == "Find target function"
        assert Path(project_path) == tmp_path.resolve()
        assert max_turns == 2
        return LocalExploreResult(
            task=task,
            project_path=str(project_path),
            model_id="fastcontext-1.0-4b-rl",
            prompt_version="fastcontext-refine-v1",
            schema_version="fastcontext-evidence-v1",
            analyzer_version="fastcontext-harness-v1",
            status="completed",
            evidence_paths=["src/foo.py:1-2", "src/extra.py:1-1"],
            notes=["focused result"],
            tool_trace=[
                {
                    "turn": 1,
                    "tool_calls": ["Grep"],
                    "tool_call_count": 1,
                    "observation_count": 1,
                    "final_citations": ["src/foo.py:1-2"],
                    "validation_notes": [],
                }
            ],
        )

    monkeypatch.setattr(
        local_explore_eval.fastcontext,
        "explore_local_project",
        fake_explore_local_project,
    )

    report = await local_explore_eval.evaluate_suite(suite, max_turns=2, label="unit")

    assert report["passed"] is True
    assert report["metrics"]["path_hits"] == 1
    assert report["metrics"]["line_overlap_hits"] == 1
    assert report["metrics"]["bad_citation_count"] == 0
    assert report["metrics"]["invalid_citation_count"] == 0
    assert report["metrics"]["tool_call_count"] == 1
    assert report["metrics"]["average_citation_count"] == 2.0
    assert report["metrics"]["over_budget_task_count"] == 0
    assert report["metrics"]["citation_budget_violation_count"] == 0
    assert report["metrics"]["average_file_precision"] == 1.0
    assert report["metrics"]["average_file_recall"] == 1.0
    assert report["metrics"]["average_line_f1"] > 0
    assert report["metrics"]["average_explore_score"] > 0
    task = report["tasks"][0]
    assert task["returned_citation_count"] == 2
    assert task["over_budget"] is False
    assert task["citation_budget_violation_count"] == 0
    assert task["manual_search"]["file_count"] == 2
    assert task["returned_file_count"] == 2
    assert task["manual_search_file_reduction"] == 0.0
    assert task["failure_buckets"] == {
        "no_tool_calls": False,
        "wrong_file": False,
        "right_file_wrong_range": False,
        "invalid_final_citation": False,
        "unsupported_final_citation": False,
        "final_answer_oscillation": False,
        "fallback_observations": False,
    }
    assert report["metrics"]["failure_bucket_counts"]["wrong_file"] == 0


@pytest.mark.asyncio
async def test_evaluate_suite_does_not_pass_fallback_observations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_local_project(tmp_path)
    suite = _suite(tmp_path)

    async def fake_explore_local_project(
        task: str,
        project_path: str | Path = ".",
        max_turns: int = 6,
    ) -> LocalExploreResult:
        return LocalExploreResult(
            task=task,
            project_path=str(project_path),
            model_id="fastcontext-1.0-4b-rl",
            prompt_version="fastcontext-refine-v1",
            schema_version="fastcontext-evidence-v1",
            analyzer_version="fastcontext-harness-v1",
            status="fallback_observations",
            evidence_paths=["src/foo.py:1-2"],
            notes=["Fallback observations only."],
            tool_trace=[],
        )

    monkeypatch.setattr(
        local_explore_eval.fastcontext,
        "explore_local_project",
        fake_explore_local_project,
    )

    report = await local_explore_eval.evaluate_suite(suite, max_turns=2, label="unit")

    assert report["passed"] is False
    assert report["metrics"]["failed_tasks"] == 1
    assert report["tasks"][0]["status"] == "fallback_observations"
    assert report["tasks"][0]["passed"] is False
    assert report["tasks"][0]["failure_buckets"]["fallback_observations"] is True
    assert report["metrics"]["failure_bucket_counts"]["fallback_observations"] == 1


@pytest.mark.asyncio
async def test_run_local_explore_eval_writes_report(tmp_path: Path, monkeypatch) -> None:
    _write_local_project(tmp_path)
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_id": "local-unit",
                "default_project_path": str(tmp_path),
                "tasks": [
                    {
                        "id": "find_target",
                        "task": "Find target function",
                        "expected_citations": [
                            {"path": "src/foo.py", "start_line": 1, "end_line": 3}
                        ],
                        "manual_search_terms": ["target"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "report.json"

    async def fake_explore_local_project(*args: Any, **kwargs: Any) -> LocalExploreResult:
        return LocalExploreResult(
            task="Find target function",
            project_path=str(tmp_path),
            model_id="fastcontext-1.0-4b-rl",
            prompt_version="fastcontext-refine-v1",
            schema_version="fastcontext-evidence-v1",
            analyzer_version="fastcontext-harness-v1",
            status="completed",
            evidence_paths=["src/foo.py:1-2"],
            notes=[],
            tool_trace=[],
        )

    monkeypatch.setattr(
        local_explore_eval.fastcontext,
        "explore_local_project",
        fake_explore_local_project,
    )

    report = await local_explore_eval.run_local_explore_eval(
        str(suite_path),
        max_turns=1,
        label="unit",
        output_path=output_path,
    )

    assert report["report_path"] == str(output_path)
    assert output_path.exists()
    stored = json.loads(output_path.read_text(encoding="utf-8"))
    assert stored["suite_id"] == "local-unit"
    assert stored["tasks"][0]["returned_citations"] == ["src/foo.py:1-2"]


def test_eval_local_explore_cli_invokes_runner(monkeypatch, capsys, tmp_path: Path) -> None:
    import repo_finder.__main__ as main_module
    from repo_finder import local_explore_eval as eval_module

    output_path = tmp_path / "report.json"

    async def fake_run_local_explore_eval(
        suite: str,
        max_turns: int = 6,
        label: str | None = None,
        output_path: Path | None = None,
        limit_tasks: int | None = None,
    ) -> dict[str, object]:
        assert suite == "repo-finder"
        assert max_turns == 2
        assert label == "unit"
        assert output_path == tmp_path / "report.json"
        assert limit_tasks == 3
        return {
            "suite_id": "local-explore-repo-finder",
            "label": label,
            "passed": True,
            "metrics": {"path_hits": 3},
            "report_path": str(output_path),
        }

    monkeypatch.setattr(eval_module, "run_local_explore_eval", fake_run_local_explore_eval)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repo-finder",
            "eval-local-explore",
            "--suite",
            "repo-finder",
            "--max-turns",
            "2",
            "--label",
            "unit",
            "--output",
            str(output_path),
            "--limit-tasks",
            "3",
        ],
    )

    main_module.main()

    captured = capsys.readouterr()
    assert '"suite_id": "local-explore-repo-finder"' in captured.out
    assert '"path_hits": 3' in captured.out
