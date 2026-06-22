import json
import sys
from pathlib import Path
from typing import Any

import pytest

from source_scout import catalog, eval_runner


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(tmp_path / ".source_scout"))
    catalog.reset_connection()
    yield
    catalog.reset_connection()


def _suite_file(tmp_path: Path, tasks: list[dict[str, Any]]) -> Path:
    path = tmp_path / "suite.json"
    path.write_text(
        json.dumps(
            {
                "suite_id": "ui-reuse",
                "description": "test suite",
                "tasks": tasks,
            }
        ),
        encoding="utf-8",
    )
    return path


def _task(
    *,
    task_id: str = "task",
    repo_id: str = "good/repo",
    capability: str = "data-table",
    avoid_repo_ids: list[str] | None = None,
    required_path_terms_any: list[str] | None = None,
    required_dependencies_any: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "task": "Find a reusable data table",
        "capability": capability,
        "expected_repo_ids": [repo_id],
        "acceptable_repo_ids": [],
        "avoid_repo_ids": avoid_repo_ids or [],
        "required_path_terms_any": required_path_terms_any or [],
        "required_dependencies_any": required_dependencies_any or [],
        "max_rank_for_hit": 3,
    }


def _asset(
    tmp_path: Path,
    *,
    repo_id: str,
    capability: str,
    entry_paths: list[str],
    dependencies: list[str] | None = None,
    score: float = 0.9,
) -> str:
    owner, name = repo_id.split("/", 1)
    snapshot_root = tmp_path / owner / name
    snapshot_root.mkdir(parents=True)
    stored_repo_id = catalog.upsert_repository(
        {
            "owner": {"login": owner},
            "name": name,
            "full_name": repo_id,
            "html_url": f"https://github.com/{repo_id}",
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
    snapshot_id = catalog.upsert_snapshot(stored_repo_id, f"{owner}sha", "main", snapshot_root)
    catalog.upsert_repository_card(
        snapshot_id,
        {
            "card_version": "repo-card-v1",
            "gemma_profile": {
                "repository_type": "reference_application",
                "capabilities": [
                    {
                        "name": capability.replace("-", " "),
                        "confidence": 1.0,
                        "evidence": entry_paths,
                    }
                ],
                "likely_usefulness": 0.9,
                "extractability": 0.9,
                "maintenance_quality": 0.9,
                "needs_fastcontext": True,
                "concerns": [],
            },
        },
    )
    return catalog.upsert_asset(
        snapshot_id,
        stored_repo_id,
        capability,
        {
            "entry_paths": entry_paths,
            "dependency_paths": ["package.json"],
            "external_dependencies": dependencies or [],
            "evidence_paths": [f"{entry_paths[0]}:1-3"],
            "reuse_score": score,
            "synthesis": {
                "adaptation_notes": [],
                "ui_path_score": 0.9,
                "noise_penalty": 0.0,
                "capability_path_score": 1.0,
            },
        },
    )


def test_load_suite_validates_required_fields(tmp_path: Path) -> None:
    suite_path = _suite_file(
        tmp_path,
        [
            _task(
                required_path_terms_any=["data-table"],
                required_dependencies_any=["@tanstack/react-table"],
            )
        ],
    )

    loaded = eval_runner.load_suite(str(suite_path))

    assert loaded["suite_id"] == "ui-reuse"
    assert loaded["tasks"][0]["id"] == "task"
    assert loaded["tasks"][0]["required_dependencies_any"] == ["@tanstack/react-table"]


def test_tracked_golden_suites_load_by_alias() -> None:
    ui_suite = eval_runner.load_suite("ui-reuse")
    backend_suite = eval_runner.load_suite("nextjs-backend")

    assert len(ui_suite["tasks"]) == 10
    assert len(backend_suite["tasks"]) == 10


def test_load_suite_rejects_missing_labels() -> None:
    with pytest.raises(ValueError, match="expected or acceptable"):
        eval_runner.validate_suite(
            {
                "suite_id": "bad",
                "tasks": [
                    {
                        "id": "bad",
                        "task": "Find something",
                        "capability": "data-table",
                    }
                ],
            }
        )


def test_evaluate_suite_scores_hits_constraints_and_mrr(tmp_path: Path) -> None:
    _asset(
        tmp_path,
        repo_id="good/repo",
        capability="data-table",
        entry_paths=["components/data-table/data-table.tsx"],
        dependencies=["@tanstack/react-table"],
    )
    suite = eval_runner.validate_suite(
        {
            "suite_id": "ui-reuse",
            "tasks": [
                _task(
                    required_path_terms_any=["data-table"],
                    required_dependencies_any=["@tanstack/react-table"],
                )
            ],
        }
    )

    report = eval_runner.evaluate_suite(suite, top_k=5, label="unit")

    assert report["metrics"]["top_1_hits"] == 1
    assert report["metrics"]["top_3_hits"] == 1
    assert report["metrics"]["mrr"] == 1.0
    assert report["tasks"][0]["candidates"][0]["failure_reasons"] == []


def test_evaluate_suite_tracks_avoid_violations(tmp_path: Path) -> None:
    _asset(
        tmp_path,
        repo_id="ufukayyildiz/omnidock",
        capability="settings",
        entry_paths=["src/worker/schema.ts"],
        score=1.0,
    )
    suite = eval_runner.validate_suite(
        {
            "suite_id": "ui-reuse",
            "tasks": [
                _task(
                    repo_id="missing/repo",
                    capability="settings",
                    avoid_repo_ids=["ufukayyildiz/omnidock"],
                )
            ],
        }
    )

    report = eval_runner.evaluate_suite(suite, top_k=5)

    assert report["metrics"]["avoid_repo_violations"] == 1
    assert "avoid_repo_in_top3" in report["tasks"][0]["candidates"][0]["failure_reasons"]


def test_eval_cli_writes_report(tmp_path: Path, monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    _asset(
        tmp_path,
        repo_id="good/repo",
        capability="data-table",
        entry_paths=["components/data-table/data-table.tsx"],
    )
    suite_path = _suite_file(tmp_path, [_task(required_path_terms_any=["data-table"])])
    output_path = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "eval",
            "--suite",
            str(suite_path),
            "--top-k",
            "5",
            "--label",
            "unit",
            "--output",
            str(output_path),
        ],
    )

    main_module.main()

    captured = capsys.readouterr()
    assert '"suite_id": "ui-reuse"' in captured.out
    assert output_path.exists()
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["metrics"]["top_1_hits"] == 1
