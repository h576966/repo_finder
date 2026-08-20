import ast
import configparser
import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .constants import SKIP_DIRS
from .fastcontext_constants import LOCAL_EXTRA_SKIP_DIRS, LOCAL_SKIP_FILE_NAMES, LOCAL_TASK_STOPWORDS

SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
TEXT_MAP_SUFFIXES = SOURCE_SUFFIXES | {".json", ".toml", ".txt", ".md", ".yml", ".yaml"}
MANIFEST_NAMES = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "uv.lock",
    "poetry.lock",
    "environment.yml",
    "environment.yaml",
    "tsconfig.json",
    "setup.cfg",
    "setup.py",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
}


@dataclass(frozen=True)
class RepoMapEntry:
    kind: str
    path: str
    name: str
    line: int | None = None
    detail: str = ""

    def compact(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in {"", None}}


@dataclass(frozen=True)
class RepoMap:
    root_name: str
    files: list[RepoMapEntry]
    symbols: list[RepoMapEntry]
    cli_entrypoints: list[RepoMapEntry]
    mcp_tools: list[RepoMapEntry]
    tests: list[RepoMapEntry]
    eval_suites: list[RepoMapEntry]
    fixtures: list[RepoMapEntry]
    manifests: list[RepoMapEntry]
    next_routes: list[RepoMapEntry]

    def all_entries(self) -> list[RepoMapEntry]:
        entries: list[RepoMapEntry] = []
        for group in (
            self.symbols,
            self.cli_entrypoints,
            self.mcp_tools,
            self.tests,
            self.eval_suites,
            self.fixtures,
            self.manifests,
            self.next_routes,
            self.files,
        ):
            entries.extend(group)
        return entries


def build_repo_map(root: Path, *, max_files: int = 200, max_symbols: int = 300) -> RepoMap:
    root_resolved = root.resolve()
    files = _candidate_files(root_resolved, max_files=max_files)
    file_entries: list[RepoMapEntry] = []
    symbols: list[RepoMapEntry] = []
    cli_entrypoints: list[RepoMapEntry] = []
    mcp_tools: list[RepoMapEntry] = []
    tests: list[RepoMapEntry] = []
    eval_suites: list[RepoMapEntry] = []
    fixtures: list[RepoMapEntry] = []
    manifests: list[RepoMapEntry] = []
    next_routes: list[RepoMapEntry] = []

    for path in files:
        rel_path = path.relative_to(root_resolved).as_posix()
        file_kind = _file_kind(rel_path)
        file_entries.append(
            RepoMapEntry(
                file_kind,
                rel_path,
                Path(rel_path).name,
                detail=_file_keyword_detail(path) if path.suffix in SOURCE_SUFFIXES else "",
            )
        )
        if _is_manifest(rel_path):
            manifests.append(RepoMapEntry("manifest", rel_path, Path(rel_path).name))
        if _is_fixture(rel_path):
            fixtures.append(RepoMapEntry("fixture", rel_path, Path(rel_path).name))
        if _is_next_route(rel_path):
            next_routes.append(RepoMapEntry("next_route", rel_path, _route_name(rel_path)))
        if _is_test_path(rel_path):
            tests.append(RepoMapEntry("test_file", rel_path, Path(rel_path).name))

        if path.suffix == ".py":
            extracted = _python_entries(path, rel_path)
            symbols.extend(extracted["symbols"])
            cli_entrypoints.extend(extracted["cli_entrypoints"])
            mcp_tools.extend(extracted["mcp_tools"])
            tests.extend(extracted["tests"])
            if path.name == "setup.py":
                cli_entrypoints.extend(_setup_py_scripts(path, rel_path))
        elif path.suffix in {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}:
            extracted = _js_ts_entries(path, rel_path)
            symbols.extend(extracted["symbols"])
            tests.extend(extracted["tests"])
        elif path.name == "package.json":
            cli_entrypoints.extend(_package_scripts(path, rel_path))
        elif path.name == "pyproject.toml":
            cli_entrypoints.extend(_pyproject_scripts(path, rel_path))
        elif path.name == "setup.cfg":
            cli_entrypoints.extend(_setup_cfg_scripts(path, rel_path))
        elif path.suffix == ".json":
            eval_suites.extend(_eval_suite_entries(path, rel_path))

    return RepoMap(
        root_name=root_resolved.name,
        files=file_entries[:max_files],
        symbols=_dedupe_entries(symbols)[:max_symbols],
        cli_entrypoints=_dedupe_entries(cli_entrypoints),
        mcp_tools=_dedupe_entries(mcp_tools),
        tests=_dedupe_entries(tests),
        eval_suites=_dedupe_entries(eval_suites),
        fixtures=_dedupe_entries(fixtures),
        manifests=_dedupe_entries(manifests),
        next_routes=_dedupe_entries(next_routes),
    )


def repo_map_seed_text(repo_map: RepoMap, task: str, *, limit: int = 80) -> str:
    entries = repo_map_relevant_entries(repo_map, task, limit=limit)
    if not entries:
        return "Generated repo map: no relevant hints found."
    lines = ["Generated repo map hints. These are likely files/categories to consider, not final evidence:"]
    for entry in entries:
        location = f":{entry.line}" if entry.line else ""
        detail = f" - {entry.detail}" if entry.detail else ""
        lines.append(f"- {entry.kind}: {entry.path}{location} :: {entry.name}{detail}")
    return "\n".join(lines)


def repo_map_seed_items(repo_map: RepoMap, task: str, *, limit: int = 40) -> list[dict[str, Any]]:
    return [entry.compact() for entry in repo_map_relevant_entries(repo_map, task, limit=limit)]


def repo_map_relevant_paths(
    repo_map: RepoMap,
    terms: list[str] | set[str],
    *,
    limit: int = 24,
) -> list[str]:
    query = " ".join(sorted(terms)) if not isinstance(terms, list) else " ".join(terms)
    paths: list[str] = []
    for entry in repo_map_relevant_entries(repo_map, query, limit=limit * 3):
        if entry.path not in paths:
            paths.append(entry.path)
        if len(paths) >= limit:
            break
    return paths


def repo_map_relevant_entries(repo_map: RepoMap, task: str, *, limit: int = 80) -> list[RepoMapEntry]:
    terms = _task_terms(task)
    if not terms:
        return repo_map.all_entries()[:limit]
    scored: list[tuple[int, int, str, RepoMapEntry]] = []
    for index, entry in enumerate(repo_map.all_entries()):
        score = _entry_score(entry, terms)
        if score > 0:
            scored.append((-score, index, entry.path, entry))
    return [entry for _score, _index, _path, entry in sorted(scored)[:limit]]


def _candidate_files(root: Path, *, max_files: int) -> list[Path]:
    skip_dirs = SKIP_DIRS | LOCAL_EXTRA_SKIP_DIRS
    matches: list[Path] = []
    for current_root_raw, dirnames, filenames in os.walk(root):
        current_root = Path(current_root_raw)
        dirnames[:] = [dirname for dirname in dirnames if dirname not in skip_dirs]
        for filename in filenames:
            if filename in LOCAL_SKIP_FILE_NAMES:
                continue
            path = current_root / filename
            if path.suffix.lower() not in TEXT_MAP_SUFFIXES and path.name not in MANIFEST_NAMES:
                continue
            matches.append(path)
    return sorted(matches, key=lambda path: _path_sort_key(path.relative_to(root).as_posix()))[:max_files]


def _python_entries(path: Path, rel_path: str) -> dict[str, list[RepoMapEntry]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return {"symbols": [], "cli_entrypoints": [], "mcp_tools": [], "tests": []}
    symbols: list[RepoMapEntry] = []
    cli_entrypoints: list[RepoMapEntry] = []
    mcp_tools: list[RepoMapEntry] = []
    tests: list[RepoMapEntry] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name in _assignment_names(node):
                symbols.append(
                    RepoMapEntry(
                        "python_constant",
                        rel_path,
                        name,
                        node.lineno,
                        _node_keyword_detail(node),
                    )
                )
    for ast_node in ast.walk(tree):
        if isinstance(ast_node, ast.ClassDef):
            kind = "python_dataclass" if _has_dataclass_decorator(ast_node) else "python_class"
            symbols.append(
                RepoMapEntry(
                    kind,
                    rel_path,
                    ast_node.name,
                    ast_node.lineno,
                    _node_keyword_detail(ast_node),
                )
            )
            for child in ast_node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        RepoMapEntry(
                            "python_method",
                            rel_path,
                            f"{ast_node.name}.{child.name}",
                            child.lineno,
                            _node_keyword_detail(child),
                        )
                    )
        elif isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = (
                "python_async_function" if isinstance(ast_node, ast.AsyncFunctionDef) else "python_function"
            )
            symbols.append(
                RepoMapEntry(kind, rel_path, ast_node.name, ast_node.lineno, _node_keyword_detail(ast_node))
            )
            if ast_node.name.startswith("test_"):
                tests.append(RepoMapEntry("test", rel_path, ast_node.name, ast_node.lineno))
            if _has_mcp_tool_decorator(ast_node):
                mcp_tools.append(RepoMapEntry("mcp_tool", rel_path, ast_node.name, ast_node.lineno))
        elif isinstance(ast_node, ast.Call):
            parser_name = _add_parser_name(ast_node)
            if parser_name:
                cli_entrypoints.append(RepoMapEntry("cli_command", rel_path, parser_name, ast_node.lineno))
    return {
        "symbols": symbols,
        "cli_entrypoints": cli_entrypoints,
        "mcp_tools": mcp_tools,
        "tests": tests,
    }


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            names.extend(item.id for item in target.elts if isinstance(item, ast.Name))
    return names


def _node_keyword_detail(node: ast.AST, *, limit: int = 18) -> str:
    terms: list[str] = []

    def add_term(value: str) -> None:
        normalized = value.lower().replace("-", "_")
        for term in re.findall(r"[a-z_][a-z0-9_:._]{2,}", normalized):
            cleaned = term.strip("_.:")
            if cleaned and cleaned not in LOCAL_TASK_STOPWORDS and cleaned not in terms:
                terms.append(cleaned)

    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            add_term(child.id)
        elif isinstance(child, ast.Attribute):
            add_term(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            add_term(child.value)
        if len(terms) >= limit:
            break
    return " ".join(terms[:limit])


def _js_ts_entries(path: Path, rel_path: str) -> dict[str, list[RepoMapEntry]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"symbols": [], "tests": []}
    symbols: list[RepoMapEntry] = []
    tests: list[RepoMapEntry] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        for pattern, kind in (
            (r"export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "ts_export_function"),
            (r"export\s+default\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)?", "ts_default_function"),
            (r"export\s+class\s+([A-Za-z_$][\w$]*)", "ts_export_class"),
            (r"export\s+const\s+([A-Za-z_$][\w$]*)", "ts_export_const"),
            (r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "ts_function"),
            (
                r"const\s+([A-Z][A-Za-z0-9_$]*)\s*=\s*(?:\(|React\.|memo\(|forwardRef\()",
                "react_component",
            ),
            (r"class\s+([A-Za-z_$][\w$]*)", "ts_class"),
        ):
            match = re.search(pattern, stripped)
            if match:
                name = match.group(1) or "default"
                symbols.append(
                    RepoMapEntry(
                        kind,
                        rel_path,
                        name,
                        line_number,
                        _text_keyword_detail(lines[line_number - 1 : line_number + 24]),
                    )
                )
                break
        test_match = re.search(r"\b(?:describe|it|test)\s*\(\s*['\"]([^'\"]+)", stripped)
        if test_match:
            tests.append(RepoMapEntry("test", rel_path, test_match.group(1), line_number))
    return {"symbols": symbols, "tests": tests}


def _text_keyword_detail(lines: list[str], *, limit: int = 18) -> str:
    terms: list[str] = []
    for line in lines:
        for term in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,}", line):
            normalized = term.lower().replace("-", "_")
            if normalized not in LOCAL_TASK_STOPWORDS and normalized not in terms:
                terms.append(normalized)
            if len(terms) >= limit:
                return " ".join(terms)
    return " ".join(terms)


def _file_keyword_detail(path: Path, *, line_limit: int = 120, term_limit: int = 24) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:line_limit]
    except OSError:
        return ""
    return _text_keyword_detail(lines, limit=term_limit)


def _package_scripts(path: Path, rel_path: str) -> list[RepoMapEntry]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    scripts = parsed.get("scripts")
    if not isinstance(scripts, dict):
        return []
    return [
        RepoMapEntry("package_script", rel_path, str(name), detail=str(command))
        for name, command in sorted(scripts.items())
    ]


def _pyproject_scripts(path: Path, rel_path: str) -> list[RepoMapEntry]:
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    entries: list[RepoMapEntry] = []
    project_scripts = parsed.get("project", {}).get("scripts", {})
    if isinstance(project_scripts, dict):
        entries.extend(
            RepoMapEntry("python_script", rel_path, str(name), detail=str(target))
            for name, target in sorted(project_scripts.items())
        )
    poetry_scripts = parsed.get("tool", {}).get("poetry", {}).get("scripts", {})
    if isinstance(poetry_scripts, dict):
        entries.extend(
            RepoMapEntry("python_script", rel_path, str(name), detail=str(target))
            for name, target in sorted(poetry_scripts.items())
        )
    return entries


def _setup_cfg_scripts(path: Path, rel_path: str) -> list[RepoMapEntry]:
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return []
    if not parser.has_section("options.entry_points"):
        return []
    raw = parser.get("options.entry_points", "console_scripts", fallback="")
    entries: list[RepoMapEntry] = []
    for line in raw.splitlines():
        name, _, target = line.strip().partition("=")
        if name.strip() and target.strip():
            entries.append(
                RepoMapEntry(
                    "python_script",
                    rel_path,
                    name.strip(),
                    detail=target.strip(),
                )
            )
    return entries


def _setup_py_scripts(path: Path, rel_path: str) -> list[RepoMapEntry]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    entries: list[RepoMapEntry] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_setup_call(node):
            continue
        for keyword in node.keywords:
            if keyword.arg == "entry_points":
                entries.extend(_entry_points_from_ast(keyword.value, rel_path, node.lineno))
    return entries


def _is_setup_call(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Name):
        return node.func.id == "setup"
    return isinstance(node.func, ast.Attribute) and node.func.attr == "setup"


def _entry_points_from_ast(node: ast.AST, rel_path: str, line: int) -> list[RepoMapEntry]:
    entries: list[RepoMapEntry] = []
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=False):
            if not (isinstance(key, ast.Constant) and key.value == "console_scripts"):
                continue
            entries.extend(_console_script_entries(value, rel_path, line))
    return entries


def _console_script_entries(node: ast.AST, rel_path: str, line: int) -> list[RepoMapEntry]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    entries: list[RepoMapEntry] = []
    for item in node.elts:
        if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
            continue
        name, _, target = item.value.partition("=")
        if name.strip() and target.strip():
            entries.append(
                RepoMapEntry(
                    "python_script",
                    rel_path,
                    name.strip(),
                    line,
                    target.strip(),
                )
            )
    return entries


def _eval_suite_entries(path: Path, rel_path: str) -> list[RepoMapEntry]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, dict):
        return []
    suite_id = parsed.get("suite_id")
    if not isinstance(suite_id, str):
        return []
    entries = [RepoMapEntry("eval_suite", rel_path, suite_id)]
    tasks = parsed.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict) and task.get("id"):
                entries.append(RepoMapEntry("eval_task", rel_path, str(task["id"]), detail=suite_id))
    return entries


def _has_mcp_tool_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
        if isinstance(target, ast.Name) and target.id == "tool":
            return True
    return False


def _has_dataclass_decorator(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name) and target.id == "dataclass":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "dataclass":
            return True
    return False


def _add_parser_name(node: ast.Call) -> str:
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "add_parser"):
        return ""
    if not node.args:
        return ""
    first_arg = node.args[0]
    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
        return first_arg.value
    return ""


def _task_terms(task: str) -> set[str]:
    return {
        term.lower().replace("-", "_")
        for term in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{1,}", task)
        if len(term) >= 3 and term.lower().replace("-", "_") not in LOCAL_TASK_STOPWORDS
    }


def _entry_score(entry: RepoMapEntry, terms: set[str]) -> int:
    searchable = f"{entry.kind} {entry.path} {entry.name} {entry.detail}".lower().replace("-", "_")
    score = sum(8 for term in terms if term in searchable)
    if not score:
        return 0
    if _kind_matches_task(entry.kind, terms):
        score += 18
    if entry.path.startswith(("src/", "app/", "components/", "lib/")):
        score += 4
    if _entry_is_off_family(entry, terms):
        score -= 48
    if entry.kind in {
        "python_dataclass",
        "python_function",
        "python_async_function",
        "python_class",
        "ts_export_function",
    }:
        score += 4
    return max(score, 0)


def _entry_is_off_family(entry: RepoMapEntry, terms: set[str]) -> bool:
    if entry.path.startswith("tests/") or entry.kind in {"test", "test_file"}:
        return not bool({"test", "tests", "pytest", "spec"} & terms)
    if entry.path.startswith("evals/") or entry.kind in {"eval_suite", "eval_task"}:
        return not bool({"eval", "evals", "evaluation", "fixture", "fixtures", "golden"} & terms)
    if entry.path.startswith("docs/"):
        return not bool({"docs", "documentation", "readme", "usage"} & terms)
    return False


def _kind_matches_task(kind: str, terms: set[str]) -> bool:
    if kind == "python_dataclass":
        return bool({"dataclass", "dataclasses", "model", "models", "result", "shape"} & terms)
    if kind in {"cli_command", "package_script", "python_script"}:
        return bool({"cli", "command", "commands", "argparse", "script", "scripts"} & terms)
    if kind == "mcp_tool":
        return bool({"mcp", "tool", "tools", "server"} & terms)
    if kind in {"test", "test_file"}:
        return bool({"test", "tests", "pytest", "spec", "assert"} & terms)
    if kind in {"eval_suite", "eval_task"}:
        return bool({"eval", "evals", "suite", "golden", "assessment"} & terms)
    if kind in {"manifest", "package_script", "python_script"}:
        return bool({"manifest", "dependency", "dependencies", "package", "pyproject"} & terms)
    if kind == "next_route":
        return bool({"route", "api", "next", "nextjs", "handler"} & terms)
    if kind == "fixture":
        return bool({"fixture", "fixtures", "golden"} & terms)
    return False


def _file_kind(rel_path: str) -> str:
    if _is_manifest(rel_path):
        return "manifest"
    if _is_next_route(rel_path):
        return "next_route"
    if _is_test_path(rel_path):
        return "test_file"
    if _is_fixture(rel_path):
        return "fixture"
    if rel_path.startswith(("src/", "app/", "components/", "lib/")):
        return "source_file"
    return "project_file"


def _is_manifest(rel_path: str) -> bool:
    path = Path(rel_path)
    return path.name in MANIFEST_NAMES or (path.suffix == ".txt" and path.name.startswith("requirements"))


def _is_fixture(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").lower()
    return any(part in normalized.split("/") for part in {"fixture", "fixtures", "__fixtures__"})


def _is_test_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").lower()
    name = Path(normalized).name
    return normalized.startswith("tests/") or name.startswith("test_") or ".test." in name or ".spec." in name


def _is_next_route(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").lower()
    return (
        normalized.startswith("app/")
        and Path(normalized).name in {"route.ts", "route.tsx", "route.js", "route.jsx"}
    ) or normalized.startswith("pages/api/")


def _route_name(rel_path: str) -> str:
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("app/"):
        return normalized.removeprefix("app/").rsplit("/", 1)[0] or "/"
    if normalized.startswith("pages/api/"):
        return normalized.removeprefix("pages/api/")
    return normalized


def _path_sort_key(rel_path: str) -> tuple[int, str]:
    normalized = rel_path.replace("\\", "/")
    if _is_manifest(normalized):
        return (0, normalized)
    if normalized.startswith(("src/", "app/", "components/", "lib/")):
        return (1, normalized)
    if normalized.startswith("tests/"):
        return (2, normalized)
    if normalized.startswith("evals/"):
        return (3, normalized)
    return (4, normalized)


def _dedupe_entries(entries: list[RepoMapEntry]) -> list[RepoMapEntry]:
    seen: set[tuple[str, str, str, int | None]] = set()
    unique: list[RepoMapEntry] = []
    for entry in entries:
        key = (entry.kind, entry.path, entry.name, entry.line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique
