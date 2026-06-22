import json
from pathlib import Path
from typing import Any

from . import catalog
from .constants import SKIP_DIRS

SOURCE_EXTENSIONS = {".tsx", ".ts", ".jsx", ".js", ".css", ".mdx"}
ENTRY_EXTENSIONS = {".tsx", ".ts", ".jsx", ".js"}
CONFIG_NAMES = {
    "package.json",
    "components.json",
    "tailwind.config.js",
    "tailwind.config.ts",
    "postcss.config.js",
    "tsconfig.json",
}
MAX_FILE_BYTES = 1_000_000

UI_PATH_PARTS = {"app", "pages", "components", "ui", "widgets", "features"}
BACKEND_CAPABILITIES = {
    "route-handlers",
    "server-actions",
    "auth-middleware",
    "trpc-router",
    "data-access",
    "file-storage",
    "email-webhooks",
    "background-jobs",
    "validation-schemas",
    "admin-export",
}
BACKEND_PATH_PARTS = {
    "actions",
    "api",
    "auth",
    "cron",
    "database",
    "db",
    "drizzle",
    "hooks",
    "lib",
    "middleware",
    "migrations",
    "prisma",
    "routers",
    "schemas",
    "server",
    "services",
    "utils",
    "worker",
    "workers",
}
NOISY_PATH_PARTS = {
    "__tests__",
    "api",
    "database",
    "db",
    "drizzle",
    "fixtures",
    "generated",
    "migrations",
    "mock",
    "mocks",
    "prisma",
    "schema",
    "schemas",
    "scripts",
    "server",
    "test",
    "tests",
    "types",
    "worker",
    "workers",
}
NOISY_FILE_STEMS = {
    "config",
    "constants",
    "index",
    "middleware",
    "route",
    "schema",
    "schemas",
    "types",
}
CAPABILITY_PATH_HINTS: dict[str, set[str]] = {
    "data-table": {"data-table", "datatable", "table", "tables", "columns", "grid"},
    "command-palette": {"command", "cmdk", "palette", "combobox", "quick-search"},
    "auth-ui": {"auth", "login", "signin", "signup", "password"},
    "settings": {"settings", "profile", "account", "preferences"},
    "dashboard": {"dashboard", "admin", "overview"},
    "forms": {"form", "forms", "validation"},
    "file-upload": {"upload", "dropzone", "file", "attachment"},
    "charts": {"chart", "charts", "graph", "analytics"},
    "navigation": {"navigation", "navbar", "sidebar", "menu", "layout"},
    "admin-interface": {"admin", "dashboard", "settings", "table"},
    "route-handlers": {"api", "route", "routes", "handler", "handlers", "endpoint"},
    "server-actions": {"actions", "server-action", "use-server"},
    "auth-middleware": {"auth", "session", "sessions", "middleware", "login"},
    "trpc-router": {"trpc", "router", "routers", "procedure"},
    "data-access": {"db", "database", "drizzle", "prisma", "schema", "queries"},
    "file-storage": {
        "attachment",
        "attachments",
        "blob",
        "document",
        "documents",
        "drive",
        "media",
        "r2",
        "s3",
        "storage",
        "upload",
        "uploads",
    },
    "email-webhooks": {"email", "webhook", "message", "handler"},
    "background-jobs": {"worker", "job", "cron", "schedule", "sync", "queue"},
    "validation-schemas": {"validation", "schema", "schemas", "zod", "resolver"},
    "admin-export": {"export", "report", "reports", "pdf", "excel", "xlsx"},
}
CAPABILITY_STRONG_CONTENT: dict[str, set[str]] = {
    "data-table": {"@tanstack/react-table", "usereacttable", "columndef", "getrowmodel"},
    "command-palette": {
        "@base-ui/react",
        "cmdk",
        "comboboxprimitive",
        "commanddialog",
        "commandinput",
    },
    "forms": {"react-hook-form", "useform", "zodresolver"},
    "file-upload": {
        "datatransfer",
        "formdata",
        "input type=\"file\"",
        "ondrop",
        "react-dropzone",
        "setprogress",
        "type=\"file\"",
        "usedropzone",
    },
    "charts": {"recharts", "responsivecontainer", "chartcontainer"},
    "route-handlers": {
        "nextrequest",
        "nextresponse",
        "export async function get",
        "export async function post",
    },
    "server-actions": {"use server", "server action", "revalidatepath", "redirect("},
    "auth-middleware": {"getserversession", "nextauth", "authoptions", "middleware"},
    "trpc-router": {
        "@trpc/server",
        "createtrpc",
        "fetchrequesthandler",
        "inittrpc",
        "protectedprocedure",
        "publicprocedure",
    },
    "data-access": {"drizzle-orm", "pgtable", "prisma", "sql`"},
    "file-storage": {
        "@aws-sdk/client-s3",
        "@vercel/blob",
        "drive.files",
        "file.arraybuffer",
        "formdata",
        "googleapis",
        "multipart",
        "putobjectcommand",
        "s3client",
        "uploadfile",
    },
    "email-webhooks": {"webhook", "email", "message", "mailbox"},
    "background-jobs": {"cron", "queue", "worker", "schedule", "sync"},
    "validation-schemas": {"z.object", "zod", "schema", "resolver"},
    "admin-export": {"jspdf", "xlsx", "export", "report"},
}

CAPABILITY_TERMS: dict[str, list[str]] = {
    "data-table": ["data table", "datatable", "table", "tanstack", "columns"],
    "command-palette": ["command palette", "cmdk", "command", "combobox", "quick search"],
    "auth-ui": ["auth", "login", "sign in", "signin", "signup", "password"],
    "settings": ["settings", "profile", "account", "preferences"],
    "dashboard": ["dashboard", "sidebar", "admin", "overview"],
    "forms": ["form", "react-hook-form", "zod", "validation"],
    "file-upload": ["upload", "dropzone", "file input", "progress", "attachment", "drag", "drop"],
    "charts": ["chart", "recharts", "visx", "nivo", "graph"],
    "navigation": ["navigation", "navbar", "sidebar", "menu"],
    "admin-interface": ["admin", "dashboard", "table", "settings"],
    "route-handlers": ["route handler", "api route", "nextrequest", "nextresponse", "route.ts"],
    "server-actions": [
        "server action",
        "server actions",
        "use server",
        "actions.ts",
        "revalidatepath",
    ],
    "auth-middleware": ["auth", "middleware", "session", "nextauth", "login"],
    "trpc-router": [
        "@trpc/server",
        "inittrpc",
        "protectedprocedure",
        "publicprocedure",
        "trpc router",
        "trpc",
    ],
    "data-access": ["drizzle", "prisma", "database", "schema", "db"],
    "file-storage": [
        "file upload",
        "object storage",
        "blob storage",
        "google drive",
        "drive.files",
        "multipart",
        "formdata",
        "upload",
        "storage",
        "blob",
        "r2",
        "s3",
    ],
    "email-webhooks": ["email", "webhook", "message", "handler"],
    "background-jobs": ["worker", "job", "cron", "schedule", "sync", "queue"],
    "validation-schemas": ["validation", "schema", "zod", "resolver"],
    "admin-export": ["export", "report", "pdf", "excel", "xlsx"],
}

RELEVANT_DEPENDENCIES = {
    "@hookform/resolvers",
    "@base-ui/react",
    "@radix-ui/react-accordion",
    "@radix-ui/react-dialog",
    "@radix-ui/react-dropdown-menu",
    "@radix-ui/react-label",
    "@radix-ui/react-popover",
    "@radix-ui/react-select",
    "@radix-ui/react-slot",
    "@radix-ui/react-tabs",
    "@aws-sdk/client-s3",
    "@tanstack/react-table",
    "@trpc/client",
    "@trpc/react-query",
    "@trpc/server",
    "@supabase/supabase-js",
    "@vercel/blob",
    "class-variance-authority",
    "cmdk",
    "drizzle-orm",
    "firebase",
    "googleapis",
    "jspdf",
    "lucide-react",
    "next",
    "next-auth",
    "nodemailer",
    "prisma",
    "react",
    "react-dom",
    "react-dropzone",
    "react-hook-form",
    "recharts",
    "resend",
    "uploadthing",
    "tailwind-merge",
    "tailwindcss",
    "xlsx",
    "zod",
}


def normalize_capability(capability: str) -> str:
    return capability.strip().lower().replace("_", "-").replace(" ", "-")


def terms_for_capability(capability: str) -> list[str]:
    normalized = normalize_capability(capability)
    terms = CAPABILITY_TERMS.get(normalized)
    if terms:
        return terms
    return [part for part in normalized.replace("-", " ").split() if len(part) > 2]


def collect_scan_files(snapshot_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in snapshot_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in CONFIG_NAMES or path.suffix in SOURCE_EXTENSIONS:
            try:
                if path.stat().st_size <= MAX_FILE_BYTES:
                    files.append(path)
            except OSError:
                continue
    return files


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _path_parts(rel_path: str) -> set[str]:
    return {part.lower() for part in rel_path.replace("\\", "/").split("/")}


def _path_tokens(rel_path: str) -> set[str]:
    tokens: set[str] = set()
    for part in rel_path.lower().replace("\\", "/").split("/"):
        stem = Path(part).stem
        tokens.add(stem)
        tokens.update(token for token in stem.replace("_", "-").split("-") if token)
    return tokens


def _capability_path_signal(rel_path: str, capability: str) -> bool:
    lower_path = rel_path.lower()
    hints = CAPABILITY_PATH_HINTS.get(capability, set())
    tokens = _path_tokens(rel_path)
    return bool(tokens & hints) or any(hint in lower_path for hint in hints if len(hint) >= 4)


def _noise_penalty(rel_path: str, capability: str) -> float:
    parts = _path_parts(rel_path)
    stem = Path(rel_path).stem.lower()
    penalty = 0.0
    noisy_parts = NOISY_PATH_PARTS
    if capability in BACKEND_CAPABILITIES:
        noisy_parts = noisy_parts - BACKEND_PATH_PARTS - {"schema", "schemas"}
    if parts & noisy_parts:
        penalty += 0.5
    if stem in NOISY_FILE_STEMS and capability not in BACKEND_CAPABILITIES:
        penalty += 0.35
    if rel_path.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
        penalty += 0.4
    return min(1.0, penalty)


def _ui_path_score(rel_path: str, capability: str) -> float:
    suffix = Path(rel_path).suffix.lower()
    if suffix not in ENTRY_EXTENSIONS:
        return 0.0

    parts = _path_parts(rel_path)
    score = 0.2
    if suffix in {".tsx", ".jsx"}:
        score += 0.35
    if parts & UI_PATH_PARTS:
        score += 0.25
    if capability in BACKEND_CAPABILITIES and parts & BACKEND_PATH_PARTS:
        score += 0.25
    if _capability_path_signal(rel_path, capability):
        score += 0.25
    score -= _noise_penalty(rel_path, capability) * 0.6
    return round(max(0.0, min(1.0, score)), 4)


def _strong_content_hits(content: str, capability: str) -> int:
    lowered = content.lower()
    return sum(1 for term in CAPABILITY_STRONG_CONTENT.get(capability, set()) if term in lowered)


def _server_action_file_signal(rel_path: str, searchable: str) -> bool:
    path = rel_path.replace("\\", "/").lower()
    if '"use server"' in searchable or "'use server'" in searchable:
        return True
    parts = set(path.split("/"))
    noisy_parts = {"__tests__", "components", "e2e", "hooks", "test", "tests"}
    if parts & noisy_parts:
        return False
    return (
        "/_actions/" in path
        or "/actions/" in path
        or path.endswith("/actions.ts")
        or path.endswith("/actions.tsx")
        or path.endswith("/action.ts")
        or path.endswith("/action.tsx")
    )


def _line_citations(rel_path: str, content: str, terms: list[str], max_ranges: int = 3) -> list[str]:
    lines = content.splitlines()
    citations: list[str] = []
    lowered_terms = [term.lower() for term in terms]
    for index, line in enumerate(lines, start=1):
        lowered = line.lower()
        if any(term in lowered for term in lowered_terms):
            start = max(1, index - 2)
            end = min(len(lines), index + 2)
            citation = f"{rel_path}:{start}-{end}"
            if citation not in citations:
                citations.append(citation)
            if len(citations) >= max_ranges:
                break
    return citations


def _load_package_dependencies(snapshot_root: Path) -> tuple[dict[str, str], list[str]]:
    dependencies: dict[str, str] = {}
    manifests: list[str] = []
    for manifest in snapshot_root.rglob("package.json"):
        if any(part in SKIP_DIRS for part in manifest.parts):
            continue
        content = read_text(manifest)
        if not content:
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue
        manifests.append(_relative(snapshot_root, manifest))
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            section_values = parsed.get(section)
            if isinstance(section_values, dict):
                for name, version in section_values.items():
                    dependencies[str(name)] = str(version)
    return dependencies, manifests


def _dependency_paths(snapshot_root: Path, manifest_paths: list[str]) -> list[str]:
    paths = list(manifest_paths)
    for name in CONFIG_NAMES - {"package.json"}:
        for path in snapshot_root.rglob(name):
            if not any(part in SKIP_DIRS for part in path.parts):
                paths.append(_relative(snapshot_root, path))
    return sorted(set(paths))


def scan_snapshot(snapshot_root: Path, capability: str, max_files: int = 10) -> dict[str, Any]:
    normalized = normalize_capability(capability)
    terms = terms_for_capability(normalized)
    dependencies, manifest_paths = _load_package_dependencies(snapshot_root)
    relevant_dependencies = sorted(name for name in dependencies if name in RELEVANT_DEPENDENCIES)
    dependency_paths = _dependency_paths(snapshot_root, manifest_paths)

    scored_files: list[tuple[float, str, list[str], float, float]] = []
    for path in collect_scan_files(snapshot_root):
        rel_path = _relative(snapshot_root, path)
        content = read_text(path)
        if content is None:
            continue
        searchable = f"{rel_path}\n{content}".lower()
        term_hits = sum(min(searchable.count(term.lower()), 8) for term in terms)
        ui_score = _ui_path_score(rel_path, normalized)
        noise_penalty = _noise_penalty(rel_path, normalized)
        strong_hits = _strong_content_hits(searchable, normalized)
        if normalized == "server-actions" and not _server_action_file_signal(
            rel_path,
            searchable,
        ):
            continue
        score = float(term_hits) + (ui_score * 4) + (strong_hits * 3)
        if path.name == "package.json":
            score += sum(1 for dep in relevant_dependencies if dep.lower() in searchable)
        elif ui_score <= 0.2:
            score *= 0.45
        if noise_penalty >= 0.7:
            score *= 0.5
        if score <= 0:
            continue
        citations = _line_citations(rel_path, content, terms)
        if not citations:
            line_count = max(1, min(80, len(content.splitlines())))
            citations = [f"{rel_path}:1-{line_count}"]
        scored_files.append((round(score, 4), rel_path, citations, ui_score, noise_penalty))

    scored_files.sort(key=lambda item: item[0], reverse=True)
    top_files = scored_files[:max_files]
    entry_paths = [
        path
        for _, path, _, ui_score, noise_penalty in top_files
        if Path(path).suffix in ENTRY_EXTENSIONS and ui_score >= 0.35 and noise_penalty < 0.7
    ]
    evidence_paths: list[str] = []
    for _, _, citations, _, _ in top_files:
        evidence_paths.extend(citations)
    ui_scores = [ui_score for _, _, _, ui_score, _ in top_files if ui_score > 0]
    avg_ui_path_score = sum(ui_scores) / len(ui_scores) if ui_scores else 0.0
    avg_noise_penalty = (
        sum(noise_penalty for _, _, _, _, noise_penalty in top_files) / len(top_files)
        if top_files
        else 0.0
    )
    capability_path_score = (
        sum(1 for path in entry_paths if _capability_path_signal(path, normalized)) / len(entry_paths)
        if entry_paths
        else 0.0
    )

    adaptation_notes = ["Copy listed files, then adapt imports, routes, and project-specific data loading."]
    if any(dep.startswith("@radix-ui/") for dep in relevant_dependencies):
        adaptation_notes.append(
            "Install matching Radix packages or map primitives to the target shadcn/ui setup."
        )
    if "@tanstack/react-table" in relevant_dependencies:
        adaptation_notes.append(
            "Carry over the table column definitions and TanStack table dependency together."
        )
    if "react-hook-form" in relevant_dependencies or "zod" in relevant_dependencies:
        adaptation_notes.append("Keep form schema, resolver, and validation messages together.")
    if any("cmdk" == dep for dep in relevant_dependencies):
        adaptation_notes.append(
            "Command palettes usually depend on cmdk plus dialog/popover primitives."
        )
    if "@base-ui/react" in relevant_dependencies and normalized == "command-palette":
        adaptation_notes.append(
            "Base UI combobox primitives can be reused as command-style quick search controls."
        )
    if any("@/" in citation for citation in evidence_paths):
        adaptation_notes.append(
            "Replace source alias imports such as '@/...' with the target project's path aliases."
        )

    dependency_bonus = min(0.2, len(relevant_dependencies) * 0.025)
    if normalized == "data-table" and "@tanstack/react-table" in relevant_dependencies:
        dependency_bonus += 0.18
    reuse_score = min(
        1.0,
        0.12
        + min(0.28, len(entry_paths) * 0.07)
        + min(0.16, len(evidence_paths) * 0.015)
        + dependency_bonus
        + (avg_ui_path_score * 0.28)
        - (avg_noise_penalty * 0.18),
    )
    if not entry_paths:
        reuse_score = min(reuse_score, 0.35)
    if normalized == "data-table" and "@tanstack/react-table" not in relevant_dependencies:
        reuse_score = min(reuse_score, 0.82 if capability_path_score > 0 else 0.55)

    return {
        "capability": normalized,
        "entry_paths": entry_paths[:max_files],
        "dependency_paths": dependency_paths,
        "external_dependencies": relevant_dependencies,
        "evidence_paths": sorted(set(evidence_paths)),
        "reuse_score": round(reuse_score, 4) if evidence_paths else 0.0,
        "synthesis": {
            "adaptation_notes": adaptation_notes,
            "match_terms": terms,
            "ui_path_score": round(avg_ui_path_score, 4),
            "noise_penalty": round(avg_noise_penalty, 4),
            "capability_path_score": round(capability_path_score, 4),
        },
    }


def run_evidence(capability: str, limit: int) -> dict[str, int]:
    stored = 0
    skipped = 0
    normalized = normalize_capability(capability)
    snapshots = catalog.list_snapshots_for_evidence(limit)
    catalog.delete_assets_for_snapshots(
        normalized,
        [str(snapshot["snapshot_id"]) for snapshot in snapshots],
    )
    for snapshot in snapshots:
        root = Path(str(snapshot["snapshot_path"]))
        if not root.exists():
            skipped += 1
            continue
        result = scan_snapshot(root, normalized)
        if not result["evidence_paths"]:
            skipped += 1
            continue
        catalog.upsert_asset(
            snapshot_id=str(snapshot["snapshot_id"]),
            repo_id=str(snapshot["repo_id"]),
            capability=normalized,
            evidence=result,
        )
        stored += 1
    catalog.record_analysis_run(
        "evidence",
        "completed",
        {"capability": normalized, "stored": stored, "skipped": skipped},
    )
    return {"stored_assets": stored, "skipped_snapshots": skipped}
