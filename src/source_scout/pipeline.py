import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from fastmcp.exceptions import ToolError

from . import catalog, snapshotter
from .constants import MAX_REPO_AGE_DAYS, MAX_REPOSITORY_SIZE_KB, MAX_STALE_DAYS, SKIP_DIRS
from .github_client import get_client

UI_RECENCY_DAYS = MAX_STALE_DAYS
CARD_VERSION = "repo-card-v1"
SOURCE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}
GENERATED_VENDOR_DIRS = {
    "dist",
    "generated",
    "__generated__",
    "vendor",
    "vendors",
}
LOCKFILE_NAMES = {
    "bun.lock",
    "bun.lockb",
    "npm-shrinkwrap.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}

NEXTJS_UI_CAPABILITIES = {
    "dashboard": "dashboard sidebar admin",
    "forms": "form validation react-hook-form zod",
    "data-table": "\"data table\" tanstack table",
    "auth-ui": "auth login sign-in",
    "settings": "settings account profile",
    "navigation": "navigation sidebar layout",
    "command-palette": "\"command palette\" cmdk",
    "file-upload": "upload dropzone file",
    "charts": "charts recharts dashboard",
    "admin-interface": "admin dashboard interface",
}


def recency_cutoff(days: int = UI_RECENCY_DAYS) -> str:
    cutoff = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = cutoff - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%d")


def build_nextjs_ui_queries() -> list[tuple[str, str]]:
    created_cutoff = recency_cutoff(MAX_REPO_AGE_DAYS)
    pushed_cutoff = recency_cutoff(MAX_STALE_DAYS)
    queries: list[tuple[str, str]] = []
    qualifiers = (
        "in:name,description,topics,readme "
        "language:TypeScript "
        "archived:false "
        "mirror:false "
        "template:false "
        "is:public "
        f"size:<={MAX_REPOSITORY_SIZE_KB} "
        f"created:>={created_cutoff} "
        f"pushed:>={pushed_cutoff}"
    )
    for capability, terms in NEXTJS_UI_CAPABILITIES.items():
        queries.append((capability, f"{terms} {qualifiers}"))
    return queries


async def scout(domain: str, limit: int) -> dict[str, int]:
    if domain != "nextjs-ui":
        raise ValueError("Only the 'nextjs-ui' scouting domain is supported in this POC.")

    client = get_client()
    seen: set[str] = set()
    stored = 0
    for capability, query in build_nextjs_ui_queries():
        if stored >= limit:
            break
        per_page = min(100, max(10, limit - stored))
        repos = await client.search_repos(query, per_page=per_page, sort="updated")
        for repo in repos:
            full_name = str(repo.get("full_name", ""))
            if not full_name or full_name in seen:
                continue
            seen.add(full_name)
            catalog.upsert_repository(repo, f"scout:nextjs-ui:{capability}")
            stored += 1
            if stored >= limit:
                break

    catalog.record_analysis_run("scout", "completed", {"domain": domain, "stored": stored})
    return {"stored_repositories": stored}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _within_days(value: str | None, days: int) -> bool:
    parsed = _parse_time(value)
    if parsed is None:
        return False
    cutoff = datetime.now(UTC) - timedelta(days=days)
    return parsed >= cutoff


def _repo_size_kb(metadata: dict[str, Any]) -> int | None:
    raw_size = metadata.get("size")
    if raw_size is None:
        return None
    try:
        return int(raw_size)
    except (TypeError, ValueError):
        return None


def _passes_metadata_gates(metadata: dict[str, Any]) -> tuple[bool, str]:
    if bool(metadata.get("private", False)):
        return False, "not public"
    if bool(metadata.get("archived", False)):
        return False, "archived"
    if metadata.get("mirror_url"):
        return False, "mirror"
    if bool(metadata.get("fork", False)):
        return False, "fork"
    if bool(metadata.get("is_template", False)):
        return False, "template"
    size_kb = _repo_size_kb(metadata)
    if size_kb is not None and size_kb > MAX_REPOSITORY_SIZE_KB:
        return False, "too large"
    if not metadata.get("created_at"):
        return False, "missing created_at"
    if not _within_days(str(metadata.get("created_at") or ""), MAX_REPO_AGE_DAYS):
        return False, "too old"
    if not metadata.get("pushed_at"):
        return False, "missing pushed_at"
    if not _within_days(str(metadata.get("pushed_at") or ""), MAX_STALE_DAYS):
        return False, "stale"
    return True, "metadata qualified"


def _walk_files(root: Path, max_files: int = 2000) -> list[str]:
    files: list[str] = []
    for path in root.rglob("*"):
        if len(files) >= max_files:
            break
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        files.append(rel.as_posix())
    return files


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _package_manifests(root: Path) -> dict[str, Any]:
    manifests: dict[str, Any] = {}
    for path in root.rglob("package.json"):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        parsed = _read_json(path)
        if parsed:
            manifests[rel.as_posix()] = {
                "name": parsed.get("name"),
                "dependencies": parsed.get("dependencies", {}),
                "devDependencies": parsed.get("devDependencies", {}),
            }
    return manifests


def _dependency_names(manifests: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for manifest in manifests.values():
        for section in ("dependencies", "devDependencies"):
            values = manifest.get(section)
            if isinstance(values, dict):
                names.update(str(name) for name in values)
    return names


def _has_generated_vendor_part(rel_path: str) -> bool:
    return bool({part.lower() for part in Path(rel_path).parts} & GENERATED_VENDOR_DIRS)


def build_repository_card(snapshot_root: Path) -> dict[str, Any]:
    files = _walk_files(snapshot_root)
    manifests = _package_manifests(snapshot_root)
    deps = _dependency_names(manifests)
    source_files = [p for p in files if Path(p).suffix in SOURCE_EXTENSIONS]
    usable_source_files = [p for p in source_files if not _has_generated_vendor_part(p)]
    tsx_files = [p for p in files if p.endswith(".tsx")]
    generated_vendor_files = [p for p in files if _has_generated_vendor_part(p)]
    lockfiles = [p for p in files if Path(p).name in LOCKFILE_NAMES]
    stack_signals = {
        "has_react_dependency": "react" in deps,
        "has_next_dependency": "next" in deps,
        "has_typescript_files": any(p.endswith((".ts", ".tsx")) for p in files),
        "has_tsx_files": bool(tsx_files),
        "has_tailwind": "tailwindcss" in deps or any("tailwind.config" in p for p in files),
        "has_radix": any(dep.startswith("@radix-ui/") for dep in deps),
        "has_shadcn_components": "components.json" in files or any("/ui/" in p for p in files),
    }
    deterministic_features = {
        "source_file_count": len(source_files),
        "usable_source_file_count": len(usable_source_files),
        "tsx_file_count": len(tsx_files),
        "generated_vendor_file_count": len(generated_vendor_files),
        "lockfile_count": len(lockfiles),
        "package_manifest_count": len(manifests),
        "top_level_files": [p for p in files if "/" not in p][:40],
    }
    return {
        "card_version": CARD_VERSION,
        "package_manifests": manifests,
        "tree_summary": {
            "total_files": len(files),
            "sample_files": files[:120],
        },
        "readme_excerpt": _read_readme_excerpt(snapshot_root),
        "stack_signals": stack_signals,
        "deterministic_features": deterministic_features,
    }


def _read_readme_excerpt(root: Path) -> str | None:
    for name in ("README.md", "readme.md", "Readme.md"):
        path = root / name
        if path.exists():
            try:
                return path.read_text(encoding="utf-8", errors="replace")[:1200]
            except OSError:
                return None
    return None


def _passes_ui_gates(metadata: dict[str, Any], card: dict[str, Any]) -> tuple[bool, str]:
    ok, reason = _passes_metadata_gates(metadata)
    if not ok:
        return ok, reason

    ok, reason = _passes_snapshot_content_gates(card)
    if not ok:
        return ok, reason

    signals = card["stack_signals"]
    has_react_ts = signals["has_react_dependency"] and signals["has_typescript_files"]
    has_next_tsx = signals["has_next_dependency"] and signals["has_tsx_files"]
    if not (has_react_ts or has_next_tsx):
        return False, "no React/TypeScript UI stack evidence"
    return True, "qualified"


def _passes_snapshot_content_gates(card: dict[str, Any]) -> tuple[bool, str]:
    features = card["deterministic_features"]
    source_file_count = int(features["source_file_count"])
    usable_source_file_count = int(features.get("usable_source_file_count", source_file_count))
    total_files = int(card["tree_summary"]["total_files"])
    generated_vendor_file_count = int(features.get("generated_vendor_file_count", 0))
    lockfile_count = int(features.get("lockfile_count", 0))
    if total_files < 1:
        return False, "docs-only or empty"
    if lockfile_count > 0 and source_file_count == 0:
        return False, "lockfile-only"
    if generated_vendor_file_count > 0 and usable_source_file_count < 1:
        return False, "generated/vendor-heavy"
    if usable_source_file_count < 1:
        return False, "docs-only or empty"
    generated_vendor_ratio = generated_vendor_file_count / max(total_files, 1)
    if total_files >= 20 and generated_vendor_ratio >= 0.6:
        return False, "generated/vendor-heavy"
    return True, "qualified"


async def qualify(limit: int) -> dict[str, int]:
    client = get_client()
    qualified = 0
    skipped = 0
    for candidate in catalog.list_repositories_for_qualification(limit):
        repo_id = str(candidate["repo_id"])
        owner = str(candidate["owner"])
        name = str(candidate["name"])
        try:
            metadata = await client.get_repo_metadata(owner, name)
            catalog.upsert_repository(metadata, "qualify:metadata-refresh")
            ok, reason = _passes_metadata_gates(metadata)
            if not ok:
                skipped += 1
                catalog.record_analysis_run(
                    "qualify",
                    "skipped",
                    {"reason": reason},
                    repo_id=repo_id,
                )
                continue
            default_branch = str(metadata.get("default_branch") or candidate.get("default_branch") or "main")
            commit_sha = await client.get_default_branch_commit(owner, name, default_branch)
            local_path, actual_sha = snapshotter.clone_snapshot(
                repo_url=str(metadata.get("html_url") or f"https://github.com/{repo_id}"),
                owner=owner,
                repo=name,
                commit_sha=commit_sha,
                default_branch=default_branch,
            )
            card = build_repository_card(local_path)
            ok, reason = _passes_ui_gates(metadata, card)
            if not ok:
                skipped += 1
                catalog.record_analysis_run(
                    "qualify",
                    "skipped",
                    {"reason": reason},
                    repo_id=repo_id,
                )
                continue
            snapshot_id = catalog.upsert_snapshot(repo_id, actual_sha, default_branch, local_path)
            catalog.upsert_repository_card(snapshot_id, card)
            catalog.record_analysis_run(
                "qualify",
                "completed",
                {"reason": reason},
                repo_id=repo_id,
                snapshot_id=snapshot_id,
            )
            qualified += 1
        except (ToolError, ValueError, OSError) as exc:
            skipped += 1
            catalog.record_analysis_run(
                "qualify",
                "failed",
                {"error": str(exc)},
                repo_id=repo_id,
            )
    return {"qualified_repositories": qualified, "skipped_repositories": skipped}


def gc(keep_per_repo: int) -> dict[str, int]:
    return catalog.garbage_collect_snapshots(keep_per_repo)
