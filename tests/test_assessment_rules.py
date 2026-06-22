import pytest

from source_scout import assessment_rules
from source_scout.models import AssessmentDimensions, CouplingRisk, RequirementAssessment


def _req(
    requirement: str,
    *,
    satisfied: bool = True,
    status: str = "",
    evidence_paths: list[str] | None = None,
) -> RequirementAssessment:
    return RequirementAssessment(
        requirement=requirement,
        satisfied=satisfied,
        status=status,
        evidence_paths=evidence_paths or [],
    )


def test_reuse_score_uses_exact_formula_without_confidence_multiplier() -> None:
    dimensions = AssessmentDimensions(
        functional_fit=0.8,
        extractability=0.7,
        dependency_fit=0.6,
        coupling_risk=0.2,
        maintenance_risk=0.3,
    )
    requirements = [
        _req("has reusable table component", evidence_paths=["src/table.tsx:1-20"]),
        _req("has sorting controls", satisfied=True),
    ]

    assert assessment_rules.calculate_reuse_score(dimensions, requirements) == pytest.approx(
        0.30 * 0.8
        + 0.20 * 0.7
        + 0.15 * 0.6
        + 0.15 * (1 - 0.2)
        + 0.10 * (1 - 0.3)
        + 0.10 * 0.5
    )


def test_requirement_coverage_counts_non_unknown_requirements_with_evidence() -> None:
    requirements = [
        _req("covered", evidence_paths=["src/a.ts:1-3"]),
        _req("claimed only"),
        _req("negative evidence", satisfied=False, evidence_paths=["src/b.ts:4-8"]),
        _req("unknown evidence ignored", satisfied=False, status="unknown", evidence_paths=["src/c.ts:1-2"]),
    ]

    assert assessment_rules.requirement_counts(requirements) == (4, 2, 2)
    assert assessment_rules.calculate_evidence_coverage(requirements) == pytest.approx(0.5)


def test_confidence_is_capped_by_evidence_coverage() -> None:
    assert assessment_rules.cap_confidence(0.95, 0.5) == pytest.approx(0.675)
    assert assessment_rules.cap_confidence(0.4, 0.5) == pytest.approx(0.4)


def test_select_requires_score_confidence_coverage_license_and_no_blockers() -> None:
    dimensions = AssessmentDimensions(
        functional_fit=0.95,
        extractability=0.9,
        dependency_fit=0.9,
        coupling_risk=0.1,
        maintenance_risk=0.1,
    )
    requirements = [
        _req("component", evidence_paths=["src/component.tsx:1-40"]),
        _req("dependencies", evidence_paths=["package.json:1-40"]),
        _req("usage", evidence_paths=["src/page.tsx:1-30"]),
    ]

    assert (
        assessment_rules.recommend_verdict(
            dimensions,
            requirements,
            model_confidence=0.95,
            license_status=assessment_rules.LICENSE_PERMISSIVE_DETECTED,
        )
        == assessment_rules.VERDICT_SELECT
    )
    assert (
        assessment_rules.recommend_verdict(
            dimensions,
            requirements,
            model_confidence=0.95,
            license_status=assessment_rules.LICENSE_UNKNOWN,
        )
        == assessment_rules.VERDICT_INSPECT
    )


def test_verdicts_fail_closed_for_insufficient_evidence_and_reject_blockers() -> None:
    strong_dimensions = AssessmentDimensions(0.9, 0.9, 0.9, 0.1, 0.1)
    weak_dimensions = AssessmentDimensions(0.1, 0.9, 0.9, 0.1, 0.1)

    assert (
        assessment_rules.recommend_verdict(
            strong_dimensions,
            [_req("claimed without evidence")],
            model_confidence=0.9,
            license_status=assessment_rules.LICENSE_PERMISSIVE_DETECTED,
        )
        == assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE
    )
    assert (
        assessment_rules.recommend_verdict(
            weak_dimensions,
            [_req("covered", evidence_paths=["src/a.ts:1-3"])],
            model_confidence=0.9,
            license_status=assessment_rules.LICENSE_PERMISSIVE_DETECTED,
        )
        == assessment_rules.VERDICT_REJECT
    )
    assert (
        assessment_rules.recommend_verdict(
            strong_dimensions,
            [_req("covered", evidence_paths=["src/a.ts:1-3"])],
            model_confidence=0.9,
            license_status=assessment_rules.LICENSE_PERMISSIVE_DETECTED,
            coupling_risks=[CouplingRisk(risk="requires app-specific backend", hard_blocker=True)],
        )
        == assessment_rules.VERDICT_REJECT
    )


def test_severe_risk_without_evidence_is_not_a_hard_blocker() -> None:
    assert not assessment_rules.has_hard_blocker(
        [CouplingRisk(risk="unsupported stack", severity="critical")]
    )
    assert assessment_rules.has_hard_blocker(
        [
            CouplingRisk(
                risk="unsupported stack",
                severity="critical",
                evidence_paths=["src/a.ts:1-3"],
            )
        ]
    )
