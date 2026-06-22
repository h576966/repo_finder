from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from source_scout import assessment_rules, assessor, catalog, evidence_ledger, fastcontext, lmstudio, pipeline


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(tmp_path / ".source_scout"))
    catalog.reset_connection()
    yield
    catalog.reset_connection()


def _write_fixture(root: Path, *, with_evidence_file: bool = True) -> None:
    if with_evidence_file:
        (root / "src" / "app" / "api").mkdir(parents=True)
        (root / "src" / "app" / "api" / "route.ts").write_text(
            "\n".join(
                [
                    "import { NextResponse } from 'next/server'",
                    "export async function GET() {",
                    "  return NextResponse.json({ ok: true })",
                    "}",
                ]
            ),
            encoding="utf-8",
        )
        (root / "src" / "lib").mkdir(parents=True)
        (root / "src" / "lib" / "schema.ts").write_text(
            "\n".join(
                [
                    "import { z } from 'zod'",
                    "export const RouteSchema = z.object({ ok: z.boolean() })",
                    "export type RouteSchemaInput = z.infer<typeof RouteSchema>",
                ]
            ),
            encoding="utf-8",
        )
    (root / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"next": "15.0.0", "react": "19.0.0", "zod": "3.0.0"},
                "devDependencies": {"typescript": "5.0.0"},
            }
        ),
        encoding="utf-8",
    )


def _repo_metadata(*, license_spdx: str | None = "MIT") -> dict[str, Any]:
    return {
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
        "license": {"spdx_id": license_spdx} if license_spdx is not None else None,
        "size": 10,
        "created_at": "2026-01-15T00:00:00Z",
        "pushed_at": "2026-06-20T12:00:00Z",
        "topics": ["nextjs"],
    }


def _candidate(
    tmp_path: Path,
    *,
    evidence_paths: list[str] | None = None,
    license_spdx: str | None = "MIT",
) -> str:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    _write_fixture(snapshot_root, with_evidence_file=evidence_paths != [])
    repo_id = catalog.upsert_repository(_repo_metadata(license_spdx=license_spdx), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    card = pipeline.build_repository_card(snapshot_root)
    card["gemma_profile"] = {
        "schema_version": "gemma-profile-v1",
        "repository_type": "reference_application",
        "capabilities": [{"name": "route-handlers", "confidence": 0.8, "evidence": ["src/app/api/route.ts"]}],
        "likely_usefulness": 0.8,
        "extractability": 0.7,
        "maintenance_quality": 0.7,
        "needs_fastcontext": False,
        "concerns": [],
    }
    catalog.upsert_repository_card(snapshot_id, card)
    return catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "route-handlers",
        {
            "entry_paths": ["src/app/api/route.ts"],
            "dependency_paths": ["package.json"],
            "external_dependencies": ["next", "react", "zod"],
            "evidence_paths": ["src/app/api/route.ts:1-4"] if evidence_paths is None else evidence_paths,
            "synthesis": {"adaptation_notes": ["Replace route-specific data access."]},
            "reuse_score": 0.8,
        },
    )


def _first_evidence_id(candidate_id: str, task: str) -> str:
    result = evidence_ledger.build_candidate_evidence_ledger(
        candidate_id,
        task_signature=catalog.task_signature(task),
    )
    return str(result.items[0]["evidence_id"])


def _valid_response(evidence_id: str, **overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "recommended_verdict": "select",
        "model_confidence": 0.95,
        "dimension_scores": {
            "functional_fit": 0.95,
            "extractability": 0.9,
            "dependency_fit": 0.9,
            "coupling_risk": 0.05,
            "maintenance_risk": 0.1,
        },
        "requirement_assessments": [
            {
                "requirement": "Reusable route handler exists",
                "status": "satisfied",
                "evidence_ids": [evidence_id],
            }
        ],
        "fit_reasons": [{"text": "Route handler is compact.", "evidence_ids": [evidence_id]}],
        "adaptation_plan": [{"step": "Copy the route handler shape.", "evidence_ids": [evidence_id]}],
        "coupling_risks": [
            {
                "risk": "Imports are framework-specific.",
                "severity": "low",
                "evidence_ids": [evidence_id],
            }
        ],
        "blockers": [],
        "missing_evidence": [],
        "needs_fastcontext": False,
    }
    response.update(overrides)
    return response


def _allowed_ids(messages: list[dict[str, Any]]) -> list[str]:
    content = str(messages[1]["content"])
    marker = "Context JSON:\n"
    start = content.index(marker) + len(marker)
    end = content.index("\n\nReturn exactly", start)
    payload = json.loads(content[start:end])
    return [str(value) for value in payload["allowed_evidence_ids"]]


def _missing_fastcontext(priority: str = "high") -> list[dict[str, str]]:
    return [
        {
            "question": "Find validation schema evidence for the route handler.",
            "preferred_retriever": "fastcontext",
            "priority": priority,
        }
    ]


@pytest.mark.asyncio
async def test_assess_candidate_normalizes_valid_response_and_records_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    evidence_id = _first_evidence_id(candidate_id, task)

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(evidence_id)

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert result.final_verdict == assessment_rules.VERDICT_SELECT
    assert result.recommended_verdict == assessment_rules.VERDICT_SELECT
    assert result.reuse_score > 0.9
    assert result.confidence == 0.95
    assert result.requirements[0].status == "satisfied"
    assert result.requirements[0].evidence_paths == ["src/app/api/route.ts:1-4"]
    assert result.reasons[0].reason == "Route handler is compact."
    assert result.adaptation_steps[0].source_paths == ["src/app/api/route.ts"]
    assert result.evidence_ledger[0]["evidence_id"] == evidence_id
    assert result.license_status == assessment_rules.LICENSE_PERMISSIVE_DETECTED
    runs = catalog.get_connection().execute(
        "SELECT stage_name, status, model_id FROM analysis_runs WHERE stage_name = 'reuse-assess'"
    ).fetchall()
    assert runs == [("reuse-assess", "completed", lmstudio.DEFAULT_GEMMA_MODEL)]


@pytest.mark.asyncio
async def test_assess_candidate_clamps_numeric_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    evidence_id = _first_evidence_id(candidate_id, task)

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(
            evidence_id,
            model_confidence=2,
            dimension_scores={
                "functional_fit": 2,
                "extractability": -1,
                "dependency_fit": 0.5,
                "coupling_risk": 4,
                "maintenance_risk": -3,
            },
        )

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert result.model_confidence == 1.0
    assert result.dimensions.functional_fit == 1.0
    assert result.dimensions.extractability == 0.0
    assert result.dimensions.coupling_risk == 1.0
    assert result.dimensions.maintenance_risk == 0.0


@pytest.mark.asyncio
async def test_assess_candidate_repairs_invalid_enum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    evidence_id = _first_evidence_id(candidate_id, task)
    calls = 0

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _valid_response(evidence_id, recommended_verdict="maybe")
        return _valid_response(evidence_id)

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert calls == 2
    assert result.final_verdict == assessment_rules.VERDICT_SELECT
    assert any("required repair" in note for note in result.validation_notes)
    assert catalog.get_connection().execute(
        "SELECT status FROM analysis_runs WHERE stage_name = 'reuse-assess'"
    ).fetchall() == [("completed_repaired",)]


@pytest.mark.asyncio
async def test_assess_candidate_falls_back_after_unknown_evidence_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_id = _candidate(tmp_path)

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response("E_unknown")

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(
        candidate_id,
        "Assess reusable route handler",
        fastcontext_policy="never",
    )

    assert result.final_verdict == assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE
    assert result.requirement_count == 0
    assert any("Unknown evidence_id" in note for note in result.validation_notes)
    statuses = catalog.get_connection().execute(
        "SELECT status FROM analysis_runs WHERE stage_name = 'reuse-assess'"
    ).fetchall()
    assert statuses == [("completed_fallback",)]


@pytest.mark.asyncio
async def test_assess_candidate_score_and_verdict_are_not_controlled_by_gemma(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    evidence_id = _first_evidence_id(candidate_id, task)

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(
            evidence_id,
            recommended_verdict="select",
            dimension_scores={
                "functional_fit": 0.1,
                "extractability": 0.9,
                "dependency_fit": 0.9,
                "coupling_risk": 0.0,
                "maintenance_risk": 0.0,
            },
        )

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert result.validation_notes[-1] == "model_recommended_verdict: select"
    assert result.recommended_verdict == assessment_rules.VERDICT_SELECT
    assert result.final_verdict == assessment_rules.VERDICT_REJECT


@pytest.mark.asyncio
async def test_model_verdict_is_stored_separately_from_final_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    evidence_id = _first_evidence_id(candidate_id, task)

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(evidence_id, recommended_verdict="reject")

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert result.recommended_verdict == assessment_rules.VERDICT_REJECT
    assert result.final_verdict == assessment_rules.VERDICT_SELECT
    stored = catalog.get_reuse_assessment(result.assessment_id)
    assert stored is not None
    assert stored.recommended_verdict == assessment_rules.VERDICT_REJECT
    assert stored.final_verdict == assessment_rules.VERDICT_SELECT


@pytest.mark.asyncio
async def test_assess_candidate_license_gate_prevents_select(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path, license_spdx=None)
    evidence_id = _first_evidence_id(candidate_id, task)

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(evidence_id)

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert result.license_status == assessment_rules.LICENSE_MISSING
    assert result.recommended_verdict == assessment_rules.VERDICT_SELECT
    assert result.final_verdict == assessment_rules.VERDICT_INSPECT


@pytest.mark.asyncio
async def test_requirement_status_is_preserved_and_counts_are_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    evidence_id = _first_evidence_id(candidate_id, task)

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(
            evidence_id,
            requirement_assessments=[
                {
                    "requirement": "Route handler exists",
                    "status": "partial",
                    "evidence_ids": [evidence_id],
                },
                {
                    "requirement": "Unknown auth integration",
                    "status": "unknown",
                    "evidence_ids": [],
                },
            ],
        )

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert [item.status for item in result.requirements] == ["partial", "unknown"]
    assert result.satisfied_requirement_count == 0
    assert result.evidence_requirement_count == 1
    assert result.evidence_coverage == 0.5


@pytest.mark.asyncio
async def test_license_blocker_is_left_to_deterministic_license_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    evidence_id = _first_evidence_id(candidate_id, task)

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(
            evidence_id,
            blockers=[
                {
                    "type": "license",
                    "severity": "high",
                    "text": "Review license terms manually.",
                    "evidence_ids": [evidence_id],
                }
            ],
        )

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert result.final_verdict == assessment_rules.VERDICT_SELECT
    assert result.coupling_risks[-1].hard_blocker is False


@pytest.mark.asyncio
async def test_high_evidence_backed_missing_functionality_is_hard_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    evidence_id = _first_evidence_id(candidate_id, task)

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(
            evidence_id,
            blockers=[
                {
                    "type": "missing_functionality",
                    "severity": "high",
                    "text": "The handler lacks the requested validation flow.",
                    "evidence_ids": [evidence_id],
                }
            ],
        )

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert result.final_verdict == assessment_rules.VERDICT_REJECT
    assert result.coupling_risks[-1].hard_blocker is True


@pytest.mark.asyncio
async def test_other_blocker_without_evidence_is_not_hard_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    evidence_id = _first_evidence_id(candidate_id, task)

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(
            evidence_id,
            blockers=[
                {
                    "type": "other",
                    "severity": "high",
                    "text": "Looks unusual but no source evidence was cited.",
                    "evidence_ids": [],
                }
            ],
        )

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert result.final_verdict == assessment_rules.VERDICT_SELECT
    assert result.coupling_risks[-1].hard_blocker is False


@pytest.mark.asyncio
async def test_assess_candidate_cache_hit_and_force_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    evidence_id = _first_evidence_id(candidate_id, task)
    calls = 0

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _valid_response(evidence_id)

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    first = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")
    cached = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")
    forced = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never", force=True)

    assert calls == 2
    assert cached.assessment_id == first.assessment_id
    assert forced.assessment_id != first.assessment_id
    assert catalog.get_connection().execute("SELECT COUNT(*) FROM reuse_assessments").fetchone()[0] == 2
    assert catalog.get_connection().execute(
        "SELECT status FROM analysis_runs WHERE stage_name = 'reuse-assess' ORDER BY created_at"
    ).fetchall() == [("completed",), ("cached",), ("completed",)]


@pytest.mark.asyncio
async def test_assess_candidate_empty_evidence_skips_gemma_and_persists_safe_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    candidate_id = _candidate(tmp_path, evidence_paths=[])

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(
        candidate_id,
        "Assess reusable route handler",
        fastcontext_policy="never",
    )

    assert calls == 0
    assert result.final_verdict == assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE
    assert result.evidence_ledger == []
    assert result.missing_evidence[0].question.startswith("Need validated source evidence")
    assert catalog.get_connection().execute("SELECT COUNT(*) FROM reuse_assessments").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_never_does_not_invoke_or_consume_fastcontext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    catalog.store_evidence_refinement(
        asset_id=candidate_id,
        repo_id="owner/repo",
        snapshot_id=str(catalog.get_asset_detail(candidate_id)["snapshot_id"]),
        task_signature=catalog.task_signature(task),
        capability="route-handlers",
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        prompt_version="fastcontext-refine-v1",
        schema_version="fastcontext-evidence-v1",
        query="prior",
        evidence_paths=["src/lib/schema.ts:1-2"],
        notes=[],
        trajectory=[],
    )

    async def fail_refine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("FastContext should not be invoked")

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(_allowed_ids(kwargs["messages"])[0])

    monkeypatch.setattr(fastcontext, "refine_candidate", fail_refine)
    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="never")

    assert all(item["origins"] == ["deterministic"] for item in result.evidence_ledger)
    assert result.fastcontext_status == "not_requested"


@pytest.mark.asyncio
async def test_auto_only_invokes_fastcontext_for_eligible_missing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    refine_calls = 0

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(
            _allowed_ids(kwargs["messages"])[0],
            missing_evidence=_missing_fastcontext("low"),
            needs_fastcontext=True,
        )

    async def fake_refine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal refine_calls
        refine_calls += 1
        return {}

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)
    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="auto")

    assert refine_calls == 0
    assert result.fastcontext_status == "not_requested"


@pytest.mark.asyncio
async def test_auto_merges_successful_fastcontext_evidence_and_reassesses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    chat_calls = 0
    refine_calls = 0

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal chat_calls
        chat_calls += 1
        ids = _allowed_ids(kwargs["messages"])
        if chat_calls == 1:
            return _valid_response(
                ids[0],
                missing_evidence=_missing_fastcontext("high"),
                needs_fastcontext=True,
            )
        schema_id = ids[-1]
        return _valid_response(schema_id)

    async def fake_refine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal refine_calls
        refine_calls += 1
        assert "Do not decide, score, prove, or reject reusability" in kwargs["task"]
        assert kwargs["task_signature_override"] == catalog.task_signature(task)
        return {
            "refinement_id": "ref-1",
            "analysis_run_id": "run-1",
            "evidence_paths": ["src/lib/schema.ts:1-2"],
            "notes": ["schema found"],
        }

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)
    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="auto")

    assert chat_calls == 2
    assert refine_calls == 1
    assert result.fastcontext_status == "completed"
    assert any(item["path"] == "src/lib/schema.ts" for item in result.evidence_ledger)
    assert any(
        "FastContext refinement completed: refinement_id=ref-1" in note
        for note in result.validation_notes
    )


@pytest.mark.asyncio
async def test_auto_does_not_reuse_fastcontext_evidence_from_another_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    other_task = "Assess reusable auth middleware"
    candidate_id = _candidate(tmp_path)
    catalog.store_evidence_refinement(
        asset_id=candidate_id,
        repo_id="owner/repo",
        snapshot_id=str(catalog.get_asset_detail(candidate_id)["snapshot_id"]),
        task_signature=catalog.task_signature(other_task),
        capability="route-handlers",
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        prompt_version="fastcontext-refine-v1",
        schema_version="fastcontext-evidence-v1",
        query="other task focused query",
        evidence_paths=["src/lib/schema.ts:1-2"],
        notes=[],
        trajectory=[],
    )
    chat_calls = 0
    refine_calls = 0

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal chat_calls
        chat_calls += 1
        ids = _allowed_ids(kwargs["messages"])
        if chat_calls == 1:
            return _valid_response(
                ids[0],
                missing_evidence=_missing_fastcontext("high"),
                needs_fastcontext=True,
            )
        return _valid_response(ids[-1])

    async def fake_refine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal refine_calls
        refine_calls += 1
        assert kwargs["task_signature_override"] == catalog.task_signature(task)
        return {
            "refinement_id": "ref-new",
            "analysis_run_id": "run-new",
            "evidence_paths": ["src/lib/schema.ts:1-2"],
            "notes": [],
        }

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)
    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="auto")

    assert chat_calls == 2
    assert refine_calls == 1
    assert result.fastcontext_status == "completed"


@pytest.mark.asyncio
async def test_auto_reuses_existing_fastcontext_evidence_when_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    catalog.store_evidence_refinement(
        asset_id=candidate_id,
        repo_id="owner/repo",
        snapshot_id=str(catalog.get_asset_detail(candidate_id)["snapshot_id"]),
        task_signature=catalog.task_signature(task),
        capability="route-handlers",
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        prompt_version="fastcontext-refine-v1",
        schema_version="fastcontext-evidence-v1",
        query="prior focused query",
        evidence_paths=["src/lib/schema.ts:1-2"],
        notes=[],
        trajectory=[],
    )
    chat_calls = 0
    refine_calls = 0

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal chat_calls
        chat_calls += 1
        ids = _allowed_ids(kwargs["messages"])
        if chat_calls == 1:
            return _valid_response(
                ids[0],
                missing_evidence=_missing_fastcontext("high"),
                needs_fastcontext=True,
            )
        return _valid_response(ids[-1])

    async def fake_refine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal refine_calls
        refine_calls += 1
        return {}

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)
    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="auto")

    assert chat_calls == 2
    assert refine_calls == 0
    assert result.fastcontext_status == "reused_existing"
    assert any(item["path"] == "src/lib/schema.ts" for item in result.evidence_ledger)
    assert any(
        "FastContext refinement reused:" in note
        for note in result.validation_notes
    )


@pytest.mark.asyncio
async def test_always_attempts_fastcontext_before_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    refine_calls = 0

    async def fake_refine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal refine_calls
        refine_calls += 1
        return {
            "refinement_id": "ref-2",
            "analysis_run_id": "run-2",
            "evidence_paths": ["src/lib/schema.ts:1-2"],
            "notes": [],
        }

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        ids = _allowed_ids(kwargs["messages"])
        return _valid_response(ids[-1])

    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine)
    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="always")

    assert refine_calls == 1
    assert result.fastcontext_status == "completed"
    assert any(item["path"] == "src/lib/schema.ts" for item in result.evidence_ledger)


@pytest.mark.asyncio
async def test_graceful_refinement_failure_keeps_deterministic_score(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    chat_calls = 0

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal chat_calls
        chat_calls += 1
        return _valid_response(
            _allowed_ids(kwargs["messages"])[0],
            missing_evidence=_missing_fastcontext("medium") if chat_calls == 1 else [],
            needs_fastcontext=chat_calls == 1,
        )

    async def fake_refine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("local model unavailable")

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)
    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="auto")

    assert chat_calls == 2
    assert result.fastcontext_status == "failed"
    assert result.confidence == 0.95
    assert any(
        "FastContext refinement failed: local model unavailable" in note
        for note in result.validation_notes
    )


@pytest.mark.asyncio
async def test_round_limit_zero_blocks_fastcontext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    refine_calls = 0

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(
            _allowed_ids(kwargs["messages"])[0],
            missing_evidence=_missing_fastcontext("high"),
            needs_fastcontext=True,
        )

    async def fake_refine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal refine_calls
        refine_calls += 1
        return {}

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)
    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine)

    result = await assessor.assess_candidate(
        candidate_id,
        task,
        fastcontext_policy="auto",
        max_evidence_rounds=0,
    )

    assert refine_calls == 0
    assert result.fastcontext_status == "not_requested"


@pytest.mark.asyncio
async def test_always_zero_rounds_does_not_consume_prior_fastcontext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)
    catalog.store_evidence_refinement(
        asset_id=candidate_id,
        repo_id="owner/repo",
        snapshot_id=str(catalog.get_asset_detail(candidate_id)["snapshot_id"]),
        task_signature=catalog.task_signature(task),
        capability="route-handlers",
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        prompt_version="fastcontext-refine-v1",
        schema_version="fastcontext-evidence-v1",
        query="prior focused query",
        evidence_paths=["src/lib/schema.ts:1-2"],
        notes=[],
        trajectory=[],
    )
    refine_calls = 0

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _valid_response(_allowed_ids(kwargs["messages"])[0])

    async def fake_refine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal refine_calls
        refine_calls += 1
        return {}

    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)
    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine)

    result = await assessor.assess_candidate(
        candidate_id,
        task,
        fastcontext_policy="always",
        max_evidence_rounds=0,
    )

    assert refine_calls == 0
    assert result.fastcontext_status == "not_requested"
    assert all(item["origins"] == ["deterministic"] for item in result.evidence_ledger)


@pytest.mark.asyncio
async def test_fastcontext_evidence_cannot_directly_raise_score(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "Assess reusable route handler"
    candidate_id = _candidate(tmp_path)

    async def fake_refine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "refinement_id": "ref-low",
            "analysis_run_id": "run-low",
            "evidence_paths": ["src/lib/schema.ts:1-2"],
            "notes": [],
        }

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        ids = _allowed_ids(kwargs["messages"])
        return _valid_response(
            ids[-1],
            dimension_scores={
                "functional_fit": 0.1,
                "extractability": 0.1,
                "dependency_fit": 0.1,
                "coupling_risk": 0.0,
                "maintenance_risk": 0.0,
            },
        )

    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine)
    monkeypatch.setattr(assessor.lmstudio, "chat_json", fake_chat_json)

    result = await assessor.assess_candidate(candidate_id, task, fastcontext_policy="always")

    assert result.fastcontext_status == "completed"
    assert result.final_verdict == assessment_rules.VERDICT_REJECT


def test_assess_cli_prints_compact_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import source_scout.__main__ as main_module

    async def fake_assess_candidate(*args: Any, **kwargs: Any) -> object:
        class Result:
            candidate_id = "asset-1"
            final_verdict = "inspect"
            reuse_score = 0.5

        return Result()

    monkeypatch.setattr(assessor, "assess_candidate", fake_assess_candidate)
    monkeypatch.setattr(
        assessor,
        "assessment_to_jsonable",
        lambda result: {
            "candidate_id": result.candidate_id,
            "final_verdict": result.final_verdict,
            "reuse_score": result.reuse_score,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "assess",
            "--candidate-id",
            "asset-1",
            "--task",
            "Find reusable route handler",
        ],
    )

    main_module.main()

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "candidate_id": "asset-1",
        "final_verdict": "inspect",
        "reuse_score": 0.5,
    }
    assert "\n  " not in output


def test_assess_cli_validates_round_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import source_scout.__main__ as main_module

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "assess",
            "--candidate-id",
            "asset-1",
            "--task",
            "Find reusable route handler",
            "--max-evidence-rounds",
            "3",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2
    assert "--max-evidence-rounds must be between 0 and 2" in capsys.readouterr().err


def test_no_stale_repo_finder_naming() -> None:
    stale_terms = ["repo" + "_finder", "REPO" + "_FINDER", "repo" + "-finder", "." + "repo_finder"]
    roots = [Path("src"), Path("tests"), Path("docs"), Path("README.md"), Path("pyproject.toml")]
    offenders: list[str] = []
    for root in roots:
        paths = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in paths:
            if path == Path("tests/test_assessor.py") or "__pycache__" in path.parts:
                continue
            if path.suffix == ".pyc":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(term in text for term in stale_terms):
                offenders.append(path.as_posix())
    assert offenders == []
