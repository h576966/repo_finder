from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class RateLimitError(Exception):
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class ReusableCandidate:
    candidate_id: str
    repo_id: str
    html_url: str
    commit_sha: str
    capability: str
    score: float
    task_signature: str = ""
    entry_paths: list[str] = field(default_factory=list)
    dependency_paths: list[str] = field(default_factory=list)
    external_dependencies: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    adaptation_notes: list[str] = field(default_factory=list)


@dataclass
class FindReusableCodeResult:
    task: str
    task_signature: str
    total_candidates: int
    results: list[ReusableCandidate]
    timestamp: str
    next_steps: list[str] = field(default_factory=list)


@dataclass
class SourceBundleResult:
    candidate_id: str
    task_signature: str
    repo_id: str
    commit_sha: str
    bundle_path: str
    manifest_path: str
    files: list[str] = field(default_factory=list)
    external_dependencies: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    adaptation_notes: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class RecordReuseOutcomeResult:
    candidate_id: str
    task_signature: str
    outcome: str
    recorded: bool
    timestamp: str


@dataclass
class LocalExploreResult:
    task: str
    project_path: str
    model_id: str
    prompt_version: str
    schema_version: str
    analyzer_version: str
    status: str
    evidence_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tool_trace: list[dict[str, object]] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class AssessmentDimensions:
    functional_fit: float
    extractability: float
    dependency_fit: float
    coupling_risk: float
    maintenance_risk: float


@dataclass
class RequirementAssessment:
    requirement: str
    satisfied: bool
    status: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.status:
            self.status = "satisfied" if self.satisfied else "unsatisfied"


@dataclass
class EvidenceBackedReason:
    reason: str
    evidence_paths: list[str] = field(default_factory=list)


@dataclass
class AdaptationStep:
    summary: str
    source_paths: list[str] = field(default_factory=list)
    target_hint: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class CouplingRisk:
    risk: str
    severity: str = "medium"
    evidence_paths: list[str] = field(default_factory=list)
    mitigation: str = ""
    hard_blocker: bool = False


@dataclass
class MissingEvidenceRequest:
    question: str
    suggested_paths: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class ReuseAssessmentResult:
    candidate_id: str
    repo_id: str
    snapshot_id: str
    commit_sha: str
    task: str
    task_signature: str
    model_id: str
    prompt_version: str
    schema_version: str
    analyzer_version: str
    input_fingerprint: str
    fastcontext_policy: str
    fastcontext_status: str
    license_status: str
    recommended_verdict: str
    final_verdict: str
    reuse_score: float
    model_confidence: float
    confidence: float
    evidence_coverage: float
    requirement_count: int
    satisfied_requirement_count: int
    evidence_requirement_count: int
    dimensions: AssessmentDimensions
    requirements: list[RequirementAssessment] = field(default_factory=list)
    reasons: list[EvidenceBackedReason] = field(default_factory=list)
    adaptation_steps: list[AdaptationStep] = field(default_factory=list)
    coupling_risks: list[CouplingRisk] = field(default_factory=list)
    missing_evidence: list[MissingEvidenceRequest] = field(default_factory=list)
    evidence_ledger: list[dict[str, Any]] = field(default_factory=list)
    validation_notes: list[str] = field(default_factory=list)
    assessment_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
