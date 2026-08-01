import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastmcp.exceptions import ToolError

from . import catalog
from .constants import _now_iso
from .models import SourceBundleResult


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


def _evidence_file_path(evidence_path: str) -> str:
    return evidence_path.rsplit(":", 1)[0] if ":" in evidence_path else evidence_path


def _unique_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _recommended_read_order(
    *,
    copied_files: list[str],
    entry_paths: list[str],
    dependency_paths: list[str],
    evidence_file_paths: list[str],
) -> list[str]:
    copied = set(copied_files)
    ordered: list[str] = []
    for path in [
        *evidence_file_paths,
        *entry_paths,
        *dependency_paths,
    ]:
        if path in copied and path not in ordered:
            ordered.append(path)
    return ordered


def create_source_bundle(candidate_id: str, task_signature: str) -> SourceBundleResult:
    if not task_signature.strip():
        raise ToolError("task_signature is required.")
    asset = catalog.get_asset_detail(candidate_id)
    if asset is None:
        raise ToolError(f"Unknown candidate_id: {candidate_id}")

    try:
        bundle_root = catalog.bundle_path(candidate_id, task_signature)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    snapshot_root = Path(str(asset["snapshot_path"]))
    entry_paths = [str(p) for p in asset["entry_paths"]]
    dependency_paths = [str(p) for p in asset["dependency_paths"]]
    evidence_paths = [str(path) for path in asset["evidence_paths"]]
    evidence_file_paths = _unique_paths(
        [path for path in (_evidence_file_path(path) for path in evidence_paths) if path.strip()]
    )
    files = _unique_paths([*entry_paths, *dependency_paths, *evidence_file_paths])
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

        synthesis = asset["synthesis"]
        recommended_read_order = _recommended_read_order(
            copied_files=copied,
            entry_paths=entry_paths,
            dependency_paths=dependency_paths,
            evidence_file_paths=evidence_file_paths,
        )
        manifest: dict[str, Any] = {
            "candidate_id": candidate_id,
            "task_signature": task_signature,
            "repo_id": asset["repo_id"],
            "html_url": asset["html_url"],
            "commit_sha": asset["commit_sha"],
            "capability": asset["capability"],
            "source_snapshot": asset["snapshot_path"],
            "copied_files": copied,
            "missing_files": missing,
            "external_dependencies": asset["external_dependencies"],
            "evidence_paths": evidence_paths,
            "evidence_file_paths": evidence_file_paths,
            "adaptation_notes": synthesis.get("adaptation_notes", []),
            "recommended_read_order": recommended_read_order,
            "file_hashes": file_hashes,
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
        task_signature=task_signature,
        repo_id=str(asset["repo_id"]),
        commit_sha=str(asset["commit_sha"]),
        bundle_path=str(bundle_root),
        manifest_path=str(manifest_path),
        files=copied,
        missing_files=missing,
        external_dependencies=[str(dep) for dep in asset["external_dependencies"]],
        evidence_paths=evidence_paths,
        evidence_file_paths=evidence_file_paths,
        adaptation_notes=[str(note) for note in synthesis.get("adaptation_notes", [])],
        recommended_read_order=recommended_read_order,
        file_hashes=file_hashes,
        timestamp=_now_iso(),
    )
