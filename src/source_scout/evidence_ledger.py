from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import catalog, path_safety

MAX_LINES_PER_ITEM = 80
MAX_CHARS_PER_ITEM = 8_000
MAX_LEDGER_ITEMS = 24
MAX_TOTAL_PROMPT_CHARS = 24_000
LEDGER_EXTRA_SKIP_DIRS = path_safety.EXTRA_SKIP_DIRS | {"generated", "vendor"}
ORIGIN_ORDER = {
    "deterministic": 0,
    "fastcontext": 1,
    "source_bundle": 2,
}

_CITATION_RE = re.compile(r"^(?P<path>.+):(?P<start>\d+)-(?P<end>\d+)$")


@dataclass(frozen=True)
class EvidenceSource:
    origin: str
    evidence_paths: Sequence[str]


@dataclass(frozen=True)
class EvidenceLedgerResult:
    items: list[dict[str, Any]]
    validation_notes: list[str]
    total_prompt_characters: int
    truncated: bool


def source_from_asset(asset: Mapping[str, Any]) -> EvidenceSource:
    return EvidenceSource(
        origin="deterministic",
        evidence_paths=[str(path) for path in asset.get("evidence_paths", [])],
    )


def source_from_fastcontext(evidence_paths: Sequence[str]) -> EvidenceSource:
    return EvidenceSource(
        origin="fastcontext",
        evidence_paths=[str(path) for path in evidence_paths],
    )


def source_from_bundle_manifest(
    manifest_path: Path,
    *,
    candidate_id: str | None = None,
    commit_sha: str | None = None,
    snapshot_root: Path | None = None,
    task_signature: str | None = None,
) -> tuple[EvidenceSource | None, list[str]]:
    if not manifest_path.exists():
        return None, []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"Skipped unreadable source bundle manifest: {manifest_path} ({exc})"]
    if not isinstance(manifest, dict):
        return None, [f"Skipped source bundle manifest with invalid shape: {manifest_path}"]

    notes: list[str] = []
    if candidate_id is not None and manifest.get("candidate_id") != candidate_id:
        return None, [f"Skipped source bundle manifest for different candidate: {manifest_path}"]
    if commit_sha is not None and manifest.get("commit_sha") != commit_sha:
        return None, [f"Skipped source bundle manifest for different commit: {manifest_path}"]
    if task_signature is not None and manifest.get("task_signature") != task_signature:
        return None, [f"Skipped source bundle manifest for different task: {manifest_path}"]
    if snapshot_root is not None and manifest.get("source_snapshot"):
        try:
            manifest_snapshot = Path(str(manifest["source_snapshot"])).resolve()
            if manifest_snapshot != snapshot_root.resolve():
                return None, [
                    f"Skipped source bundle manifest for different snapshot: {manifest_path}"
                ]
        except OSError as exc:
            return None, [f"Skipped source bundle manifest with invalid snapshot path: {exc}"]

    raw_paths = manifest.get("evidence_paths", [])
    if not isinstance(raw_paths, list):
        notes.append(f"Skipped source bundle manifest evidence paths with invalid shape: {manifest_path}")
        raw_paths = []
    return (
        EvidenceSource(origin="source_bundle", evidence_paths=[str(path) for path in raw_paths]),
        notes,
    )


def build_candidate_evidence_ledger(
    candidate_id: str,
    *,
    task_signature: str | None = None,
    fastcontext_evidence_paths: Sequence[str] = (),
    include_bundle_manifest: bool = True,
) -> EvidenceLedgerResult:
    asset = catalog.get_asset_detail(candidate_id)
    if asset is None:
        raise ValueError(f"Unknown candidate_id: {candidate_id}")

    sources = [source_from_asset(asset)]
    if fastcontext_evidence_paths:
        sources.append(source_from_fastcontext(fastcontext_evidence_paths))

    notes: list[str] = []
    if include_bundle_manifest:
        manifest_source, manifest_notes = source_from_bundle_manifest(
            catalog.bundle_path(candidate_id) / "bundle.json",
            candidate_id=candidate_id,
            commit_sha=str(asset["commit_sha"]),
            snapshot_root=Path(str(asset["snapshot_path"])),
            task_signature=task_signature,
        )
        notes.extend(manifest_notes)
        if manifest_source is not None:
            sources.append(manifest_source)

    result = build_evidence_ledger(
        snapshot_root=Path(str(asset["snapshot_path"])),
        commit_sha=str(asset["commit_sha"]),
        sources=sources,
    )
    return EvidenceLedgerResult(
        items=result.items,
        validation_notes=[*notes, *result.validation_notes],
        total_prompt_characters=result.total_prompt_characters,
        truncated=result.truncated,
    )


def build_evidence_ledger(
    *,
    snapshot_root: Path,
    commit_sha: str,
    sources: Sequence[EvidenceSource],
    max_items: int = MAX_LEDGER_ITEMS,
    max_lines_per_item: int = MAX_LINES_PER_ITEM,
    max_chars_per_item: int = MAX_CHARS_PER_ITEM,
    max_total_prompt_chars: int = MAX_TOTAL_PROMPT_CHARS,
) -> EvidenceLedgerResult:
    root = snapshot_root.resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"snapshot_root must be an existing directory: {snapshot_root}")

    keyed_items: dict[tuple[str, int, int, str], dict[str, Any]] = {}
    validation_notes: list[str] = []
    for source in sources:
        origin = source.origin.strip() or "unknown"
        for evidence_path in source.evidence_paths:
            item, notes = _materialize_item(
                root,
                commit_sha=commit_sha,
                origin=origin,
                evidence_path=str(evidence_path),
                max_lines_per_item=max_lines_per_item,
                max_chars_per_item=max_chars_per_item,
            )
            validation_notes.extend(notes)
            if item is None:
                continue
            key = (
                str(item["path"]),
                int(item["start_line"]),
                int(item["end_line"]),
                str(item["content_hash"]),
            )
            existing = keyed_items.get(key)
            if existing is None:
                keyed_items[key] = item
                continue
            origins = set(existing["origins"])
            origins.update(item["origins"])
            existing["origins"] = _ordered_origins(origins)

    ordered = sorted(keyed_items.values(), key=_item_sort_key)
    budgeted, budget_notes, total_prompt_characters, truncated = _apply_budgets(
        ordered,
        max_items=max_items,
        max_total_prompt_chars=max_total_prompt_chars,
    )
    validation_notes.extend(budget_notes)
    return EvidenceLedgerResult(
        items=budgeted,
        validation_notes=validation_notes,
        total_prompt_characters=total_prompt_characters,
        truncated=truncated,
    )


def _materialize_item(
    root: Path,
    *,
    commit_sha: str,
    origin: str,
    evidence_path: str,
    max_lines_per_item: int,
    max_chars_per_item: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    parsed = _parse_evidence_path(evidence_path)
    if parsed is None:
        return None, [f"Skipped evidence without exact positive line range: {evidence_path}"]
    raw_path, start_line, end_line = parsed
    if path_safety.has_glob_meta(raw_path):
        return None, [f"Skipped wildcard or glob evidence path: {evidence_path}"]
    if raw_path.endswith(("/", "\\")):
        return None, [f"Skipped directory evidence path: {evidence_path}"]
    if start_line <= 0 or end_line <= 0:
        return None, [f"Skipped evidence with non-positive line range: {evidence_path}"]
    if end_line < start_line:
        return None, [f"Skipped evidence with reversed line range: {evidence_path}"]
    if end_line - start_line + 1 > max_lines_per_item:
        return None, [f"Skipped overly broad evidence range: {evidence_path}"]

    try:
        path, safe_rel = path_safety.resolve_under_root(
            root,
            raw_path,
            extra_skip_dirs=LEDGER_EXTRA_SKIP_DIRS,
        )
    except path_safety.PathSafetyError as exc:
        return None, [f"Skipped invalid evidence path '{evidence_path}': {exc}"]

    if not path.is_file():
        return None, [f"Skipped missing evidence file: {safe_rel}"]

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return None, [f"Skipped unreadable evidence file: {safe_rel} ({exc})"]
    if start_line > len(lines) or end_line > len(lines):
        return None, [
            f"Skipped evidence beyond EOF: {safe_rel}:{start_line}-{end_line} "
            f"(file has {len(lines)} lines)"
        ]

    selected_lines = lines[start_line - 1:end_line]
    raw_content = "\n".join(selected_lines)
    content_hash = f"sha256:{hashlib.sha256(raw_content.encode()).hexdigest()}"
    snippet, capped = _numbered_snippet(
        selected_lines,
        start_line=start_line,
        max_chars=max_chars_per_item,
    )
    evidence_id = _evidence_id(
        commit_sha=commit_sha,
        path=safe_rel,
        start_line=start_line,
        end_line=end_line,
        content_hash=content_hash,
    )
    notes = []
    if capped:
        notes.append(f"Capped evidence snippet characters: {safe_rel}:{start_line}-{end_line}")
    return (
        {
            "evidence_id": evidence_id,
            "origins": [origin],
            "path": safe_rel,
            "start_line": start_line,
            "end_line": end_line,
            "commit_sha": commit_sha,
            "content_hash": content_hash,
            "snippet": snippet,
            "validated": True,
        },
        notes,
    )


def _parse_evidence_path(evidence_path: str) -> tuple[str, int, int] | None:
    match = _CITATION_RE.match(evidence_path.strip())
    if match is None:
        return None
    try:
        start_line = int(match.group("start"))
        end_line = int(match.group("end"))
    except ValueError:
        return None
    return match.group("path"), start_line, end_line


def _numbered_snippet(
    lines: Sequence[str],
    *,
    start_line: int,
    max_chars: int,
) -> tuple[str, bool]:
    numbered: list[str] = []
    remaining = max(0, max_chars)
    capped = False
    for index, line in enumerate(lines, start=start_line):
        prefix = f"{index}|"
        text = f"{prefix}{line}"
        separator = "\n" if numbered else ""
        available = remaining - len(separator)
        if available <= 0:
            capped = True
            break
        if len(text) > available:
            numbered.append(f"{separator}{text[:available]}")
            capped = True
            break
        numbered.append(f"{separator}{text}")
        remaining -= len(separator) + len(text)
    return "".join(numbered), capped


def _evidence_id(
    *,
    commit_sha: str,
    path: str,
    start_line: int,
    end_line: int,
    content_hash: str,
) -> str:
    identity = "\0".join([commit_sha, path, str(start_line), str(end_line), content_hash])
    return f"E_{hashlib.sha256(identity.encode()).hexdigest()[:20]}"


def _ordered_origins(origins: set[str]) -> list[str]:
    return sorted(origins, key=lambda origin: (ORIGIN_ORDER.get(origin, 99), origin))


def _item_sort_key(item: Mapping[str, Any]) -> tuple[str, int, int, str]:
    return (
        str(item["path"]),
        int(item["start_line"]),
        int(item["end_line"]),
        str(item["content_hash"]),
    )


def _apply_budgets(
    items: list[dict[str, Any]],
    *,
    max_items: int,
    max_total_prompt_chars: int,
) -> tuple[list[dict[str, Any]], list[str], int, bool]:
    accepted: list[dict[str, Any]] = []
    notes: list[str] = []
    total_chars = 0
    truncated = False
    for item in items:
        if len(accepted) >= max_items:
            truncated = True
            notes.append(f"Skipped evidence after item budget: {item['path']}")
            continue
        snippet_chars = len(str(item["snippet"]))
        if total_chars + snippet_chars > max_total_prompt_chars:
            truncated = True
            notes.append(f"Skipped evidence after prompt character budget: {item['path']}")
            continue
        accepted.append(item)
        total_chars += snippet_chars
    return accepted, notes, total_chars, truncated
