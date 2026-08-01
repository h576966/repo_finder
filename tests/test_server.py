import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from source_scout import catalog, fastcontext, server
from source_scout.models import (
    AdaptationStep,
    AssessmentDimensions,
    LocalExploreResult,
    ReuseAssessmentResult,
)


@pytest.mark.asyncio
async def test_explore_local_code_tool_is_read_only_and_ephemeral(monkeypatch, tmp_path: Path) -> None:
    async def fake_explore_local_project(
        task: str,
        project_path: str,
        max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
    ) -> LocalExploreResult:
        assert task == "Find MCP tools"
        assert project_path == str(tmp_path)
        assert max_turns == 2
        return LocalExploreResult(
            task=task,
            project_path=str(tmp_path),
            model_id="fastcontext-1.0-4b-rl",
            prompt_version="fastcontext-refine-v2",
            schema_version="fastcontext-evidence-v1",
            analyzer_version="fastcontext-harness-v1",
            status="completed",
            evidence_paths=["src/source_scout/server.py:1-20"],
            notes=["MCP tools are registered here."],
            tool_trace=[],
        )

    monkeypatch.setattr(server.fastcontext, "explore_local_project", fake_explore_local_project)

    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert "explore_local_code" in tools
    assert tools["explore_local_code"].annotations.readOnlyHint is True
    assert "Read-only local" in str(tools["explore_local_code"].description)

    result = await server.explore_local_code(
        "Find MCP tools",
        str(tmp_path),
        max_turns=2,
    )

    assert result.evidence_paths == ["src/source_scout/server.py:1-20"]
    assert catalog.get_connection().execute("SELECT COUNT(*) FROM reuse_outcomes").fetchone()[0] == 0
    assert catalog.get_connection().execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_assess_reusable_code_tool_is_registered_and_returns_cli_shape(
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

    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert "assess_reusable_code" in tools
    assert not bool(getattr(tools["assess_reusable_code"].annotations, "readOnlyHint", False))
    assert "local assessment cache" in str(tools["assess_reusable_code"].description)

    result = await server.assess_reusable_code(
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
            "project_path": None,
        }
    ]


@pytest.mark.asyncio
async def test_assess_reusable_code_validates_task_policy_and_round_limit() -> None:
    with pytest.raises(ToolError, match="Task description is required"):
        await server.assess_reusable_code("asset-1", "")

    with pytest.raises(ToolError, match="fastcontext_policy must be one of"):
        await server.assess_reusable_code(
            "asset-1",
            "Find reusable code",
            fastcontext_policy="sometimes",
        )

    with pytest.raises(ToolError, match="max_evidence_rounds must be between 0 and 2"):
        await server.assess_reusable_code(
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
        await server.assess_reusable_code("missing", "Find reusable code")


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


def _store_reusable_assessment(asset_id: str, task: str) -> str:
    asset = catalog.get_asset_detail(asset_id)
    assert asset is not None
    source_path = Path(str(asset["snapshot_path"])) / "components" / "data-table.tsx"
    first_line = source_path.read_text(encoding="utf-8").splitlines()[0]
    assessment = ReuseAssessmentResult(
        candidate_id=asset_id,
        repo_id=str(asset["repo_id"]),
        snapshot_id=str(asset["snapshot_id"]),
        commit_sha=str(asset["commit_sha"]),
        task=task,
        task_signature=catalog.task_signature(task),
        model_id="test-model",
        prompt_version=server.assessor.PROMPT_VERSION,
        schema_version=server.assessor.SCHEMA_VERSION,
        analyzer_version=server.assessor.ANALYZER_VERSION,
        input_fingerprint="server-assessment-input",
        fastcontext_policy="never",
        fastcontext_status="not_requested",
        license_status="unknown",
        recommended_verdict="select",
        final_verdict="select",
        reuse_score=0.9,
        model_confidence=0.9,
        confidence=0.9,
        evidence_coverage=1.0,
        requirement_count=1,
        satisfied_requirement_count=1,
        evidence_requirement_count=1,
        dimensions=AssessmentDimensions(0.9, 0.9, 0.9, 0.1, 0.1),
        adaptation_steps=[
            AdaptationStep(
                "Reuse the data table.",
                ["components/data-table.tsx"],
            )
        ],
        evidence_ledger=[
            {
                "path": "components/data-table.tsx",
                "start_line": 1,
                "end_line": 1,
                "commit_sha": str(asset["commit_sha"]),
                "content_hash": f"sha256:{hashlib.sha256(first_line.encode()).hexdigest()}",
                "validated": True,
            }
        ],
    )
    return catalog.store_reuse_assessment(assessment)


@pytest.mark.asyncio
async def test_reuse_tools_carry_task_signature_and_record_outcomes(tmp_path: Path) -> None:
    asset_id = _create_reusable_asset(tmp_path)

    result = await server.find_reusable_code("Find a reusable data table", max_repos=1)

    assert result.task_signature == catalog.task_signature("Find a reusable data table")
    assert result.results[0].candidate_id == asset_id
    assert result.results[0].task_signature == result.task_signature
    assert "assess_reusable_code(candidate_id, task)" in result.next_steps[0]

    assessment_id = _store_reusable_assessment(asset_id, result.task)
    bundle = await server.get_source_bundle(assessment_id)
    manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
    assert bundle.task_signature == result.task_signature
    assert Path(bundle.bundle_path).parts[-2:] == (asset_id, assessment_id)
    assert bundle.files == ["components/data-table.tsx"]
    assert bundle.recommended_read_order == ["components/data-table.tsx"]
    assert "components/data-table.tsx" in bundle.file_hashes
    assert manifest["task_signature"] == result.task_signature
    assert manifest["assessment_id"] == assessment_id
    assert manifest["copied_files"] == bundle.files
    assert manifest["recommended_read_order"] == bundle.recommended_read_order

    recorded = await server.record_reuse_outcome(
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
async def test_find_reusable_code_profiles_target_project(tmp_path: Path) -> None:
    _create_reusable_asset(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.0.0", "react": "19.0.0"}}),
        encoding="utf-8",
    )
    (target / "app.tsx").write_text("export const App = () => null\n", encoding="utf-8")

    result = await server.find_reusable_code(
        "Find a reusable data table",
        project_path=str(target),
        max_repos=1,
    )

    assert result.target_profile_fingerprint.startswith("sha256:")
    assert result.task_signature == catalog.task_signature(
        result.task,
        result.target_profile_fingerprint,
    )
    assert result.results[0].target_fit_score >= 0.03
    assert result.results[0].target_fit_notes

    without_profile = await server.find_reusable_code(
        "Find a reusable data table",
        project_path="  ",
        max_repos=1,
    )
    assert without_profile.target_profile_fingerprint == ""

    with pytest.raises(ToolError, match="does not exist"):
        await server.find_reusable_code(
            "Find a reusable data table",
            project_path=str(tmp_path / "missing"),
        )


@pytest.mark.asyncio
async def test_reuse_tools_advertise_local_mutations() -> None:
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    for name in ("find_reusable_code", "get_source_bundle", "record_reuse_outcome"):
        assert not bool(getattr(tools[name].annotations, "readOnlyHint", False))
        assert "local" in str(tools[name].description).lower()

    assert "source bundle" in str(tools["get_source_bundle"].description).lower()


@pytest.mark.asyncio
async def test_reuse_tools_require_assessment_and_task_signature(tmp_path: Path) -> None:
    asset_id = _create_reusable_asset(tmp_path)

    with pytest.raises(ToolError, match="assessment_id is required"):
        await server.get_source_bundle("")

    with pytest.raises(ToolError, match="task_signature is required"):
        await server.record_reuse_outcome(asset_id, "", "selected")
