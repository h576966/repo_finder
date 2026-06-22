import json
import shutil
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError

from . import catalog
from .constants import _now_iso
from .models import SourceBundleResult


def _safe_source_path(root: Path, rel_path: str) -> Path:
    source = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if source != root_resolved and root_resolved not in source.parents:
        raise ToolError(f"Unsafe source path in bundle: {rel_path}")
    return source


def create_source_bundle(candidate_id: str, task_signature: str) -> SourceBundleResult:
    if not task_signature.strip():
        raise ToolError("task_signature is required.")
    asset = catalog.get_asset_detail(candidate_id)
    if asset is None:
        raise ToolError(f"Unknown candidate_id: {candidate_id}")

    bundle_root = catalog.bundle_path(candidate_id)
    source_root = bundle_root / "source"
    source_root.mkdir(parents=True, exist_ok=True)

    snapshot_root = Path(str(asset["snapshot_path"]))
    entry_paths = [str(p) for p in asset["entry_paths"]]
    dependency_paths = [str(p) for p in asset["dependency_paths"]]
    files = sorted(set(entry_paths + dependency_paths))
    copied: list[str] = []
    for rel_path in files:
        source = _safe_source_path(snapshot_root, rel_path)
        if not source.exists() or not source.is_file():
            continue
        destination = source_root / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(rel_path)

    synthesis = asset["synthesis"]
    manifest: dict[str, Any] = {
        "candidate_id": candidate_id,
        "task_signature": task_signature,
        "repo_id": asset["repo_id"],
        "html_url": asset["html_url"],
        "commit_sha": asset["commit_sha"],
        "capability": asset["capability"],
        "source_snapshot": asset["snapshot_path"],
        "files": copied,
        "external_dependencies": asset["external_dependencies"],
        "evidence_paths": asset["evidence_paths"],
        "adaptation_notes": synthesis.get("adaptation_notes", []),
        "created_at": _now_iso(),
    }
    manifest_path = bundle_root / "bundle.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    return SourceBundleResult(
        candidate_id=candidate_id,
        task_signature=task_signature,
        repo_id=str(asset["repo_id"]),
        commit_sha=str(asset["commit_sha"]),
        bundle_path=str(bundle_root),
        manifest_path=str(manifest_path),
        files=copied,
        external_dependencies=[str(dep) for dep in asset["external_dependencies"]],
        evidence_paths=[str(path) for path in asset["evidence_paths"]],
        adaptation_notes=[str(note) for note in synthesis.get("adaptation_notes", [])],
        timestamp=_now_iso(),
    )
