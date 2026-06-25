from collections.abc import Mapping, Sequence
from typing import Any

from . import assessment_rules
from .models import (
    AdaptationStep,
    AssessmentDimensions,
    CouplingRisk,
    EvidenceBackedReason,
    MissingEvidenceRequest,
    RequirementAssessment,
)

ALLOWED_MODEL_VERDICTS = {
    assessment_rules.VERDICT_SELECT,
    assessment_rules.VERDICT_INSPECT,
    assessment_rules.VERDICT_REJECT,
    assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE,
}
ALLOWED_REQUIREMENT_STATUSES = {"satisfied", "partial", "unsatisfied", "unknown"}
ALLOWED_RISK_SEVERITIES = {"low", "medium", "high"}
ALLOWED_BLOCKER_TYPES = {
    "license",
    "missing_functionality",
    "unsupported_stack",
    "excessive_coupling",
    "other",
}
ALLOWED_RETRIEVERS = {"deterministic", "fastcontext"}
ALLOWED_PRIORITIES = {"low", "medium", "high"}


class AssessorError(RuntimeError):
    pass


def _normalize_response(
    response: Mapping[str, Any],
    evidence_id_map: Mapping[str, str],
) -> dict[str, Any]:
    errors: list[str] = []
    verdict = _string(response.get("recommended_verdict", response.get("recommendation_verdict")))
    if verdict not in ALLOWED_MODEL_VERDICTS:
        errors.append(f"Invalid recommended_verdict: {verdict}")

    dimensions_raw = response.get("dimension_scores")
    if not isinstance(dimensions_raw, Mapping):
        dimensions_raw = {}
    dimensions = AssessmentDimensions(
        functional_fit=assessment_rules.clamp_dimension_score(
            _float_value(dimensions_raw.get("functional_fit"))
        ),
        extractability=assessment_rules.clamp_dimension_score(
            _float_value(dimensions_raw.get("extractability"))
        ),
        dependency_fit=assessment_rules.clamp_dimension_score(
            _float_value(dimensions_raw.get("dependency_fit"))
        ),
        coupling_risk=assessment_rules.clamp_dimension_score(
            _float_value(dimensions_raw.get("coupling_risk"))
        ),
        maintenance_risk=assessment_rules.clamp_dimension_score(
            _float_value(dimensions_raw.get("maintenance_risk"))
        ),
    )

    requirements = _normalize_requirements(response.get("requirement_assessments"), evidence_id_map, errors)
    reasons = _normalize_reasons(response.get("fit_reasons"), evidence_id_map, errors)
    adaptation_steps = _normalize_adaptation_steps(response.get("adaptation_plan"), evidence_id_map, errors)
    coupling_risks = _normalize_coupling_risks(response.get("coupling_risks"), evidence_id_map, errors)
    coupling_risks.extend(_normalize_blockers(response.get("blockers"), evidence_id_map, errors))
    missing_evidence = _normalize_missing_evidence(response.get("missing_evidence"), errors)
    if errors:
        raise AssessorError("; ".join(errors))
    return {
        "model_recommended_verdict": verdict,
        "model_confidence": assessment_rules.clamp_dimension_score(
            _float_value(response.get("model_confidence"))
        ),
        "dimensions": dimensions,
        "requirements": requirements,
        "reasons": reasons,
        "adaptation_steps": adaptation_steps,
        "coupling_risks": coupling_risks,
        "missing_evidence": missing_evidence,
        "needs_fastcontext": bool(response.get("needs_fastcontext", False)),
    }


def _normalize_requirements(
    raw: Any,
    evidence_id_map: Mapping[str, str],
    errors: list[str],
) -> list[RequirementAssessment]:
    items = _list_of_mappings(raw, "requirement_assessments", errors)
    requirements: list[RequirementAssessment] = []
    for item in items:
        requirement = _string(item.get("requirement")).strip()
        status = _string(item.get("status"))
        if status not in ALLOWED_REQUIREMENT_STATUSES:
            errors.append(f"Invalid requirement status: {status}")
        evidence_paths = _evidence_paths(item.get("evidence_ids"), evidence_id_map, errors)
        if status in {"satisfied", "partial"} and not evidence_paths:
            errors.append(f"Requirement '{requirement}' has status '{status}' without evidence_ids.")
        requirements.append(
            RequirementAssessment(
                requirement=requirement,
                satisfied=status == "satisfied",
                status=status,
                evidence_paths=evidence_paths,
                notes=[f"status: {status}"],
            )
        )
    return requirements


def _normalize_reasons(
    raw: Any,
    evidence_id_map: Mapping[str, str],
    errors: list[str],
) -> list[EvidenceBackedReason]:
    reasons: list[EvidenceBackedReason] = []
    for item in _list_of_mappings(raw, "fit_reasons", errors):
        text = _string(item.get("text")).strip()
        evidence_paths = _evidence_paths(item.get("evidence_ids"), evidence_id_map, errors)
        if text and not evidence_paths:
            errors.append(f"Fit reason '{text}' is missing evidence_ids.")
        reasons.append(EvidenceBackedReason(reason=text, evidence_paths=evidence_paths))
    return reasons


def _normalize_adaptation_steps(
    raw: Any,
    evidence_id_map: Mapping[str, str],
    errors: list[str],
) -> list[AdaptationStep]:
    steps: list[AdaptationStep] = []
    for item in _list_of_mappings(raw, "adaptation_plan", errors):
        step = _string(item.get("step")).strip()
        evidence_paths = _evidence_paths(item.get("evidence_ids"), evidence_id_map, errors)
        if step and not evidence_paths:
            errors.append(f"Adaptation step '{step}' is missing evidence_ids.")
        steps.append(
            AdaptationStep(
                summary=step,
                source_paths=sorted({_citation_path(path) for path in evidence_paths}),
            )
        )
    return steps


def _normalize_coupling_risks(
    raw: Any,
    evidence_id_map: Mapping[str, str],
    errors: list[str],
) -> list[CouplingRisk]:
    risks: list[CouplingRisk] = []
    for item in _list_of_mappings(raw, "coupling_risks", errors):
        risk = _string(item.get("risk")).strip()
        severity = _string(item.get("severity"))
        if severity not in ALLOWED_RISK_SEVERITIES:
            errors.append(f"Invalid coupling risk severity: {severity}")
            severity = "medium"
        evidence_paths = _evidence_paths(item.get("evidence_ids"), evidence_id_map, errors)
        if risk and not evidence_paths:
            errors.append(f"Coupling risk '{risk}' is missing evidence_ids.")
        risks.append(
            CouplingRisk(
                risk=risk,
                severity=severity,
                evidence_paths=evidence_paths,
                hard_blocker=False,
            )
        )
    return risks


def _normalize_blockers(
    raw: Any,
    evidence_id_map: Mapping[str, str],
    errors: list[str],
) -> list[CouplingRisk]:
    blockers: list[CouplingRisk] = []
    for item in _list_of_mappings(raw, "blockers", errors):
        blocker_type = _string(item.get("type"))
        if blocker_type not in ALLOWED_BLOCKER_TYPES:
            errors.append(f"Invalid blocker type: {blocker_type}")
            blocker_type = "other"
        text = _string(item.get("text")).strip()
        severity = _blocker_severity(blocker_type, item.get("severity"))
        evidence_paths = _evidence_paths(item.get("evidence_ids"), evidence_id_map, errors)
        hard_blocker = _is_hard_blocker(blocker_type, severity, evidence_paths)
        blockers.append(
            CouplingRisk(
                risk=f"Blocker ({blocker_type}): {text}",
                severity=severity,
                evidence_paths=evidence_paths,
                hard_blocker=hard_blocker,
            )
        )
    return blockers


def _blocker_severity(blocker_type: str, raw_severity: Any) -> str:
    severity = _string(raw_severity)
    if severity in ALLOWED_RISK_SEVERITIES:
        return severity
    if blocker_type in {"missing_functionality", "unsupported_stack", "excessive_coupling"}:
        return "high"
    return "medium"


def _is_hard_blocker(blocker_type: str, severity: str, evidence_paths: Sequence[str]) -> bool:
    if blocker_type == "license":
        return False
    if blocker_type in {"missing_functionality", "unsupported_stack", "excessive_coupling"}:
        return severity == "high" and bool(evidence_paths)
    if blocker_type == "other":
        return severity == "high" and bool(evidence_paths)
    return False


def _normalize_missing_evidence(raw: Any, errors: list[str]) -> list[MissingEvidenceRequest]:
    requests: list[MissingEvidenceRequest] = []
    for item in _list_of_mappings(raw, "missing_evidence", errors):
        question = _string(item.get("question")).strip()
        retriever = _string(item.get("preferred_retriever"))
        priority = _string(item.get("priority"))
        if retriever not in ALLOWED_RETRIEVERS:
            errors.append(f"Invalid missing_evidence preferred_retriever: {retriever}")
        if priority not in ALLOWED_PRIORITIES:
            errors.append(f"Invalid missing_evidence priority: {priority}")
        requests.append(
            MissingEvidenceRequest(
                question=question,
                reason=f"preferred_retriever={retriever}; priority={priority}",
            )
        )
    return requests


def _evidence_paths(raw_ids: Any, evidence_id_map: Mapping[str, str], errors: list[str]) -> list[str]:
    if raw_ids is None:
        return []
    if not isinstance(raw_ids, list):
        errors.append("evidence_ids must be a list.")
        return []
    evidence_paths: list[str] = []
    for raw_id in raw_ids:
        evidence_id = _string(raw_id)
        path = evidence_id_map.get(evidence_id)
        if path is None:
            errors.append(f"Unknown evidence_id: {evidence_id}")
            continue
        if path not in evidence_paths:
            evidence_paths.append(path)
    return evidence_paths


def _list_of_mappings(raw: Any, field_name: str, errors: list[str]) -> list[Mapping[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{field_name} must be a list.")
        return []
    items: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            errors.append(f"{field_name} entries must be objects.")
            continue
        items.append(item)
    return items


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _string(value: Any) -> str:
    return str(value or "").strip()


def _validation_errors(exc: Exception) -> list[str]:
    if isinstance(exc, AssessorError):
        return [str(exc)]
    return [f"{type(exc).__name__}: {exc}"]


def _citation_path(evidence_path: str) -> str:
    if ":" not in evidence_path:
        return evidence_path
    path, _range = evidence_path.rsplit(":", 1)
    return path
