from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class RateLimitError(Exception):
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class ImplementationReference:
    reference_id: str
    snapshot_id: str
    repo_id: str
    commit_sha: str
    path: str
    content_sha256: str
    relevance_score: float
    matched_terms: list[str]
    source_kind: str
    selection_kind: str
    origin: str
    permalink: str | None
    manifest_paths: list[str] = field(default_factory=list)
    repository_facts: dict[str, Any] = field(default_factory=dict)
    target_fit: dict[str, Any] = field(default_factory=dict)


@dataclass
class FindImplementationReferencesResult:
    task: str
    status: str
    results: list[ImplementationReference]
    abstention_reason: str | None = None
    target_profile_fingerprint: str = ""
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    schema_version: str = "implementation-references-v2"


@dataclass
class ImplementationReferenceSnippet:
    path: str
    start_line: int
    end_line: int
    content: str
    content_sha256: str
    permalink: str | None = None
    normalization: str = "utf8-replace-crlf-to-lf-no-final-newline"


@dataclass
class ImplementationReferenceContext:
    reference_id: str
    snapshot_id: str
    repo_id: str
    commit_sha: str
    source_kind: str
    selection_kind: str
    origin: str
    path: str
    content_sha256: str
    snippets: list[ImplementationReferenceSnippet]
    manifests: list[dict[str, Any]]
    repository_facts: dict[str, Any]
    target_fit: dict[str, Any]
    license: dict[str, Any]
    missing_evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False
    schema_version: str = "implementation-references-v2"
    hash_basis: str = "git-blob-bytes"
    historical_content_sha256: str | None = None


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
    run_id: str | None = None
    report_path: str | None = None
    stop_reason: str | None = None
    missing_context: bool = False
    truncated: bool = False
