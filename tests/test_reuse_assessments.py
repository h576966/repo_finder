from pathlib import Path

import pytest

from source_scout import assessment_rules, catalog
from source_scout.models import (
    AdaptationStep,
    AssessmentDimensions,
    CouplingRisk,
    EvidenceBackedReason,
    MissingEvidenceRequest,
    RequirementAssessment,
    ReuseAssessmentResult,
)


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(tmp_path / ".source_scout"))
    catalog.reset_connection()
    yield
    catalog.reset_connection()


def _assessment(
    *,
    created_at: str = "2026-06-22T10:00:00+00:00",
    final_verdict: str = assessment_rules.VERDICT_INSPECT,
) -> ReuseAssessmentResult:
    dimensions = AssessmentDimensions(
        functional_fit=0.8,
        extractability=0.7,
        dependency_fit=0.6,
        coupling_risk=0.2,
        maintenance_risk=0.3,
    )
    requirements = [
        RequirementAssessment(
            requirement="has reusable route handler",
            satisfied=True,
            status="satisfied",
            evidence_paths=["src/app/api/users/route.ts:1-60"],
        ),
        RequirementAssessment(
            requirement="uses zod validation",
            satisfied=False,
            status="unknown",
            evidence_paths=[],
            notes=["No schema evidence found."],
        ),
    ]
    score = assessment_rules.score_assessment(
        dimensions,
        requirements,
        model_confidence=0.9,
        license_status=assessment_rules.LICENSE_REVIEW_REQUIRED,
    )
    return ReuseAssessmentResult(
        candidate_id="asset-route-handlers",
        repo_id="owner/repo",
        snapshot_id="snapshot-123",
        commit_sha="abc123",
        task="Find a reusable API route handler",
        task_signature="task123",
        model_id="google/gemma-4-12b-qat",
        prompt_version="reuse-assessor-v1",
        schema_version="reuse-assessment-v1",
        analyzer_version="deterministic-assessment-v1",
        input_fingerprint="input123",
        fastcontext_policy="required",
        fastcontext_status="completed",
        license_status=assessment_rules.LICENSE_REVIEW_REQUIRED,
        recommended_verdict=assessment_rules.VERDICT_SELECT,
        final_verdict=final_verdict,
        reuse_score=score.reuse_score,
        model_confidence=score.model_confidence,
        confidence=score.confidence,
        evidence_coverage=score.evidence_coverage,
        requirement_count=score.requirement_count,
        satisfied_requirement_count=score.satisfied_requirement_count,
        evidence_requirement_count=score.evidence_requirement_count,
        dimensions=dimensions,
        requirements=requirements,
        reasons=[
            EvidenceBackedReason(
                reason="Route handler has a compact request/response shape.",
                evidence_paths=["src/app/api/users/route.ts:1-60"],
            )
        ],
        adaptation_steps=[
            AdaptationStep(
                summary="Copy handler shape and replace repository-specific data access.",
                source_paths=["src/app/api/users/route.ts"],
                target_hint="app/api/users/route.ts",
            )
        ],
        coupling_risks=[
            CouplingRisk(
                risk="Data access helper is project-specific.",
                severity="medium",
                evidence_paths=["src/lib/db.ts:1-40"],
                mitigation="Replace with target app database helper.",
            )
        ],
        missing_evidence=[
            MissingEvidenceRequest(
                question="Is validation colocated elsewhere?",
                suggested_paths=["src/lib/validators.ts"],
            )
        ],
        evidence_ledger=[
            {
                "path": "src/app/api/users/route.ts",
                "range": "1-60",
                "source": "fastcontext",
            }
        ],
        validation_notes=["License needs manual review."],
        created_at=created_at,
    )


def test_reuse_assessment_schema_created() -> None:
    conn = catalog.get_connection()

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    columns = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'reuse_assessments'
            """
        ).fetchall()
    }

    assert "reuse_assessments" in tables
    assert {
        "candidate_id",
        "task_signature",
        "input_fingerprint",
        "fastcontext_policy",
        "license_status",
        "recommended_verdict",
        "final_verdict",
        "evidence_ledger",
    }.issubset(columns)


def test_store_and_get_reuse_assessment_round_trip() -> None:
    assessment = _assessment()

    assessment_id = catalog.store_reuse_assessment(assessment)
    stored = catalog.get_reuse_assessment(assessment_id)

    assert stored is not None
    assert stored.assessment_id == assessment_id
    assert stored.candidate_id == assessment.candidate_id
    assert stored.task_signature == assessment.task_signature
    assert stored.dimensions.functional_fit == assessment.dimensions.functional_fit
    assert stored.requirements[0].evidence_paths == ["src/app/api/users/route.ts:1-60"]
    assert stored.requirements[1].status == "unknown"
    assert stored.recommended_verdict == assessment_rules.VERDICT_SELECT
    assert stored.reasons[0].reason.startswith("Route handler")
    assert stored.adaptation_steps[0].target_hint == "app/api/users/route.ts"
    assert stored.coupling_risks[0].mitigation == "Replace with target app database helper."
    assert stored.missing_evidence[0].suggested_paths == ["src/lib/validators.ts"]
    assert stored.evidence_ledger[0]["source"] == "fastcontext"
    assert stored.validation_notes == ["License needs manual review."]


def test_latest_reuse_assessment_preserves_history() -> None:
    first = _assessment(
        created_at="2026-06-22T10:00:00+00:00",
        final_verdict=assessment_rules.VERDICT_INSPECT,
    )
    second = _assessment(
        created_at="2026-06-22T11:00:00+00:00",
        final_verdict=assessment_rules.VERDICT_REJECT,
    )

    first_id = catalog.store_reuse_assessment(first)
    second_id = catalog.store_reuse_assessment(second)
    latest = catalog.get_latest_reuse_assessment(
        "asset-route-handlers",
        "task123",
        "input123",
    )
    row_count = catalog.get_connection().execute(
        """
        SELECT COUNT(*)
        FROM reuse_assessments
        WHERE candidate_id = ? AND task_signature = ? AND input_fingerprint = ?
        """,
        ["asset-route-handlers", "task123", "input123"],
    ).fetchone()[0]

    assert first_id != second_id
    assert row_count == 2
    assert latest is not None
    assert latest.assessment_id == second_id
    assert latest.final_verdict == assessment_rules.VERDICT_REJECT
