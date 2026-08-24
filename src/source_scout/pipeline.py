import json
import re
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from fastmcp.exceptions import ToolError

from . import catalog_core, catalog_repositories, snapshotter
from .constants import MAX_REPO_AGE_DAYS, MAX_REPOSITORY_SIZE_KB, MAX_STALE_DAYS, SKIP_DIRS
from .github_client import get_client

UI_RECENCY_DAYS = MAX_STALE_DAYS
CARD_VERSION = "repo-card-v1"
SOURCE_EXTENSIONS = {
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".py",
}
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
    "poetry.lock",
    "uv.lock",
    "yarn.lock",
}
PYTHON_MANIFEST_NAMES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "uv.lock",
    "poetry.lock",
    "environment.yml",
    "environment.yaml",
}
PYTHON_AI_DATA_DEPENDENCIES = {
    "anthropic",
    "chromadb",
    "duckdb",
    "fastapi",
    "gradio",
    "instructor",
    "langchain",
    "litellm",
    "llama-index",
    "openai",
    "pandas",
    "polars",
    "pydantic",
    "qdrant-client",
    "sentence-transformers",
    "streamlit",
    "transformers",
    "typer",
}
NODE_AI_DEPENDENCIES = {
    "@ai-sdk/openai",
    "@ai-sdk/react",
    "@ai-sdk/anthropic",
    "@langchain/core",
    "ai",
    "anthropic",
    "langchain",
    "ollama",
    "openai",
}

NEXTJS_UI_CAPABILITIES = {
    "dashboard": "dashboard sidebar admin",
    "forms": "form validation react-hook-form zod",
    "data-table": '"data table" tanstack table',
    "auth-ui": "auth login sign-in",
    "settings": "settings account profile",
    "navigation": "navigation sidebar layout",
    "command-palette": '"command palette" cmdk',
    "file-upload": "upload dropzone file",
    "charts": "charts recharts dashboard",
    "admin-interface": "admin dashboard interface",
}
PERSONAL_CODE_QUERY_PACKS = [
    ("dashboard", "dashboard admin nextjs react tailwind", "TypeScript"),
    ("data-table", '"data table" tanstack table nextjs', "TypeScript"),
    ("forms", "react-hook-form zod nextjs form validation", "TypeScript"),
    ("route-handlers", "nextjs route handler api route", "TypeScript"),
    ("server-actions", '"use server" "server actions" nextjs', "TypeScript"),
    ("data-access", "drizzle prisma database schema nextjs", "TypeScript"),
    ("node-ai-sdk", '"ai sdk" openai anthropic nextjs', "TypeScript"),
    ("model-server-integration", '"openai compatible" lm studio ollama', "TypeScript"),
    ("llm-harness", '"llm" harness structured output tool calling', "Python"),
    ("local-ai-integration", '"local ai" ollama lmstudio llama.cpp', "Python"),
    ("rag-retrieval", "rag retrieval embeddings vector search", "Python"),
    ("eval-harness", "llm eval benchmark golden tasks", "Python"),
    ("data-pipeline", "duckdb polars pandas data pipeline", "Python"),
    ("python-api", "fastapi pydantic openai api", "Python"),
    ("python-cli", "typer cli openai data tool", "Python"),
]
SUPPORTED_SCOUT_DOMAINS = {"nextjs-ui", "personal-code"}


def recency_cutoff(days: int = UI_RECENCY_DAYS) -> str:
    cutoff = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = cutoff - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%d")


def _shared_qualifiers(language: str) -> str:
    created_cutoff = recency_cutoff(MAX_REPO_AGE_DAYS)
    pushed_cutoff = recency_cutoff(MAX_STALE_DAYS)
    return (
        "in:name,description,topics,readme "
        f"language:{language} "
        "archived:false "
        "mirror:false "
        "template:false "
        "is:public "
        f"size:<={MAX_REPOSITORY_SIZE_KB} "
        f"created:>={created_cutoff} "
        f"pushed:>={pushed_cutoff}"
    )


def build_nextjs_ui_queries() -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    qualifiers = _shared_qualifiers("TypeScript")
    for capability, terms in NEXTJS_UI_CAPABILITIES.items():
        queries.append((capability, f"{terms} {qualifiers}"))
    return queries


def build_personal_code_queries() -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    for capability, terms, language in PERSONAL_CODE_QUERY_PACKS:
        queries.append((capability, f"{terms} {_shared_qualifiers(language)}"))
    return queries


def build_domain_queries(domain: str) -> list[tuple[str, str]]:
    if domain == "nextjs-ui":
        return build_nextjs_ui_queries()
    if domain == "personal-code":
        return build_personal_code_queries()
    raise ValueError(f"Unsupported scout domain: {domain}")


async def scout(domain: str, limit: int) -> dict[str, int]:
    if domain not in SUPPORTED_SCOUT_DOMAINS:
        supported = ", ".join(sorted(SUPPORTED_SCOUT_DOMAINS))
        raise ValueError(f"Unsupported scout domain '{domain}'. Supported domains: {supported}.")

    client = get_client()
    seen: set[str] = set()
    stored = 0
    for capability, query in build_domain_queries(domain):
        if stored >= limit:
            break
        per_page = min(100, max(10, limit - stored))
        repos = await client.search_repos(query, per_page=per_page, sort="updated")
        for repo in repos:
            full_name = str(repo.get("full_name", ""))
            if not full_name or full_name in seen:
                continue
            seen.add(full_name)
            catalog_repositories.upsert_repository(repo, f"scout:{domain}:{capability}")
            stored += 1
            if stored >= limit:
                break

    catalog_core.record_analysis_run("scout", "completed", {"domain": domain, "stored": stored})
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


def _normalize_dependency_name(raw: str) -> str:
    value = raw.strip().split(";", 1)[0].strip()
    value = re.split(r"[<>=~! \[]", value, maxsplit=1)[0]
    return value.strip().lower().replace("_", "-")


def _python_dependency_dict(values: list[str]) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for value in values:
        name = _normalize_dependency_name(value)
        if name and not name.startswith("#"):
            dependencies[name] = ""
    return dependencies


def _is_python_manifest(path: Path) -> bool:
    return path.name in PYTHON_MANIFEST_NAMES or (
        path.suffix == ".txt" and path.name.startswith("requirements")
    )


def _python_manifests(root: Path) -> dict[str, Any]:
    manifests: dict[str, Any] = {}
    for path in root.rglob("*"):
        if not path.is_file() or not _is_python_manifest(path):
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.name == "pyproject.toml":
            try:
                parsed = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            project = parsed.get("project", {}) if isinstance(parsed, dict) else {}
            dependencies = list(project.get("dependencies", [])) if isinstance(project, dict) else []
            optional = project.get("optional-dependencies", {}) if isinstance(project, dict) else {}
            dev_dependencies: list[str] = []
            if isinstance(optional, dict):
                for values in optional.values():
                    if isinstance(values, list):
                        dev_dependencies.extend(str(value) for value in values)
            dependency_groups = parsed.get("dependency-groups", {}) if isinstance(parsed, dict) else {}
            if isinstance(dependency_groups, dict):
                for values in dependency_groups.values():
                    if isinstance(values, list):
                        dev_dependencies.extend(str(value) for value in values if isinstance(value, str))
            tool = parsed.get("tool", {}) if isinstance(parsed, dict) else {}
            poetry = tool.get("poetry", {}) if isinstance(tool, dict) else {}
            if isinstance(poetry, dict):
                poetry_deps = poetry.get("dependencies", {})
                if isinstance(poetry_deps, dict):
                    dependencies.extend(str(name) for name in poetry_deps if str(name).lower() != "python")
                poetry_dev = poetry.get("dev-dependencies", {})
                if isinstance(poetry_dev, dict):
                    dev_dependencies.extend(str(name) for name in poetry_dev)
                poetry_groups = poetry.get("group", {})
                if isinstance(poetry_groups, dict):
                    for group in poetry_groups.values():
                        if not isinstance(group, dict):
                            continue
                        group_dependencies = group.get("dependencies", {})
                        if isinstance(group_dependencies, dict):
                            dev_dependencies.extend(str(name) for name in group_dependencies)
            manifests[rel.as_posix()] = {
                "name": project.get("name") if isinstance(project, dict) else None,
                "dependencies": _python_dependency_dict([str(value) for value in dependencies]),
                "devDependencies": _python_dependency_dict(dev_dependencies),
            }
            continue
        if path.name.startswith("requirements") and path.suffix == ".txt":
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            dependencies = [
                line.strip() for line in lines if line.strip() and not line.lstrip().startswith(("#", "-"))
            ]
            manifests[rel.as_posix()] = {
                "name": None,
                "dependencies": _python_dependency_dict(dependencies),
                "devDependencies": {},
            }
            continue
        manifests[rel.as_posix()] = {
            "name": None,
            "dependencies": {},
            "devDependencies": {},
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
    manifests.update(_python_manifests(snapshot_root))
    deps = _dependency_names(manifests)
    lower_deps = {dep.lower() for dep in deps}
    source_files = [p for p in files if Path(p).suffix in SOURCE_EXTENSIONS]
    usable_source_files = [p for p in source_files if not _has_generated_vendor_part(p)]
    tsx_files = [p for p in files if p.endswith(".tsx")]
    js_ts_files = [
        p for p in files if p.endswith((".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"))
    ]
    python_files = [p for p in files if p.endswith(".py")]
    generated_vendor_files = [p for p in files if _has_generated_vendor_part(p)]
    lockfiles = [p for p in files if Path(p).name in LOCKFILE_NAMES]
    python_manifest_paths = [p for p in manifests if _is_python_manifest(Path(p))]
    stack_signals = {
        "has_react_dependency": "react" in deps,
        "has_next_dependency": "next" in deps,
        "has_typescript_files": any(p.endswith((".ts", ".tsx", ".mts", ".cts")) for p in files),
        "has_tsx_files": bool(tsx_files),
        "has_javascript_or_typescript_files": bool(js_ts_files),
        "has_package_manifest": any(Path(path).name == "package.json" for path in manifests),
        "has_node_ai_dependency": bool(NODE_AI_DEPENDENCIES & deps),
        "has_python_files": bool(python_files),
        "has_python_manifest": bool(python_manifest_paths),
        "has_python_ai_data_dependency": bool(PYTHON_AI_DATA_DEPENDENCIES & lower_deps),
        "has_tailwind": "tailwindcss" in deps or any("tailwind.config" in p for p in files),
        "has_radix": any(dep.startswith("@radix-ui/") for dep in deps),
        "has_shadcn_components": "components.json" in files or any("/ui/" in p for p in files),
    }
    deterministic_features = {
        "source_file_count": len(source_files),
        "usable_source_file_count": len(usable_source_files),
        "js_ts_source_file_count": len(js_ts_files),
        "python_source_file_count": len(python_files),
        "tsx_file_count": len(tsx_files),
        "generated_vendor_file_count": len(generated_vendor_files),
        "lockfile_count": len(lockfiles),
        "package_manifest_count": len(manifests),
        "python_manifest_count": len(python_manifest_paths),
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


def _passes_reuse_gates(metadata: dict[str, Any], card: dict[str, Any]) -> tuple[bool, str]:
    ok, reason = _passes_metadata_gates(metadata)
    if not ok:
        return ok, reason

    ok, reason = _passes_snapshot_content_gates(card)
    if not ok:
        return ok, reason

    signals = card["stack_signals"]
    has_react_ts = signals.get("has_react_dependency") and signals.get("has_typescript_files")
    has_next_tsx = signals.get("has_next_dependency") and signals.get("has_tsx_files")
    has_node_source = signals.get("has_package_manifest") and signals.get(
        "has_javascript_or_typescript_files",
    )
    has_node_ai = signals.get("has_node_ai_dependency") and signals.get(
        "has_javascript_or_typescript_files",
    )
    has_python_reuse_source = signals.get("has_python_files") and (
        signals.get("has_python_manifest") or signals.get("has_python_ai_data_dependency")
    )
    if not (has_react_ts or has_next_tsx or has_node_source or has_node_ai or has_python_reuse_source):
        return False, "no reusable TS/JS/Python stack evidence"
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
    for candidate in catalog_repositories.list_repositories_for_qualification(limit):
        repo_id = str(candidate["repo_id"])
        owner = str(candidate["owner"])
        name = str(candidate["name"])
        try:
            metadata = await client.get_repo_metadata(owner, name)
            catalog_repositories.upsert_repository(metadata, "qualify:metadata-refresh")
            ok, reason = _passes_metadata_gates(metadata)
            if not ok:
                skipped += 1
                catalog_core.record_analysis_run(
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
            ok, reason = _passes_reuse_gates(metadata, card)
            if not ok:
                skipped += 1
                catalog_core.record_analysis_run(
                    "qualify",
                    "skipped",
                    {"reason": reason},
                    repo_id=repo_id,
                )
                continue
            snapshot_id = catalog_repositories.upsert_snapshot(
                repo_id, actual_sha, default_branch, local_path
            )
            catalog_repositories.upsert_repository_card(snapshot_id, card)
            catalog_core.record_analysis_run(
                "qualify",
                "completed",
                {"reason": reason},
                repo_id=repo_id,
                snapshot_id=snapshot_id,
            )
            qualified += 1
        except (ToolError, ValueError, OSError) as exc:
            skipped += 1
            catalog_core.record_analysis_run(
                "qualify",
                "failed",
                {"error": str(exc)},
                repo_id=repo_id,
            )
    return {"qualified_repositories": qualified, "skipped_repositories": skipped}


def gc(keep_per_repo: int) -> dict[str, int]:
    return catalog_repositories.garbage_collect_snapshots(keep_per_repo)
