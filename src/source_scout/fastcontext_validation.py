from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import deepseek
from .fastcontext_constants import (
    FOCUSED_FINAL_CITATION_LINES,
    MAX_CITATION_LINES,
    MAX_FINAL_CITATION_CHOICES,
    MAX_FINAL_CITATIONS,
    MAX_FINAL_FILES,
    PRIORITY_OBSERVATION_PATH_LIMIT,
)
from .fastcontext_tools import (
    _evidence_path_sort_key,
    _has_glob_meta,
    _is_noisy_evidence_path,
    _is_primary_source_path,
    _optional_int,
    _resolve_under_root,
)
from .fastcontext_types import (
    EvidenceBudgetResult,
    FastContextCitation,
    FastContextError,
    ObservationSupport,
    ParsedFastContextResponse,
)


def _observed_citation_choices_text(
    support: ObservationSupport,
    *,
    priority_paths: list[str] | None = None,
) -> str:
    choice_items = _observed_citation_choice_items(support, priority_paths=priority_paths)
    if not choice_items:
        files = "\n".join(
            f"- {path}"
            for path in sorted(
                support.files,
                key=lambda path: _prioritized_path_sort_key(path, priority_paths),
            )[:MAX_FINAL_CITATION_CHOICES]
        )
        if files:
            return (
                "Observed files without line ranges:\n"
                f"{files}\n\n"
                "No valid line ranges have been observed yet. Exact line ranges are required."
            )
        return "Observed citation choices:\n- none"
    formatted = "\n".join(
        f"- {choice_id}: {citation.evidence_path()} ({_citation_choice_label(citation)})"
        for choice_id, citation in choice_items
    )
    return f"Observed citation choices:\n{formatted}"


def _observed_citation_choices(
    support: ObservationSupport,
    limit: int = MAX_FINAL_CITATION_CHOICES,
    priority_paths: list[str] | None = None,
) -> list[str]:
    return [
        citation.evidence_path()
        for _choice_id, citation in _observed_citation_choice_items(
            support,
            limit=limit,
            priority_paths=priority_paths,
        )
    ]


def _observed_citation_choice_items(
    support: ObservationSupport,
    limit: int = MAX_FINAL_CITATION_CHOICES,
    priority_paths: list[str] | None = None,
) -> list[tuple[str, FastContextCitation]]:
    choices: list[tuple[str, FastContextCitation]] = []
    for path in sorted(
        support.ranges,
        key=lambda path: _prioritized_path_sort_key(path, priority_paths),
    ):
        for start, end in sorted(_merge_ranges(support.ranges[path]), key=_range_sort_key):
            choice_id = f"C{len(choices) + 1}"
            choices.append((choice_id, FastContextCitation(path=path, start_line=start, end_line=end)))
            if len(choices) >= limit:
                return choices
    return choices


def _observed_citation_choice_map(
    support: ObservationSupport,
    *,
    priority_paths: list[str] | None = None,
) -> dict[str, FastContextCitation]:
    return {
        choice_id: citation
        for choice_id, citation in _observed_citation_choice_items(
            support,
            priority_paths=priority_paths,
        )
    }


def _observed_priority_paths(
    support: ObservationSupport,
    priority_paths: list[str] | None = None,
) -> list[str]:
    observed = {path.replace("\\", "/") for path in support.ranges}
    paths: list[str] = []
    for priority_path in (priority_paths or [])[:PRIORITY_OBSERVATION_PATH_LIMIT]:
        normalized = priority_path.replace("\\", "/")
        if normalized in observed:
            paths.append(normalized)
    return paths


def _required_observed_priority_path(
    support: ObservationSupport,
    priority_paths: list[str] | None = None,
) -> str:
    observed = _observed_priority_paths(support, priority_paths)
    return observed[0] if observed else ""


def _priority_omission_notes(
    evidence_paths: list[str],
    support: ObservationSupport,
    priority_paths: list[str] | None = None,
) -> list[str]:
    required_path = _required_observed_priority_path(support, priority_paths)
    if not required_path:
        return []
    selected_paths = _citation_files(evidence_paths)
    if required_path in selected_paths:
        return []
    return [f"Final answer omitted observed task-priority path: {required_path}"]


def _priority_observation_evidence_paths(
    support: ObservationSupport,
    priority_paths: list[str] | None = None,
) -> list[str]:
    required_path = _required_observed_priority_path(support, priority_paths)
    if not required_path:
        return []
    return [
        evidence_path
        for evidence_path in _observed_citation_choices(
            support,
            priority_paths=priority_paths,
        )
        if _citation_path(evidence_path) == required_path
    ][:MAX_FINAL_CITATIONS]


def _apply_evidence_budget(
    evidence_paths: list[str],
    *,
    max_citations: int = MAX_FINAL_CITATIONS,
    max_files: int = MAX_FINAL_FILES,
    priority_paths: list[str] | None = None,
) -> EvidenceBudgetResult:
    unique_paths = sorted(
        set(evidence_paths),
        key=lambda path: _evidence_citation_sort_key(path, priority_paths),
    )
    original_count = len(unique_paths)
    original_file_count = len(_citation_files(unique_paths))
    over_budget = original_count > max_citations or original_file_count > max_files
    accepted = unique_paths[:max_citations]
    accepted_file_count = len(_citation_files(accepted))
    truncated = accepted != unique_paths
    notes: list[str] = []
    if over_budget:
        notes.append(
            "Citation budget exceeded: "
            f"{original_count} citations across {original_file_count} files; "
            f"maximum is {max_citations} citations across {max_files} files."
        )
    if truncated:
        notes.append(
            f"Citation budget applied: accepted {len(accepted)} citations across {accepted_file_count} files."
        )
    return EvidenceBudgetResult(
        evidence_paths=accepted,
        notes=notes,
        over_budget=over_budget,
        truncated=truncated,
        original_count=original_count,
        accepted_count=len(accepted),
        original_file_count=original_file_count,
        accepted_file_count=accepted_file_count,
    )


def _budget_trace(budget_result: EvidenceBudgetResult) -> dict[str, Any]:
    return {
        "original_count": budget_result.original_count,
        "accepted_count": budget_result.accepted_count,
        "original_file_count": budget_result.original_file_count,
        "accepted_file_count": budget_result.accepted_file_count,
        "over_budget": budget_result.over_budget,
        "truncated": budget_result.truncated,
    }


def _citation_files(evidence_paths: list[str]) -> set[str]:
    return {_citation_path(path) for path in evidence_paths if _citation_path(path)}


def _citation_path(evidence_path: str) -> str:
    match = re.match(r"(?P<path>.+?):\d+(?:-\d+)?$", evidence_path)
    if match:
        return match.group("path")
    return evidence_path


def _evidence_citation_sort_key(
    evidence_path: str,
    priority_paths: list[str] | None = None,
) -> tuple[int, int, str, int, int, str]:
    path = _citation_path(evidence_path)
    start_line = 0
    end_line = 0
    match = re.match(r".+?:(?P<start>\d+)(?:-\d+)?$", evidence_path)
    if match:
        start_line = int(match.group("start"))
        end_match = re.match(r".+?:\d+-(?P<end>\d+)$", evidence_path)
        end_line = int(end_match.group("end")) if end_match else start_line
    priority, normalized = _evidence_path_sort_key(path)
    priority_index = _priority_path_index(path, priority_paths)
    broad_penalty, _range_start, _range_end = _range_sort_key((start_line, end_line or start_line))
    return priority_index, priority, normalized, broad_penalty, start_line, evidence_path


def _prioritized_path_sort_key(
    path: str,
    priority_paths: list[str] | None = None,
) -> tuple[int, int, str]:
    priority, normalized = _evidence_path_sort_key(path)
    return _priority_path_index(path, priority_paths), priority, normalized


def _priority_path_index(path: str, priority_paths: list[str] | None = None) -> int:
    normalized = path.replace("\\", "/")
    for index, priority_path in enumerate(priority_paths or []):
        if normalized == priority_path.replace("\\", "/"):
            return index
    return 10_000


def _citation_choice_label(citation: FastContextCitation) -> str:
    if _is_primary_source_path(citation.path):
        if _is_focused_citation(citation):
            return "primary source, focused"
        return "primary source, broad"
    if _is_noisy_evidence_path(citation.path):
        return "supporting/noisy"
    if _is_focused_citation(citation):
        return "supporting, focused"
    return "supporting, broad"


def _is_focused_citation(citation: FastContextCitation) -> bool:
    span = _citation_line_span(citation)
    return span is not None and span <= FOCUSED_FINAL_CITATION_LINES


def _citation_line_span(citation: FastContextCitation) -> int | None:
    if citation.start_line is None:
        return None
    end_line = citation.end_line if citation.end_line is not None else citation.start_line
    if end_line < citation.start_line:
        return None
    return end_line - citation.start_line + 1


def _fastcontext_response_format() -> dict[str, Any]:
    citation_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["path"],
        "additionalProperties": True,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "fastcontext_response",
            "schema": {
                "type": "object",
                "properties": {
                    "final_answer": {
                        "type": "object",
                        "properties": {
                            "citation_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "evidence": {
                                "type": "array",
                                "items": citation_schema,
                            },
                            "notes": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "additionalProperties": True,
                    },
                    "ok": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
        },
    }


def parse_fastcontext_response(content: str) -> ParsedFastContextResponse:
    try:
        parsed = deepseek.parse_json_content(content)
    except deepseek.ModelError:
        return ParsedFastContextResponse(
            citations=_parse_final_answer_citations(content),
            citation_ids=_parse_final_answer_citation_ids(content),
            notes=[],
        )

    return ParsedFastContextResponse(
        citations=_extract_citations(parsed),
        citation_ids=_extract_citation_ids(parsed),
        notes=_extract_notes(parsed),
    )


def _validated_evidence_paths(
    root: Path,
    citations: list[FastContextCitation],
    observation_support: ObservationSupport | None = None,
) -> tuple[list[str], list[str]]:
    evidence_paths: list[str] = []
    notes: list[str] = []
    for citation in citations:
        shape_note = _citation_shape_note(citation)
        if shape_note is not None:
            notes.append(shape_note)
            continue
        try:
            path, safe_rel = _resolve_under_root(root, citation.path)
        except FastContextError as exc:
            notes.append(f"Skipped invalid citation '{citation.evidence_path()}': {exc}")
            continue
        if not path.is_file():
            notes.append(f"Skipped missing citation file: {safe_rel}")
            continue
        normalized = FastContextCitation(
            path=safe_rel,
            start_line=citation.start_line,
            end_line=citation.end_line,
            reason=citation.reason,
        )
        line_note = _line_validation_note(path, safe_rel, normalized)
        if line_note is not None:
            notes.append(line_note)
            continue
        if observation_support is not None and observation_support.files:
            support_note = _support_validation_note(safe_rel, normalized, observation_support)
            if support_note is not None:
                notes.append(support_note)
                continue
        evidence_paths.append(normalized.evidence_path())
    return sorted(set(evidence_paths)), notes


def _validated_response_evidence_paths(
    root: Path,
    parsed: ParsedFastContextResponse,
    observation_support: ObservationSupport,
    *,
    priority_paths: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    if parsed.citation_ids:
        id_paths, id_notes = _validated_citation_id_paths(
            root,
            parsed.citation_ids,
            observation_support,
            priority_paths=priority_paths,
        )
        notes.extend(id_notes)
        if id_paths:
            return id_paths, notes
    if parsed.citations:
        raw_paths, raw_notes = _validated_evidence_paths(
            root,
            parsed.citations,
            observation_support=observation_support,
        )
        notes.extend(raw_notes)
        return raw_paths, notes
    return [], notes


def _validated_citation_id_paths(
    root: Path,
    citation_ids: list[str],
    observation_support: ObservationSupport,
    *,
    priority_paths: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    id_map = _observed_citation_choice_map(
        observation_support,
        priority_paths=priority_paths,
    )
    citations: list[FastContextCitation] = []
    notes: list[str] = []
    for citation_id in citation_ids:
        normalized = citation_id.strip().upper()
        citation = id_map.get(normalized)
        if citation is None:
            notes.append(f"Skipped unknown citation_id: {citation_id}")
            continue
        citations.append(citation)
    evidence_paths, validation_notes = _validated_evidence_paths(
        root,
        citations,
        observation_support=observation_support,
    )
    notes.extend(validation_notes)
    return evidence_paths, notes


def _citation_shape_note(citation: FastContextCitation) -> str | None:
    path = citation.path.strip()
    if _has_glob_meta(path):
        return f"Skipped wildcard or glob citation: {citation.evidence_path()}"
    if citation.start_line is None or citation.end_line is None:
        return f"Skipped citation without exact line range: {citation.evidence_path()}"
    if path.endswith(("/", "\\")):
        return f"Skipped directory citation: {citation.evidence_path()}"
    return None


def _line_validation_note(path: Path, safe_rel: str, citation: FastContextCitation) -> str | None:
    if citation.start_line is None:
        return None
    start_line = citation.start_line
    end_line = citation.end_line if citation.end_line is not None else start_line
    if start_line <= 0 or end_line <= 0:
        return f"Skipped citation with non-positive line range: {safe_rel}:{start_line}-{end_line}"
    if end_line < start_line:
        return f"Skipped citation with reversed line range: {safe_rel}:{start_line}-{end_line}"
    if end_line - start_line + 1 > MAX_CITATION_LINES:
        return f"Skipped overly broad citation: {safe_rel}:{start_line}-{end_line}"
    line_count = _line_count(path)
    if start_line > line_count or end_line > line_count:
        return (
            f"Skipped citation beyond EOF: {safe_rel}:{start_line}-{end_line} (file has {line_count} lines)"
        )
    return None


def _support_validation_note(
    safe_rel: str,
    citation: FastContextCitation,
    support: ObservationSupport,
) -> str | None:
    if safe_rel not in support.files:
        return f"Skipped unsupported citation file from final answer: {citation.evidence_path()}"
    if citation.start_line is None:
        return None
    ranges = support.ranges.get(safe_rel, [])
    if not ranges:
        return f"Skipped citation without observed line support: {citation.evidence_path()}"
    start_line = citation.start_line
    end_line = citation.end_line if citation.end_line is not None else start_line
    if any(start <= end_line and start_line <= end for start, end in ranges):
        return None
    return f"Skipped citation outside observed line ranges: {citation.evidence_path()}"


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError as exc:
        raise FastContextError(f"Could not read citation file: {path}") from exc


def _observation_support(observations: list[dict[str, Any]]) -> ObservationSupport:
    files: set[str] = set()
    ranges: dict[str, list[tuple[int, int]]] = {}
    for observation in observations:
        if not observation.get("ok"):
            continue
        result = observation.get("result")
        if not isinstance(result, dict):
            continue
        tool = str(observation.get("tool", ""))
        if tool == "Read":
            path = result.get("path")
            if isinstance(path, str):
                files.add(path)
                start = _optional_int(result.get("start_line"))
                end = _optional_int(result.get("end_line"))
                if start is not None and end is not None and end >= start:
                    ranges.setdefault(path, []).append((start, end))
            continue
        matches = result.get("matches")
        if not isinstance(matches, list):
            continue
        for match in matches:
            if isinstance(match, str):
                files.add(match)
                continue
            if not isinstance(match, dict):
                continue
            path = match.get("path")
            if not isinstance(path, str):
                continue
            files.add(path)
            start = _optional_int(match.get("start_line") or match.get("line"))
            end = _optional_int(match.get("end_line") or match.get("line"))
            if start is not None and end is not None and end >= start:
                ranges.setdefault(path, []).append((start, end))
    return ObservationSupport(files=files, ranges=ranges)


def _merge_observation_support(
    current: ObservationSupport,
    incoming: ObservationSupport,
) -> ObservationSupport:
    files = set(current.files)
    files.update(incoming.files)
    ranges = {path: list(path_ranges) for path, path_ranges in current.ranges.items()}
    for path, path_ranges in incoming.ranges.items():
        ranges.setdefault(path, []).extend(path_ranges)
    return ObservationSupport(files=files, ranges=ranges)


def _evidence_from_observation_support(
    support: ObservationSupport,
    limit: int = 5,
    priority_paths: list[str] | None = None,
) -> list[str]:
    evidence: list[str] = []
    for path, path_ranges in sorted(
        support.ranges.items(),
        key=lambda item: _prioritized_path_sort_key(item[0], priority_paths),
    ):
        merged = sorted(_merge_ranges(path_ranges), key=_range_sort_key)
        for start, end in merged:
            evidence.append(f"{path}:{start}-{end}")
            if len(evidence) >= limit:
                return evidence
    if evidence:
        return evidence
    return sorted(
        support.files,
        key=lambda path: _prioritized_path_sort_key(path, priority_paths),
    )[:limit]


def _evidence_from_trajectory(
    trajectory: list[dict[str, Any]],
    limit: int = 5,
) -> list[str]:
    evidence: list[str] = []
    seen: set[str] = set()
    for turn in reversed(trajectory):
        observations = turn.get("tool_observations", [])
        if not isinstance(observations, list):
            continue
        for observation in reversed(observations):
            if not isinstance(observation, dict) or not observation.get("ok"):
                continue
            for citation in _citations_from_observation(observation):
                if citation in seen:
                    continue
                seen.add(citation)
                evidence.append(citation)
                if len(evidence) >= limit:
                    return list(reversed(evidence))
    return list(reversed(evidence))


def _citations_from_observation(observation: dict[str, Any]) -> list[str]:
    result = observation.get("result")
    if not isinstance(result, dict):
        return []
    tool = str(observation.get("tool", ""))
    if tool == "Read":
        path = result.get("path")
        start = _optional_int(result.get("start_line"))
        end = _optional_int(result.get("end_line"))
        if not isinstance(path, str) or start is None or end is None or end < start:
            return []
        capped_end = min(end, start + 79)
        return [f"{path}:{start}-{capped_end}"]
    matches = result.get("matches")
    if not isinstance(matches, list):
        return []
    citations: list[str] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        path = match.get("path")
        start = _optional_int(match.get("start_line") or match.get("line"))
        end = _optional_int(match.get("end_line") or match.get("line"))
        if not isinstance(path, str) or start is None or end is None or end < start:
            continue
        citations.append(f"{path}:{start}-{min(end, start + 79)}")
    return citations


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if start <= 0 or end < start:
            continue
        capped_end = min(end, start + MAX_CITATION_LINES - 1)
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, capped_end))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (
                previous_start,
                min(max(previous_end, capped_end), previous_start + MAX_CITATION_LINES - 1),
            )
    return merged


def _range_sort_key(path_range: tuple[int, int]) -> tuple[int, int, int]:
    start, end = path_range
    line_count = max(0, end - start + 1)
    broad_penalty = 1 if line_count > FOCUSED_FINAL_CITATION_LINES else 0
    return broad_penalty, start, end


def _extract_citations(parsed: dict[str, Any]) -> list[FastContextCitation]:
    final_answer = parsed.get("final_answer") or parsed.get("evidence") or parsed.get("citations")
    evidence: Any
    if isinstance(final_answer, dict):
        evidence = final_answer.get("evidence") or final_answer.get("citations") or []
    else:
        evidence = final_answer
    return _citations_from_value(evidence)


def _extract_citation_ids(parsed: dict[str, Any]) -> list[str]:
    final_answer = parsed.get("final_answer")
    raw_ids: Any
    if isinstance(final_answer, dict):
        raw_ids = final_answer.get("citation_ids") or final_answer.get("evidence_ids") or []
    else:
        raw_ids = parsed.get("citation_ids") or parsed.get("evidence_ids") or []
    return _citation_ids_from_value(raw_ids)


def _extract_notes(parsed: dict[str, Any]) -> list[str]:
    final_answer = parsed.get("final_answer")
    notes = final_answer.get("notes") if isinstance(final_answer, dict) else parsed.get("notes")
    if not isinstance(notes, list):
        return []
    return [str(note) for note in notes]


def _citations_from_value(value: Any) -> list[FastContextCitation]:
    if isinstance(value, str):
        return _parse_citation_lines(value)
    if not isinstance(value, list):
        return []
    citations: list[FastContextCitation] = []
    for item in value:
        if isinstance(item, str):
            citations.extend(_parse_citation_lines(item))
        elif isinstance(item, dict):
            path = str(item.get("path") or item.get("file") or "").strip()
            if not path:
                continue
            start_line = _optional_int(item.get("start_line") or item.get("start"))
            end_line = _optional_int(item.get("end_line") or item.get("end"))
            citations.append(
                FastContextCitation(
                    path=path,
                    start_line=start_line,
                    end_line=end_line,
                    reason=str(item.get("reason", "")),
                )
            )
    return citations


def _citation_ids_from_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return _parse_citation_ids(value)
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        ids.extend(_parse_citation_ids(str(item)))
    return _dedupe_preserve_order(ids)


def _parse_final_answer_citations(content: str) -> list[FastContextCitation]:
    block = re.search(
        r"<final_answer>\s*(?P<body>.*?)\s*</final_answer>",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if block:
        return _parse_citation_lines(block.group("body"))
    return _parse_citation_lines(content)


def _parse_final_answer_citation_ids(content: str) -> list[str]:
    block = re.search(
        r"<final_answer>\s*(?P<body>.*?)\s*</final_answer>",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if block:
        return _parse_citation_ids(block.group("body"))
    return _parse_citation_ids(content)


def _parse_citation_ids(text: str) -> list[str]:
    return _dedupe_preserve_order(
        match.group(0).upper() for match in re.finditer(r"\bC\d+\b", text, flags=re.IGNORECASE)
    )


def _dedupe_preserve_order(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _parse_citation_lines(text: str) -> list[FastContextCitation]:
    citations: list[FastContextCitation] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("-*` ")
        if not line:
            continue
        match = re.search(
            r"(?P<path>[\w./\\()[\]@ -]+\.(?:ts|tsx|js|jsx|json|md|css|scss|mjs|cjs))"
            r"[:#L]+(?P<start>\d+)(?:[-:L]+(?P<end>\d+))?",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        citations.append(
            FastContextCitation(
                path=match.group("path").strip(),
                start_line=int(match.group("start")),
                end_line=int(match.group("end")) if match.group("end") else None,
            )
        )
    return citations
