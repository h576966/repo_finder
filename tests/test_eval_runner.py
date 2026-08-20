import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from source_scout import catalog, eval_runner


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
    task_text: str = "Find a reusable data table",
    repo_id: str = "good/repo",
    capability: str = "data-table",
    expect_no_match: bool = False,
    project_path: str = "",
    expected_commit_sha: str = "",
    expected_source_paths_any: list[str] | None = None,
    avoid_repo_ids: list[str] | None = None,
    required_path_terms_any: list[str] | None = None,
    required_dependencies_any: list[str] | None = None,
    required_bundle_files_all: list[str] | None = None,
    allowed_bundle_files: list[str] | None = None,
    max_unresolved_local_imports: int | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "task": task_text,
        "capability": capability,
        "expect_no_match": expect_no_match,
        "project_path": project_path,
        "expected_commit_sha": expected_commit_sha,
        "expected_source_paths_any": expected_source_paths_any or [],
        "expected_repo_ids": [] if expect_no_match else [repo_id],
        "acceptable_repo_ids": [],
        "avoid_repo_ids": avoid_repo_ids or [],
        "required_path_terms_any": required_path_terms_any or [],
        "required_dependencies_any": required_dependencies_any or [],
        "required_bundle_files_all": required_bundle_files_all or [],
        "allowed_bundle_files": allowed_bundle_files or [],
        "max_unresolved_local_imports": max_unresolved_local_imports,
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
    for entry_path in entry_paths:
        path = snapshot_root / entry_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export const reusable = true\n", encoding="utf-8")
    (snapshot_root / "package.json").write_text(
        json.dumps({"dependencies": {dependency: "1.0.0" for dependency in dependencies or []}}),
        encoding="utf-8",
    )
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
            "repository_profile": {
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
    entry_path = "components/data-table/data-table.tsx"
    suite_path = _suite_file(
        tmp_path,
        [
            _task(
                project_path="evals/fixtures/target",
                expected_commit_sha="goodsha",
                expected_source_paths_any=[entry_path],
                required_path_terms_any=["data-table"],
                required_dependencies_any=["@tanstack/react-table"],
                required_bundle_files_all=[entry_path, "package.json"],
                allowed_bundle_files=[entry_path, "package.json", "src/table.test.tsx"],
                max_unresolved_local_imports=0,
            )
        ],
    )

    loaded = eval_runner.load_suite(str(suite_path))

    assert loaded["suite_id"] == "ui-reuse"
    assert loaded["tasks"][0]["id"] == "task"
    assert loaded["tasks"][0]["required_dependencies_any"] == ["@tanstack/react-table"]
    assert loaded["tasks"][0]["project_path"] == "evals/fixtures/target"
    assert loaded["tasks"][0]["expected_commit_sha"] == "goodsha"
    assert loaded["tasks"][0]["expected_source_paths_any"] == [entry_path]
    assert loaded["tasks"][0]["required_bundle_files_all"] == [entry_path, "package.json"]
    assert loaded["tasks"][0]["max_unresolved_local_imports"] == 0


def test_tracked_golden_suites_load_by_alias() -> None:
    ui_suite = eval_runner.load_suite("ui-reuse")
    backend_suite = eval_runner.load_suite("nextjs-backend")
    personal_suite = eval_runner.load_suite("personal-code")
    holdout_suite = eval_runner.load_suite("core-holdout")

    assert len(ui_suite["tasks"]) == 10
    assert len(backend_suite["tasks"]) == 10
    assert len(personal_suite["tasks"]) >= 10
    assert len(holdout_suite["tasks"]) == 14
    assert sum(task["expect_no_match"] for task in holdout_suite["tasks"]) == 8


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


def test_load_suite_allows_no_match_without_repo_labels() -> None:
    suite = eval_runner.validate_suite(
        {
            "suite_id": "negative",
            "tasks": [
                {
                    "id": "no-match",
                    "task": "Find an implementation that is not cataloged",
                    "capability": "data-table",
                    "expect_no_match": True,
                }
            ],
        }
    )

    task = suite["tasks"][0]
    assert task["expect_no_match"] is True
    assert task["expected_repo_ids"] == []
    assert task["max_unresolved_local_imports"] is None


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
    assert report["metrics"]["positive_task_count"] == 1
    assert report["metrics"]["no_match_task_count"] == 0
    assert report["metrics"]["retrieval_correct_rate"] == 1.0
    assert report["tasks"][0]["candidates"][0]["failure_reasons"] == []


def test_evaluate_suite_scores_correct_no_match() -> None:
    suite = eval_runner.validate_suite(
        {
            "suite_id": "negative",
            "tasks": [_task(expect_no_match=True)],
        }
    )

    report = eval_runner.evaluate_suite(suite, top_k=3)

    assert report["passed"] is True
    assert report["metrics"]["positive_task_count"] == 0
    assert report["metrics"]["no_match_task_count"] == 1
    assert report["metrics"]["correct_no_match_count"] == 1
    assert report["metrics"]["correct_no_match_rate"] == 1.0
    assert report["metrics"]["retrieval_correct_rate"] == 1.0
    assert report["tasks"][0]["no_match_correct"] is True
    assert report["tasks"][0]["failure_buckets"] == []


def test_evaluate_suite_reports_unexpected_candidate_for_no_match(tmp_path: Path) -> None:
    _asset(
        tmp_path,
        repo_id="unexpected/repo",
        capability="data-table",
        entry_paths=["components/data-table/data-table.tsx"],
    )
    suite = eval_runner.validate_suite(
        {
            "suite_id": "negative",
            "tasks": [_task(expect_no_match=True)],
        }
    )

    report = eval_runner.evaluate_suite(suite, top_k=3)

    assert report["passed"] is False
    assert report["metrics"]["correct_no_match_count"] == 0
    assert report["metrics"]["unexpected_candidate_on_no_match_count"] == 1
    assert report["tasks"][0]["unexpected_candidate_on_no_match"] is True
    assert report["tasks"][0]["failure_buckets"] == ["unexpected_candidate_on_no_match"]
    assert report["tasks"][0]["candidates"][0]["failure_reasons"] == ["unexpected_candidate_on_no_match"]


def test_evaluate_suite_enforces_commit_and_source_path_contract(tmp_path: Path) -> None:
    _asset(
        tmp_path,
        repo_id="good/repo",
        capability="data-table",
        entry_paths=["components/data-table/data-table.tsx"],
    )
    suite = eval_runner.validate_suite(
        {
            "suite_id": "contract",
            "tasks": [
                _task(
                    expected_commit_sha="different-sha",
                    expected_source_paths_any=["src/missing-table.tsx"],
                )
            ],
        }
    )

    report = eval_runner.evaluate_suite(suite, top_k=3)

    task = report["tasks"][0]
    assert task["retrieval_correct"] is False
    assert task["constraint_failures"] == 1
    assert task["failure_buckets"] == ["expected_candidate_failed_constraints"]
    assert task["candidates"][0]["commit_sha_match"] is False
    assert task["candidates"][0]["source_path_match"] is False
    assert "commit_sha_mismatch" in task["candidates"][0]["failure_reasons"]
    assert "missing_expected_source_path" in task["candidates"][0]["failure_reasons"]


def test_evaluate_suite_rejects_expected_repo_with_wrong_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = SimpleNamespace(
        candidate_id="wrong-capability",
        repo_id="good/repo",
        capability="file-storage",
        score=0.9,
        commit_sha="sha",
        entry_paths=["src/table.ts"],
        evidence_paths=["src/table.ts:1-1"],
        external_dependencies=[],
    )
    monkeypatch.setattr(
        eval_runner.catalog,
        "search_assets",
        lambda *_args, **_kwargs: [candidate],
    )
    suite = eval_runner.validate_suite({"suite_id": "capability", "tasks": [_task(capability="data-table")]})

    report = eval_runner.evaluate_suite(suite, top_k=3)

    assert report["tasks"][0]["retrieval_correct"] is False
    assert report["metrics"]["top_1_capability_correct_count"] == 0
    assert "capability_mismatch" in report["tasks"][0]["candidates"][0]["failure_reasons"]


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
                    task_text="Find reusable profile or account settings UI",
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
            "source_scout",
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


@pytest.mark.asyncio
async def test_reuse_loop_report_records_find_assess_bundle_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_path = "components/data-table/data-table.tsx"
    asset_id = _asset(
        tmp_path,
        repo_id="good/repo",
        capability="data-table",
        entry_paths=[entry_path],
        dependencies=["@tanstack/react-table"],
    )
    suite = eval_runner.validate_suite(
        {
            "suite_id": "ui-reuse",
            "description": "loop suite",
            "tasks": [
                _task(
                    expected_commit_sha="goodsha",
                    expected_source_paths_any=[entry_path],
                    required_dependencies_any=["@tanstack/react-table"],
                    required_bundle_files_all=[entry_path, "package.json"],
                    allowed_bundle_files=[entry_path, "package.json"],
                    max_unresolved_local_imports=0,
                )
            ],
        }
    )

    class FakeAssessment:
        assessment_id = "assessment-1"
        final_verdict = "select"
        reuse_score = 0.86
        confidence = 0.77
        evidence_coverage = 0.66
        validation_notes = ["useful note", "", "second note"]

    async def fake_assess_candidate(**kwargs: Any) -> FakeAssessment:
        assert kwargs["candidate_id"] == asset_id
        assert kwargs["fastcontext_policy"] == "never"
        assert kwargs["max_evidence_rounds"] == 0
        return FakeAssessment()

    def fake_create_source_bundle(assessment_id: str) -> SimpleNamespace:
        assert assessment_id == "assessment-1"
        bundle_root = tmp_path / "bundles" / asset_id / assessment_id
        entry_copy = bundle_root / "source" / entry_path
        manifest_copy = bundle_root / "source" / "package.json"
        entry_copy.parent.mkdir(parents=True)
        entry_copy.write_text("export const reusable = true\n", encoding="utf-8")
        manifest_copy.write_text("{}\n", encoding="utf-8")
        return SimpleNamespace(
            bundle_path=str(bundle_root),
            commit_sha="goodsha",
            files=[entry_path, "package.json"],
            missing_files=[],
            recommended_read_order=[entry_path, "package.json"],
            file_hashes={
                entry_path: hashlib.sha256(entry_copy.read_bytes()).hexdigest(),
                "package.json": hashlib.sha256(manifest_copy.read_bytes()).hexdigest(),
            },
            unresolved_local_imports=[],
            total_bytes=entry_copy.stat().st_size + manifest_copy.stat().st_size,
        )

    monkeypatch.setattr(eval_runner.assessor, "assess_candidate", fake_assess_candidate)
    monkeypatch.setattr(eval_runner.bundles, "create_source_bundle", fake_create_source_bundle)
    output_path = tmp_path / "reuse-loop.json"

    report = await eval_runner.run_reuse_loop_report(
        str(_suite_file(tmp_path, suite["tasks"])),
        top_k=3,
        label="unit",
        output_path=output_path,
        limit_tasks=1,
    )

    task = report["tasks"][0]
    assert output_path.exists()
    assert report["metrics"]["task_count"] == 1
    assert report["metrics"]["top_k_expected_or_acceptable_hits"] == 1
    assert report["metrics"]["top_1_expected_or_acceptable_hits"] == 1
    assert report["metrics"]["top_1_expected_or_acceptable_hit_rate"] == 1.0
    assert report["metrics"]["positive_task_count"] == 1
    assert report["metrics"]["no_match_task_count"] == 0
    assert report["metrics"]["retrieval_correct_rate"] == 1.0
    assert report["metrics"]["selected_verdict_counts"] == {"select": 1}
    assert report["metrics"]["bundle_quality_task_count"] == 1
    assert report["metrics"]["bundle_quality_pass_count"] == 1
    assert report["metrics"]["bundle_quality_failure_count"] == 0
    assert report["metrics"]["bundle_required_file_recall"] == 1.0
    assert report["metrics"]["bundle_allowed_file_precision"] == 1.0
    assert report["metrics"]["bundle_unresolved_local_import_count"] == 0
    assert report["metrics"]["bundle_commit_sha_mismatch_count"] == 0
    assert report["metrics"]["accepted_assessment_count"] == 1
    assert report["metrics"]["positive_bundle_count"] == 1
    assert report["metrics"]["reuse_loop_success_count"] == 1
    assert report["passed"] is True
    assert task["returned_candidates"] == [
        {
            "rank": 1,
            "candidate_id": asset_id,
            "repo_id": "good/repo",
            "capability": "data-table",
            "target_fit_score": 0.0,
        }
    ]
    assert task["expected_or_acceptable_repo_in_top_k"] is True
    assert task["selected_candidate_id"] == asset_id
    assert task["selected_repo_id"] == "good/repo"
    assert task["selected_is_expected_or_acceptable"] is True
    assert task["selected_meets_expectations"] is True
    assert task["assessment_final_verdict"] == "select"
    assert task["reuse_score"] == 0.86
    assert task["confidence"] == 0.77
    assert task["evidence_coverage"] == 0.66
    assert Path(task["bundle_path"]).parts[-2:] == (asset_id, "assessment-1")
    assert task["copied_file_count"] == 2
    assert task["missing_file_count"] == 0
    assert task["missing_required_bundle_files"] == []
    assert task["unexpected_bundle_files"] == []
    assert task["bundle_commit_sha_match"] is True
    assert task["bundle_quality_passed"] is True
    assert task["assessment_accepted"] is True
    assert task["bundle_created"] is True
    assert task["reuse_loop_success"] is True
    assert task["bundle_failure_buckets"] == []
    assert task["notable_validation_notes"] == ["useful note", "second note"]


@pytest.mark.asyncio
async def test_reuse_loop_correct_no_match_skips_assessment_and_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = eval_runner.validate_suite(
        {
            "suite_id": "negative",
            "description": "no-match loop suite",
            "tasks": [_task(expect_no_match=True)],
        }
    )

    async def unexpected_assessment(**_kwargs: Any) -> None:
        raise AssertionError("assessment should not run for a no-match task")

    def unexpected_bundle(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("bundle creation should not run for a no-match task")

    monkeypatch.setattr(eval_runner.assessor, "assess_candidate", unexpected_assessment)
    monkeypatch.setattr(eval_runner.bundles, "create_source_bundle", unexpected_bundle)

    report = await eval_runner.evaluate_reuse_loop_suite(suite, top_k=3)

    task = report["tasks"][0]
    assert report["passed"] is True
    assert report["metrics"]["positive_task_count"] == 0
    assert report["metrics"]["no_match_task_count"] == 1
    assert report["metrics"]["correct_no_match_count"] == 1
    assert report["metrics"]["retrieval_correct_rate"] == 1.0
    assert report["metrics"]["assessed_count"] == 0
    assert report["metrics"]["bundle_count"] == 0
    assert report["metrics"]["no_match_downstream_work_count"] == 0
    assert report["metrics"]["no_match_reuse_loop_success_count"] == 1
    assert task["no_match_correct"] is True
    assert task["selected_candidate_id"] is None
    assert task["assessment_final_verdict"] is None
    assert task["bundle_path"] is None
    assert task["assessment_attempted"] is False
    assert task["bundle_attempted"] is False
    assert task["no_match_downstream_work"] is False
    assert task["reuse_loop_success"] is True
    assert task["failure_buckets"] == []


@pytest.mark.asyncio
async def test_reuse_loop_reject_verdict_fails_without_calling_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _asset(
        tmp_path,
        repo_id="good/repo",
        capability="data-table",
        entry_paths=["components/data-table/data-table.tsx"],
    )
    suite = eval_runner.validate_suite({"suite_id": "reject", "tasks": [_task()]})

    class FakeAssessment:
        assessment_id = "assessment-reject"
        final_verdict = "reject"
        reuse_score = 0.2
        confidence = 0.9
        evidence_coverage = 0.8
        validation_notes: list[str] = []

    async def fake_assess_candidate(**_kwargs: Any) -> FakeAssessment:
        return FakeAssessment()

    def unexpected_bundle(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("reject assessments must not create bundles")

    monkeypatch.setattr(eval_runner.assessor, "assess_candidate", fake_assess_candidate)
    monkeypatch.setattr(eval_runner.bundles, "create_source_bundle", unexpected_bundle)

    report = await eval_runner.evaluate_reuse_loop_suite(suite, top_k=3)

    task = report["tasks"][0]
    assert report["passed"] is False
    assert report["metrics"]["unacceptable_assessment_count"] == 1
    assert report["metrics"]["positive_bundle_count"] == 0
    assert report["metrics"]["positive_reuse_loop_success_count"] == 0
    assert task["assessment_attempted"] is True
    assert task["assessment_accepted"] is False
    assert task["bundle_attempted"] is False
    assert task["bundle_created"] is False
    assert task["reuse_loop_success"] is False
    assert "assessment_not_bundle_eligible" in task["failure_buckets"]
    assert "bundle_not_created" in task["failure_buckets"]


@pytest.mark.asyncio
async def test_reuse_loop_inspect_verdict_can_publish_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _asset(
        tmp_path,
        repo_id="good/repo",
        capability="data-table",
        entry_paths=["components/data-table/data-table.tsx"],
    )
    suite = eval_runner.validate_suite({"suite_id": "inspect", "tasks": [_task()]})

    class FakeAssessment:
        assessment_id = "assessment-inspect"
        final_verdict = "inspect"
        reuse_score = 0.6
        confidence = 0.7
        evidence_coverage = 0.8
        validation_notes: list[str] = []

    async def fake_assess_candidate(**_kwargs: Any) -> FakeAssessment:
        return FakeAssessment()

    def fake_bundle(assessment_id: str) -> SimpleNamespace:
        assert assessment_id == "assessment-inspect"
        return SimpleNamespace(bundle_path=str(tmp_path / "inspection-bundle"))

    monkeypatch.setattr(eval_runner.assessor, "assess_candidate", fake_assess_candidate)
    monkeypatch.setattr(eval_runner.bundles, "create_source_bundle", fake_bundle)

    report = await eval_runner.evaluate_reuse_loop_suite(suite, top_k=3)

    task = report["tasks"][0]
    assert report["passed"] is True
    assert report["metrics"]["accepted_assessment_count"] == 1
    assert report["metrics"]["positive_bundle_count"] == 1
    assert task["assessment_accepted"] is True
    assert task["bundle_created"] is True
    assert task["reuse_loop_success"] is True


@pytest.mark.asyncio
async def test_reuse_loop_fails_when_only_a_lower_rank_candidate_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = eval_runner.validate_suite({"suite_id": "wrong-top-one", "tasks": [_task()]})
    wrong = SimpleNamespace(
        candidate_id="wrong-candidate",
        repo_id="wrong/repo",
        capability="data-table",
        target_fit_score=0.0,
        entry_paths=["src/wrong.ts"],
        evidence_paths=["src/wrong.ts"],
        external_dependencies=[],
        commit_sha="wrongsha",
    )
    expected = SimpleNamespace(
        candidate_id="expected-candidate",
        repo_id="good/repo",
        capability="data-table",
        target_fit_score=0.0,
        entry_paths=["src/expected.ts"],
        evidence_paths=["src/expected.ts"],
        external_dependencies=[],
        commit_sha="goodsha",
    )

    def fake_search_assets(*_args: Any, **_kwargs: Any) -> list[SimpleNamespace]:
        return [wrong, expected]

    class FakeAssessment:
        assessment_id = "wrong-assessment"
        final_verdict = "select"
        reuse_score = 0.8
        confidence = 0.8
        evidence_coverage = 0.8
        validation_notes: list[str] = []

    async def fake_assess_candidate(**kwargs: Any) -> FakeAssessment:
        assert kwargs["candidate_id"] == "wrong-candidate"
        return FakeAssessment()

    def fake_bundle(_assessment_id: str) -> SimpleNamespace:
        return SimpleNamespace(bundle_path=str(tmp_path / "wrong-bundle"))

    monkeypatch.setattr(eval_runner.catalog, "search_assets", fake_search_assets)
    monkeypatch.setattr(eval_runner.assessor, "assess_candidate", fake_assess_candidate)
    monkeypatch.setattr(eval_runner.bundles, "create_source_bundle", fake_bundle)

    report = await eval_runner.evaluate_reuse_loop_suite(suite, top_k=3)

    task = report["tasks"][0]
    assert task["retrieval_correct"] is True
    assert task["selected_meets_expectations"] is False
    assert task["assessment_accepted"] is True
    assert task["bundle_created"] is True
    assert task["reuse_loop_success"] is False
    assert "selected_candidate_failed_constraints" in task["failure_buckets"]
    assert report["metrics"]["positive_reuse_loop_success_count"] == 0
    assert report["passed"] is False


@pytest.mark.asyncio
async def test_reuse_loop_report_skips_bundle_when_assessment_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = _asset(
        tmp_path,
        repo_id="good/repo",
        capability="data-table",
        entry_paths=["components/data-table/data-table.tsx"],
    )
    suite = eval_runner.validate_suite(
        {
            "suite_id": "ui-reuse",
            "description": "loop suite",
            "tasks": [_task()],
        }
    )

    async def fake_assess_candidate(**kwargs: Any) -> None:
        assert kwargs["candidate_id"] == asset_id
        raise RuntimeError("assessment failed")

    monkeypatch.setattr(eval_runner.assessor, "assess_candidate", fake_assess_candidate)

    report = await eval_runner.evaluate_reuse_loop_suite(suite, top_k=3, label="unit")

    task = report["tasks"][0]
    assert report["passed"] is False
    assert report["metrics"]["assessment_error_count"] == 1
    assert report["metrics"]["bundle_count"] == 0
    assert report["metrics"]["bundle_error_count"] == 0
    assert task["assessment_error"] == "assessment failed"
    assert task["bundle_error"] is None
    assert task["bundle_path"] is None


@pytest.mark.asyncio
async def test_reuse_loop_reports_bundle_quality_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_path = "components/data-table/data-table.tsx"
    _asset(
        tmp_path,
        repo_id="good/repo",
        capability="data-table",
        entry_paths=[entry_path],
    )
    suite = eval_runner.validate_suite(
        {
            "suite_id": "bundle-quality",
            "tasks": [
                _task(
                    required_bundle_files_all=[entry_path, "src/missing.ts"],
                    allowed_bundle_files=[entry_path],
                    max_unresolved_local_imports=0,
                )
            ],
        }
    )

    class FakeAssessment:
        assessment_id = "assessment-2"
        final_verdict = "select"
        reuse_score = 0.8
        confidence = 0.8
        evidence_coverage = 0.8
        validation_notes: list[str] = []

    async def fake_assess_candidate(**_kwargs: Any) -> FakeAssessment:
        return FakeAssessment()

    def fake_create_source_bundle(assessment_id: str) -> SimpleNamespace:
        assert assessment_id == "assessment-2"
        bundle_root = tmp_path / "bundles" / assessment_id
        entry_copy = bundle_root / "source" / entry_path
        manifest_copy = bundle_root / "source" / "package.json"
        entry_copy.parent.mkdir(parents=True)
        entry_copy.write_text("export const reusable = true\n", encoding="utf-8")
        manifest_copy.write_text("{}\n", encoding="utf-8")
        return SimpleNamespace(
            bundle_path=str(bundle_root),
            commit_sha="goodsha",
            files=[entry_path, "package.json"],
            missing_files=[],
            recommended_read_order=[entry_path, "package.json"],
            file_hashes={
                entry_path: hashlib.sha256(entry_copy.read_bytes()).hexdigest(),
                "package.json": "incorrect-hash",
            },
            unresolved_local_imports=[],
            total_bytes=entry_copy.stat().st_size + manifest_copy.stat().st_size,
        )

    monkeypatch.setattr(eval_runner.assessor, "assess_candidate", fake_assess_candidate)
    monkeypatch.setattr(eval_runner.bundles, "create_source_bundle", fake_create_source_bundle)

    report = await eval_runner.evaluate_reuse_loop_suite(suite, top_k=3)

    task = report["tasks"][0]
    assert report["passed"] is False
    assert report["metrics"]["bundle_quality_failure_count"] == 1
    assert report["metrics"]["bundle_required_file_recall"] == 0.5
    assert report["metrics"]["bundle_allowed_file_precision"] == 0.5
    assert report["metrics"]["bundle_missing_required_file_count"] == 1
    assert report["metrics"]["bundle_unexpected_file_count"] == 1
    assert report["metrics"]["bundle_file_hash_failure_count"] == 1
    assert task["missing_required_bundle_files"] == ["src/missing.ts"]
    assert task["unexpected_bundle_files"] == ["package.json"]
    assert task["bundle_failure_buckets"] == [
        "bundle_missing_required_files",
        "bundle_unexpected_files",
        "bundle_file_hash_failure",
    ]
    assert task["file_hash_failures"] == ["hash_mismatch:package.json"]
    assert task["failure_buckets"] == task["bundle_failure_buckets"]


def test_eval_reuse_loop_cli_prints_summary(monkeypatch, capsys, tmp_path: Path) -> None:
    import source_scout.__main__ as main_module

    async def fake_run_reuse_loop_report(
        suite: str,
        top_k: int,
        label: str | None = None,
        output_path: Path | None = None,
        *,
        limit_tasks: int | None = None,
        fastcontext_policy: str = "never",
        max_evidence_rounds: int = 0,
        force_assessment: bool = True,
    ) -> dict[str, Any]:
        assert suite == "ui-reuse"
        assert top_k == 2
        assert label == "unit"
        assert output_path == tmp_path / "loop.json"
        assert limit_tasks == 1
        assert fastcontext_policy == "never"
        assert max_evidence_rounds == 0
        assert force_assessment is False
        return {
            "suite_id": "ui-reuse",
            "label": label,
            "passed": True,
            "metrics": {"task_count": 1},
            "report_path": str(output_path),
        }

    monkeypatch.setattr(eval_runner, "run_reuse_loop_report", fake_run_reuse_loop_report)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source_scout",
            "eval-reuse-loop",
            "--suite",
            "ui-reuse",
            "--top-k",
            "2",
            "--label",
            "unit",
            "--output",
            str(tmp_path / "loop.json"),
            "--limit-tasks",
            "1",
            "--use-cache",
        ],
    )

    main_module.main()

    captured = capsys.readouterr()
    assert '"suite_id": "ui-reuse"' in captured.out
    assert '"task_count": 1' in captured.out
