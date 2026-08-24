import hashlib
from dataclasses import asdict
from typing import Any

from .catalog_core import _hash_id, _json_dump, _json_load, get_connection
from .constants import _now_iso
from .models import (
    AdaptationStep,
    AssessmentDimensions,
    CouplingRisk,
    EvidenceBackedReason,
    MissingEvidenceRequest,
    RequirementAssessment,
    ReuseAssessmentResult,
)

ALLOWED_REUSE_OUTCOMES = {
    "returned",
    "opened_bundle",
    "selected",
    "integrated_successfully",
    "rejected_irrelevant",
    "rejected_too_coupled",
    "rejected_low_quality",
}


def record_reuse_outcome(
    asset_id: str | None,
    repo_id: str,
    task_signature: str,
    outcome: str,
    notes: str | None = None,
) -> str:
    if outcome not in ALLOWED_REUSE_OUTCOMES:
        allowed = ", ".join(sorted(ALLOWED_REUSE_OUTCOMES))
        raise ValueError(f"Invalid outcome '{outcome}'. Allowed: {allowed}")
    outcome_id = _hash_id(asset_id or "", repo_id, task_signature, outcome, _now_iso())
    get_connection().execute(
        """
        INSERT INTO reuse_outcomes (
            outcome_id, asset_id, repo_id, task_signature,
            outcome, notes, recorded_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [outcome_id, asset_id, repo_id, task_signature, outcome, notes, _now_iso()],
    )
    return outcome_id


def task_signature(task: str, target_profile_fingerprint: str = "") -> str:
    normalized = " ".join(task.lower().split())
    if target_profile_fingerprint:
        normalized = f"{normalized}\0{target_profile_fingerprint}"
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def store_evidence_refinement(
    *,
    asset_id: str,
    repo_id: str,
    snapshot_id: str,
    task_signature: str,
    parent_task_signature: str | None = None,
    capability: str,
    model_id: str,
    prompt_version: str,
    schema_version: str,
    query: str,
    evidence_paths: list[str],
    notes: list[str],
    trajectory: list[dict[str, Any]],
) -> str:
    stored_task_signature = parent_task_signature or task_signature
    refinement_id = _hash_id(
        asset_id,
        stored_task_signature,
        model_id,
        prompt_version,
        _now_iso(),
    )
    get_connection().execute(
        """
        INSERT INTO evidence_refinements (
            refinement_id, asset_id, repo_id, snapshot_id, task_signature,
            capability, model_id, prompt_version, schema_version, query,
            evidence_paths, notes, trajectory, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            refinement_id,
            asset_id,
            repo_id,
            snapshot_id,
            stored_task_signature,
            capability,
            model_id,
            prompt_version,
            schema_version,
            query,
            _json_dump(evidence_paths),
            _json_dump(notes),
            _json_dump(trajectory),
            _now_iso(),
        ],
    )
    return refinement_id


def list_evidence_refinements(
    asset_id: str,
    *,
    limit: int = 5,
    task_signature: str | None = None,
) -> list[dict[str, Any]]:
    conn = get_connection()
    where = "WHERE asset_id = ?"
    params: list[Any] = [asset_id]
    if task_signature is not None:
        where += " AND task_signature = ?"
        params.append(task_signature)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT *
        FROM evidence_refinements
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    columns = [str(c[0]) for c in conn.description]
    refinements: list[dict[str, Any]] = []
    for row in rows:
        data = dict(zip(columns, row, strict=False))
        for key in ("evidence_paths", "notes", "trajectory"):
            loaded = _json_load(data.get(key), [])
            data[key] = loaded if isinstance(loaded, list) else []
        refinements.append(data)
    return refinements


def _json_dicts(value: str | None) -> list[dict[str, Any]]:
    loaded = _json_load(value, [])
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, dict)]


def _reuse_assessment_from_row(data: dict[str, Any]) -> ReuseAssessmentResult:
    dimensions_data = _json_load(data.get("dimensions"), {})
    if not isinstance(dimensions_data, dict):
        dimensions_data = {}
    dimensions = AssessmentDimensions(
        functional_fit=float(dimensions_data.get("functional_fit", 0.0)),
        extractability=float(dimensions_data.get("extractability", 0.0)),
        dependency_fit=float(dimensions_data.get("dependency_fit", 0.0)),
        coupling_risk=float(dimensions_data.get("coupling_risk", 0.0)),
        maintenance_risk=float(dimensions_data.get("maintenance_risk", 0.0)),
    )
    return ReuseAssessmentResult(
        assessment_id=str(data["assessment_id"]),
        candidate_id=str(data["candidate_id"]),
        repo_id=str(data["repo_id"]),
        snapshot_id=str(data["snapshot_id"]),
        commit_sha=str(data["commit_sha"]),
        task=str(data["task"]),
        task_signature=str(data["task_signature"]),
        model_id=str(data["model_id"]),
        prompt_version=str(data["prompt_version"]),
        schema_version=str(data["schema_version"]),
        analyzer_version=str(data["analyzer_version"]),
        input_fingerprint=str(data["input_fingerprint"]),
        target_profile=_json_load(data.get("target_profile"), {}),
        target_profile_fingerprint=str(data.get("target_profile_fingerprint") or ""),
        fastcontext_policy=str(data["fastcontext_policy"]),
        fastcontext_status=str(data["fastcontext_status"]),
        license_status=str(data["license_status"]),
        recommended_verdict=str(data["recommended_verdict"]),
        final_verdict=str(data["final_verdict"]),
        reuse_score=float(data["reuse_score"]),
        model_confidence=float(data["model_confidence"]),
        confidence=float(data["confidence"]),
        evidence_coverage=float(data["evidence_coverage"]),
        requirement_count=int(data["requirement_count"]),
        satisfied_requirement_count=int(data["satisfied_requirement_count"]),
        evidence_requirement_count=int(data["evidence_requirement_count"]),
        dimensions=dimensions,
        requirements=[
            RequirementAssessment(
                requirement=str(item.get("requirement", "")),
                satisfied=bool(item.get("satisfied", False)),
                status=str(
                    item.get(
                        "status",
                        "satisfied" if bool(item.get("satisfied", False)) else "unsatisfied",
                    )
                ),
                evidence_paths=[str(path) for path in item.get("evidence_paths", [])],
                notes=[str(note) for note in item.get("notes", [])],
            )
            for item in _json_dicts(data.get("requirements"))
        ],
        reasons=[
            EvidenceBackedReason(
                reason=str(item.get("reason", "")),
                evidence_paths=[str(path) for path in item.get("evidence_paths", [])],
            )
            for item in _json_dicts(data.get("reasons"))
        ],
        adaptation_steps=[
            AdaptationStep(
                summary=str(item.get("summary", "")),
                source_paths=[str(path) for path in item.get("source_paths", [])],
                target_hint=str(item.get("target_hint", "")),
                notes=[str(note) for note in item.get("notes", [])],
            )
            for item in _json_dicts(data.get("adaptation_steps"))
        ],
        coupling_risks=[
            CouplingRisk(
                risk=str(item.get("risk", "")),
                severity=str(item.get("severity", "medium")),
                evidence_paths=[str(path) for path in item.get("evidence_paths", [])],
                mitigation=str(item.get("mitigation", "")),
                hard_blocker=bool(item.get("hard_blocker", False)),
            )
            for item in _json_dicts(data.get("coupling_risks"))
        ],
        missing_evidence=[
            MissingEvidenceRequest(
                question=str(item.get("question", "")),
                suggested_paths=[str(path) for path in item.get("suggested_paths", [])],
                reason=str(item.get("reason", "")),
            )
            for item in _json_dicts(data.get("missing_evidence"))
        ],
        evidence_ledger=_json_dicts(data.get("evidence_ledger")),
        validation_notes=[str(note) for note in _json_load(data.get("validation_notes"), [])],
        created_at=str(data["created_at"]),
    )


def store_reuse_assessment(assessment: ReuseAssessmentResult) -> str:
    created_at = assessment.created_at or _now_iso()
    assessment_id = assessment.assessment_id or _hash_id(
        assessment.candidate_id,
        assessment.task_signature,
        assessment.input_fingerprint,
        created_at,
    )
    get_connection().execute(
        """
        INSERT INTO reuse_assessments (
            assessment_id, candidate_id, repo_id, snapshot_id, commit_sha,
            task, task_signature, model_id, prompt_version, schema_version,
            analyzer_version, input_fingerprint, target_profile,
            target_profile_fingerprint, fastcontext_policy, fastcontext_status,
            license_status, recommended_verdict, final_verdict, reuse_score,
            model_confidence, confidence, evidence_coverage, requirement_count,
            satisfied_requirement_count, evidence_requirement_count, dimensions,
            requirements, reasons, adaptation_steps, coupling_risks,
            missing_evidence, evidence_ledger, validation_notes, created_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?
        )
        """,
        [
            assessment_id,
            assessment.candidate_id,
            assessment.repo_id,
            assessment.snapshot_id,
            assessment.commit_sha,
            assessment.task,
            assessment.task_signature,
            assessment.model_id,
            assessment.prompt_version,
            assessment.schema_version,
            assessment.analyzer_version,
            assessment.input_fingerprint,
            _json_dump(assessment.target_profile),
            assessment.target_profile_fingerprint,
            assessment.fastcontext_policy,
            assessment.fastcontext_status,
            assessment.license_status,
            assessment.recommended_verdict,
            assessment.final_verdict,
            assessment.reuse_score,
            assessment.model_confidence,
            assessment.confidence,
            assessment.evidence_coverage,
            assessment.requirement_count,
            assessment.satisfied_requirement_count,
            assessment.evidence_requirement_count,
            _json_dump(asdict(assessment.dimensions)),
            _json_dump([asdict(item) for item in assessment.requirements]),
            _json_dump([asdict(item) for item in assessment.reasons]),
            _json_dump([asdict(item) for item in assessment.adaptation_steps]),
            _json_dump([asdict(item) for item in assessment.coupling_risks]),
            _json_dump([asdict(item) for item in assessment.missing_evidence]),
            _json_dump(assessment.evidence_ledger),
            _json_dump(assessment.validation_notes),
            created_at,
        ],
    )
    return assessment_id


def get_reuse_assessment(assessment_id: str) -> ReuseAssessmentResult | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM reuse_assessments WHERE assessment_id = ?",
        [assessment_id],
    ).fetchone()
    if row is None:
        return None
    columns = [str(c[0]) for c in conn.description]
    return _reuse_assessment_from_row(dict(zip(columns, row, strict=False)))


def get_latest_reuse_assessment(
    candidate_id: str,
    task_signature: str,
    input_fingerprint: str,
) -> ReuseAssessmentResult | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT *
        FROM reuse_assessments
        WHERE candidate_id = ?
            AND task_signature = ?
            AND input_fingerprint = ?
        ORDER BY created_at DESC, assessment_id DESC
        LIMIT 1
        """,
        [candidate_id, task_signature, input_fingerprint],
    ).fetchone()
    if row is None:
        return None
    columns = [str(c[0]) for c in conn.description]
    return _reuse_assessment_from_row(dict(zip(columns, row, strict=False)))
