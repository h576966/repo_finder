import hashlib
import json
import shutil
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastmcp.exceptions import ToolError

from . import assessment_rules, catalog
from .bundle_closure import BundleClosureError, BundleClosurePlan, plan_bundle_closure
from .constants import _now_iso
from .models import ReuseAssessmentResult, SourceBundleResult

BUNDLE_SCHEMA_VERSION = "source-bundle-v2"
MAX_TOTAL_FILES = 10
MAX_TOTAL_BYTES = 512 * 1024


def _safe_source_path(root: Path, rel_path: str) -> Path:
    relative = Path(rel_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ToolError(f"Unsafe source path in bundle: {rel_path}")
    source = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if source != root_resolved and root_resolved not in source.parents:
        raise ToolError(f"Unsafe source path in bundle: {rel_path}")
    return source


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_generated_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _replace_bundle_directory(staging_root: Path, bundle_root: Path) -> None:
    if not bundle_root.exists():
        staging_root.replace(bundle_root)
        return

    backup_root = bundle_root.parent / f".{bundle_root.name}.backup-{uuid4().hex}"
    bundle_root.replace(backup_root)
    try:
        staging_root.replace(bundle_root)
    except OSError:
        backup_root.replace(bundle_root)
        raise
    _remove_generated_path(backup_root)


def _unique_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def create_source_bundle(assessment_id: str) -> SourceBundleResult:
    if not assessment_id.strip():
        raise ToolError("assessment_id is required.")
    assessment = catalog.get_reuse_assessment(assessment_id)
    if assessment is None:
        raise ToolError(f"Unknown assessment_id: {assessment_id}")
    _validate_assessment(assessment)

    candidate_id = assessment.candidate_id
    asset = catalog.get_asset_detail(candidate_id)
    if asset is None:
        raise ToolError(f"Unknown candidate_id: {candidate_id}")
    _validate_assessment_asset(assessment, asset)

    required_seeds = _assessment_source_paths(assessment)
    if not required_seeds:
        raise ToolError("Assessment has no validated adaptation source paths; reassess the candidate.")

    snapshot_root = Path(str(asset["snapshot_path"]))
    _validate_adaptation_evidence(assessment, snapshot_root, required_seeds)
    try:
        closure = plan_bundle_closure(snapshot_root, required_seeds)
    except BundleClosureError as exc:
        raise ToolError(str(exc)) from exc

    bundle_mode = (
        "assessment" if assessment.final_verdict == assessment_rules.VERDICT_SELECT else "inspection"
    )
    unresolved = [
        f"{item.importer} -> {item.specifier} ({item.reason})" for item in closure.unresolved_local_imports
    ]
    blocking_unresolved = [
        item for item in closure.unresolved_local_imports if item.reason != "path_alias_not_supported"
    ]
    warnings = list(closure.warnings)
    if len(blocking_unresolved) != len(closure.unresolved_local_imports):
        warnings.append("Path aliases were reported without guessing their target files.")
    if bundle_mode == "inspection":
        warnings.insert(0, "Inspection bundle: assessment requires review before reuse.")
    elif blocking_unresolved or closure.truncated or closure.warnings:
        blocking_details = [
            f"{item.importer} -> {item.specifier} ({item.reason})" for item in blocking_unresolved
        ]
        details = "; ".join([*blocking_details, *closure.warnings])
        raise ToolError(f"Select bundle dependency closure is incomplete: {details}")

    optional_files, optional_reasons, optional_bytes = _optional_bundle_files(
        snapshot_root,
        closure,
        asset,
    )
    planned_total_bytes = closure.total_bytes + optional_bytes
    required_files = closure.files
    files = [*required_files, *optional_files]

    try:
        bundle_root = catalog.assessment_bundle_path(candidate_id, assessment_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    source_paths = [(rel_path, _safe_source_path(snapshot_root, rel_path)) for rel_path in files]
    bundle_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = bundle_root.parent / f".{bundle_root.name}.staging-{uuid4().hex}"
    source_root = staging_root / "source"
    copied: list[str] = []
    missing: list[str] = []
    file_hashes: dict[str, str] = {}
    try:
        source_root.mkdir(parents=True)
        for rel_path, source in source_paths:
            if not source.exists() or not source.is_file():
                missing.append(rel_path)
                continue
            destination = source_root / rel_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(rel_path)
            file_hashes[rel_path] = _file_sha256(destination)

        missing_required = [path for path in required_files if path not in copied]
        if missing_required:
            raise ToolError(f"Required bundle files disappeared before copy: {missing_required}")
        if missing:
            warnings.append(f"Optional files were missing during copy: {', '.join(missing)}.")
        total_bytes = sum((source_root / path).stat().st_size for path in copied)
        if total_bytes > planned_total_bytes:
            raise ToolError("Bundle files changed size during publication; retry the bundle request.")

        evidence_paths = _assessment_evidence_paths(assessment)
        evidence_file_paths = _unique_paths(
            [str(item.get("path", "")) for item in assessment.evidence_ledger if item.get("path")]
        )
        selection_reasons = {
            **{path: "assessment_adaptation_step" for path in closure.required_files},
            **{path: "local_import_closure" for path in closure.supporting_files},
            **optional_reasons,
        }
        recommended_read_order = [path for path in files if path in copied]
        source_permalinks = {
            path: _source_permalink(str(asset["html_url"]), assessment.commit_sha, path) for path in copied
        }
        adaptation_notes = [step.summary for step in assessment.adaptation_steps if step.summary]
        manifest: dict[str, Any] = {
            "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
            "bundle_mode": bundle_mode,
            "assessment_id": assessment_id,
            "assessment_final_verdict": assessment.final_verdict,
            "assessment_input_fingerprint": assessment.input_fingerprint,
            "assessment_model_id": assessment.model_id,
            "assessment_prompt_version": assessment.prompt_version,
            "assessment_schema_version": assessment.schema_version,
            "assessment_analyzer_version": assessment.analyzer_version,
            "target_profile_fingerprint": assessment.target_profile_fingerprint,
            "candidate_id": candidate_id,
            "task_signature": assessment.task_signature,
            "repo_id": asset["repo_id"],
            "html_url": asset["html_url"],
            "commit_sha": asset["commit_sha"],
            "license_spdx": asset.get("license_spdx"),
            "capability": asset["capability"],
            "source_snapshot": asset["snapshot_path"],
            "copied_files": copied,
            "required_files": required_files,
            "optional_files": optional_files,
            "missing_files": missing,
            "external_dependencies": asset["external_dependencies"],
            "external_dependency_constraints": _external_dependency_constraints(
                str(asset["snapshot_id"]),
                optional_files,
            ),
            "evidence_paths": evidence_paths,
            "evidence_file_paths": evidence_file_paths,
            "adaptation_notes": adaptation_notes,
            "selection_reasons": selection_reasons,
            "closure_depth": 2,
            "closure_truncated": closure.truncated,
            "unresolved_local_imports": [asdict(item) for item in closure.unresolved_local_imports],
            "warnings": warnings,
            "recommended_read_order": recommended_read_order,
            "file_hashes": file_hashes,
            "source_permalinks": source_permalinks,
            "total_bytes": total_bytes,
            "created_at": _now_iso(),
        }
        (staging_root / "bundle.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _replace_bundle_directory(staging_root, bundle_root)
    finally:
        _remove_generated_path(staging_root)

    manifest_path = bundle_root / "bundle.json"

    return SourceBundleResult(
        candidate_id=candidate_id,
        task_signature=assessment.task_signature,
        repo_id=str(asset["repo_id"]),
        commit_sha=str(asset["commit_sha"]),
        bundle_path=str(bundle_root),
        manifest_path=str(manifest_path),
        files=copied,
        missing_files=missing,
        external_dependencies=[str(dep) for dep in asset["external_dependencies"]],
        evidence_paths=evidence_paths,
        evidence_file_paths=evidence_file_paths,
        adaptation_notes=adaptation_notes,
        recommended_read_order=recommended_read_order,
        file_hashes=file_hashes,
        assessment_id=assessment_id,
        bundle_mode=bundle_mode,
        required_files=required_files,
        optional_files=optional_files,
        unresolved_local_imports=unresolved,
        warnings=warnings,
        total_bytes=total_bytes,
        timestamp=_now_iso(),
    )


def _validate_assessment(assessment: ReuseAssessmentResult) -> None:
    from . import assessor

    if assessment.schema_version != assessor.SCHEMA_VERSION:
        raise ToolError("Assessment schema is stale; reassess the candidate before bundling.")
    if assessment.analyzer_version != assessor.ANALYZER_VERSION:
        raise ToolError("Assessment analyzer is stale; reassess the candidate before bundling.")
    if assessment.final_verdict in {
        assessment_rules.VERDICT_REJECT,
        assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE,
    }:
        raise ToolError(f"Assessment verdict {assessment.final_verdict!r} cannot create a bundle.")
    if assessment.final_verdict not in {
        assessment_rules.VERDICT_SELECT,
        assessment_rules.VERDICT_INSPECT,
    }:
        raise ToolError(f"Unsupported assessment verdict: {assessment.final_verdict}")


def _validate_assessment_asset(assessment: ReuseAssessmentResult, asset: dict[str, Any]) -> None:
    mismatches: list[str] = []
    for field, expected in (
        ("candidate_id", assessment.candidate_id),
        ("repo_id", assessment.repo_id),
        ("snapshot_id", assessment.snapshot_id),
        ("commit_sha", assessment.commit_sha),
    ):
        actual_field = "asset_id" if field == "candidate_id" else field
        if str(asset.get(actual_field, "")) != expected:
            mismatches.append(field)
    if mismatches:
        raise ToolError(
            "Assessment no longer matches the current candidate snapshot "
            f"({', '.join(mismatches)}); reassess before bundling."
        )
    latest = catalog.get_latest_snapshot_identity(assessment.repo_id)
    if latest != (assessment.snapshot_id, assessment.commit_sha):
        raise ToolError("Assessment references a superseded catalog snapshot; reassess before bundling.")


def _assessment_source_paths(assessment: ReuseAssessmentResult) -> list[str]:
    allowed = {
        str(item.get("path", "")).replace("\\", "/")
        for item in assessment.evidence_ledger
        if item.get("path") and item.get("validated") is True
    }
    requested = _unique_paths(
        [
            str(path).replace("\\", "/")
            for step in assessment.adaptation_steps
            for path in step.source_paths
            if str(path).strip()
        ]
    )
    invalid = [path for path in requested if path not in allowed]
    if invalid:
        raise ToolError(f"Assessment adaptation paths are not validated evidence: {invalid}")
    return requested


def _validate_adaptation_evidence(
    assessment: ReuseAssessmentResult,
    snapshot_root: Path,
    required_paths: list[str],
) -> None:
    for required_path in required_paths:
        items = [
            item
            for item in assessment.evidence_ledger
            if str(item.get("path", "")).replace("\\", "/") == required_path
        ]
        if not items:
            raise ToolError(f"Assessment adaptation path has no evidence ledger item: {required_path}")
        source = _safe_source_path(snapshot_root, required_path)
        if not source.is_file():
            raise ToolError(f"Assessment adaptation source is missing: {required_path}")
        try:
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise ToolError(f"Could not revalidate assessment evidence: {required_path}") from exc
        for item in items:
            if item.get("validated") is not True:
                raise ToolError(f"Assessment adaptation evidence is not validated: {required_path}")
            if str(item.get("commit_sha", "")) != assessment.commit_sha:
                raise ToolError(f"Assessment adaptation evidence commit is stale: {required_path}")
            try:
                start_line = int(item["start_line"])
                end_line = int(item["end_line"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ToolError(f"Assessment adaptation evidence range is invalid: {required_path}") from exc
            if start_line <= 0 or end_line < start_line or end_line > len(lines):
                raise ToolError(f"Assessment adaptation evidence range is invalid: {required_path}")
            content = "\n".join(lines[start_line - 1 : end_line])
            actual_hash = f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"
            if str(item.get("content_hash", "")) != actual_hash:
                raise ToolError(f"Assessment adaptation evidence hash is stale: {required_path}")


def _assessment_evidence_paths(assessment: ReuseAssessmentResult) -> list[str]:
    paths: list[str] = []
    for item in assessment.evidence_ledger:
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        start = item.get("start_line")
        end = item.get("end_line")
        citation = f"{path}:{start}-{end}" if start is not None and end is not None else path
        paths.append(citation)
    return _unique_paths(paths)


def _optional_bundle_files(
    snapshot_root: Path,
    closure: BundleClosurePlan,
    asset: dict[str, Any],
) -> tuple[list[str], dict[str, str], int]:
    candidates: list[tuple[str, str]] = []
    for path in _nearest_dependency_paths(closure.required_files, asset["dependency_paths"]):
        candidates.append((path, "governing_manifest_or_config"))
    for path in closure.required_files:
        related = _related_test_path(snapshot_root, path)
        if related is not None:
            candidates.append((related, "related_test"))

    selected: list[str] = []
    reasons: dict[str, str] = {}
    selected_bytes = 0
    existing = set(closure.files)
    for path, reason in candidates:
        if path in existing or path in selected:
            continue
        source = _safe_source_path(snapshot_root, path)
        if not source.is_file():
            continue
        size = source.stat().st_size
        if len(closure.files) + len(selected) >= MAX_TOTAL_FILES:
            break
        if closure.total_bytes + selected_bytes + size > MAX_TOTAL_BYTES:
            break
        selected.append(path)
        reasons[path] = reason
        selected_bytes += size
    return selected, reasons, selected_bytes


def _nearest_dependency_paths(required_files: Iterable[str], dependency_paths: Any) -> list[str]:
    available = [str(path).replace("\\", "/") for path in dependency_paths or []]
    selected: list[str] = []
    for required in required_files:
        required_parent = Path(required).parent
        ancestors = [required_parent, *required_parent.parents]
        governing = [
            path for path in available if Path(path).parent == Path(".") or Path(path).parent in ancestors
        ]
        if governing:
            closest = max(governing, key=lambda path: len(Path(path).parent.parts))
            if closest not in selected:
                selected.append(closest)
    return selected


def _related_test_path(snapshot_root: Path, source_path: str) -> str | None:
    source = Path(source_path)
    suffix = source.suffix
    stem = source.stem
    for candidate in (
        source.with_name(f"{stem}.test{suffix}"),
        source.with_name(f"{stem}.spec{suffix}"),
        Path("tests") / source.with_name(f"test_{stem}{suffix}").name,
    ):
        normalized = candidate.as_posix()
        if _safe_source_path(snapshot_root, normalized).is_file():
            return normalized
    return None


def _source_permalink(html_url: str, commit_sha: str, path: str) -> str:
    return f"{html_url.rstrip('/')}/blob/{commit_sha}/{quote(path)}"


def _external_dependency_constraints(snapshot_id: str, manifest_paths: list[str]) -> dict[str, str]:
    card = catalog.get_repository_card_for_snapshot(snapshot_id)
    if card is None:
        return {}
    manifests = card.get("package_manifests", {})
    if not isinstance(manifests, dict):
        return {}
    selected_manifests = set(manifest_paths) & {str(path) for path in manifests}
    if not selected_manifests:
        return {}
    constraints: dict[str, str] = {}
    for path, manifest in manifests.items():
        if str(path) not in selected_manifests:
            continue
        if not isinstance(manifest, dict):
            continue
        for section in ("dependencies", "devDependencies"):
            values = manifest.get(section)
            if isinstance(values, dict):
                constraints.update({str(name): str(value) for name, value in values.items()})
    return constraints
