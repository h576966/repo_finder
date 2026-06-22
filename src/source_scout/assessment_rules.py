from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .models import AssessmentDimensions, CouplingRisk, RequirementAssessment

LICENSE_PERMISSIVE_DETECTED = "permissive_detected"
LICENSE_REVIEW_REQUIRED = "review_required"
LICENSE_MISSING = "missing"
LICENSE_UNKNOWN = "unknown"
LICENSE_STATUSES = {
    LICENSE_PERMISSIVE_DETECTED,
    LICENSE_REVIEW_REQUIRED,
    LICENSE_MISSING,
    LICENSE_UNKNOWN,
}

VERDICT_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
VERDICT_REJECT = "reject"
VERDICT_INSPECT = "inspect"
VERDICT_SELECT = "select"

CONFIDENCE_BASE_CAP = 0.35
CONFIDENCE_EVIDENCE_CAP_WEIGHT = 0.65

SELECT_MIN_SCORE = 0.78
SELECT_MIN_CONFIDENCE = 0.72
SELECT_MIN_EVIDENCE_COVERAGE = 0.70
INSUFFICIENT_EVIDENCE_MAX_COVERAGE = 0.20
REJECT_MAX_FUNCTIONAL_FIT = 0.25
REJECT_MAX_REUSE_SCORE = 0.35


@dataclass(frozen=True)
class AssessmentScore:
    reuse_score: float
    model_confidence: float
    confidence: float
    evidence_coverage: float
    requirement_count: int
    satisfied_requirement_count: int
    evidence_requirement_count: int
    recommended_verdict: str


def clamp_dimension_score(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


def normalize_dimensions(dimensions: AssessmentDimensions) -> AssessmentDimensions:
    return AssessmentDimensions(
        functional_fit=clamp_dimension_score(dimensions.functional_fit),
        extractability=clamp_dimension_score(dimensions.extractability),
        dependency_fit=clamp_dimension_score(dimensions.dependency_fit),
        coupling_risk=clamp_dimension_score(dimensions.coupling_risk),
        maintenance_risk=clamp_dimension_score(dimensions.maintenance_risk),
    )


def requirement_counts(
    requirements: Sequence[RequirementAssessment],
) -> tuple[int, int, int]:
    requirement_count = len(requirements)
    satisfied_requirement_count = sum(1 for item in requirements if item.satisfied)
    evidence_requirement_count = sum(
        1 for item in requirements if item.satisfied and bool(item.evidence_paths)
    )
    return requirement_count, satisfied_requirement_count, evidence_requirement_count


def calculate_evidence_coverage(requirements: Sequence[RequirementAssessment]) -> float:
    requirement_count, _, evidence_requirement_count = requirement_counts(requirements)
    if requirement_count == 0:
        return 0.0
    return evidence_requirement_count / requirement_count


def cap_confidence(model_confidence: float, evidence_coverage: float) -> float:
    model_score = clamp_dimension_score(model_confidence)
    coverage = clamp_dimension_score(evidence_coverage)
    evidence_cap = CONFIDENCE_BASE_CAP + CONFIDENCE_EVIDENCE_CAP_WEIGHT * coverage
    return min(model_score, evidence_cap)


def calculate_reuse_score(
    dimensions: AssessmentDimensions,
    requirements: Sequence[RequirementAssessment],
) -> float:
    normalized = normalize_dimensions(dimensions)
    evidence_coverage = calculate_evidence_coverage(requirements)
    return (
        0.30 * normalized.functional_fit
        + 0.20 * normalized.extractability
        + 0.15 * normalized.dependency_fit
        + 0.15 * (1 - normalized.coupling_risk)
        + 0.10 * (1 - normalized.maintenance_risk)
        + 0.10 * evidence_coverage
    )


def has_hard_blocker(coupling_risks: Sequence[CouplingRisk]) -> bool:
    return any(
        risk.hard_blocker or risk.severity.lower() in {"blocker", "critical"}
        for risk in coupling_risks
    )


def recommend_verdict(
    dimensions: AssessmentDimensions,
    requirements: Sequence[RequirementAssessment],
    model_confidence: float,
    license_status: str,
    coupling_risks: Sequence[CouplingRisk] = (),
) -> str:
    normalized = normalize_dimensions(dimensions)
    evidence_coverage = calculate_evidence_coverage(requirements)
    reuse_score = calculate_reuse_score(normalized, requirements)
    confidence = cap_confidence(model_confidence, evidence_coverage)

    if has_hard_blocker(coupling_risks):
        return VERDICT_REJECT
    if not requirements or evidence_coverage < INSUFFICIENT_EVIDENCE_MAX_COVERAGE:
        return VERDICT_INSUFFICIENT_EVIDENCE
    if normalized.functional_fit <= REJECT_MAX_FUNCTIONAL_FIT or reuse_score < REJECT_MAX_REUSE_SCORE:
        return VERDICT_REJECT
    if (
        reuse_score >= SELECT_MIN_SCORE
        and confidence >= SELECT_MIN_CONFIDENCE
        and evidence_coverage >= SELECT_MIN_EVIDENCE_COVERAGE
        and license_status == LICENSE_PERMISSIVE_DETECTED
    ):
        return VERDICT_SELECT
    return VERDICT_INSPECT


def score_assessment(
    dimensions: AssessmentDimensions,
    requirements: Sequence[RequirementAssessment],
    model_confidence: float,
    license_status: str,
    coupling_risks: Sequence[CouplingRisk] = (),
) -> AssessmentScore:
    requirement_count, satisfied_requirement_count, evidence_requirement_count = requirement_counts(
        requirements
    )
    evidence_coverage = calculate_evidence_coverage(requirements)
    return AssessmentScore(
        reuse_score=calculate_reuse_score(dimensions, requirements),
        model_confidence=clamp_dimension_score(model_confidence),
        confidence=cap_confidence(model_confidence, evidence_coverage),
        evidence_coverage=evidence_coverage,
        requirement_count=requirement_count,
        satisfied_requirement_count=satisfied_requirement_count,
        evidence_requirement_count=evidence_requirement_count,
        recommended_verdict=recommend_verdict(
            dimensions,
            requirements,
            model_confidence,
            license_status,
            coupling_risks,
        ),
    )
