from dataclasses import dataclass
from typing import Any


class FastContextError(RuntimeError):
    pass


class FastContextLoopError(FastContextError):
    def __init__(self, message: str, trajectory: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.trajectory = trajectory


@dataclass(frozen=True)
class FastContextCitation:
    path: str
    start_line: int | None = None
    end_line: int | None = None
    reason: str = ""

    def evidence_path(self) -> str:
        if self.start_line is None:
            return self.path
        end_line = self.end_line if self.end_line is not None else self.start_line
        return f"{self.path}:{self.start_line}-{max(self.start_line, end_line)}"


@dataclass(frozen=True)
class ParsedFastContextResponse:
    tool_calls: list[dict[str, Any]]
    citations: list[FastContextCitation]
    citation_ids: list[str]
    notes: list[str]


@dataclass(frozen=True)
class ObservationSupport:
    files: set[str]
    ranges: dict[str, list[tuple[int, int]]]


@dataclass(frozen=True)
class EvidenceBudgetResult:
    evidence_paths: list[str]
    notes: list[str]
    over_budget: bool
    truncated: bool
    original_count: int
    accepted_count: int
    original_file_count: int
    accepted_file_count: int


@dataclass(frozen=True)
class FastContextLoopResult:
    status: str
    evidence_paths: list[str]
    notes: list[str]
    trajectory: list[dict[str, Any]]
