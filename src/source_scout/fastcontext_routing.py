import re
from pathlib import Path
from typing import Any

from .fastcontext_constants import (
    LOCAL_CONTEXT_FILE_LIMIT,
    LOCAL_CONTEXT_GREP_LIMIT,
    LOCAL_TASK_STOPWORDS,
    PRIMARY_SOURCE_PREFIXES,
)
from .fastcontext_tools import (
    _evidence_path_sort_key,
    _iter_files,
    _relative_path,
    _resolve_under_root,
    glob_paths,
    grep_paths,
)
from .fastcontext_types import FastContextError
from .repo_map import (
    build_repo_map,
    repo_map_relevant_paths,
    repo_map_seed_items,
    repo_map_seed_text,
)


def _local_seed_context(root: Path, task: str) -> dict[str, Any]:
    files = glob_paths(root, "**/*", limit=LOCAL_CONTEXT_FILE_LIMIT)
    pattern = _task_grep_pattern(task)
    matches: list[dict[str, Any]] = []
    if pattern:
        matches = grep_paths(root, pattern, limit=LOCAL_CONTEXT_GREP_LIMIT)["matches"]
    terms = _task_terms(task)
    routing = _task_family_routing(terms)
    repo_map = build_repo_map(root, max_files=240, max_symbols=2000)
    repo_map_paths = repo_map_relevant_paths(repo_map, terms, limit=LOCAL_CONTEXT_FILE_LIMIT)
    likely_source_files = _likely_source_files(
        root,
        terms,
        matches,
        routing=routing,
        repo_map_paths=repo_map_paths,
    )
    return {
        "task_type": routing["task_type"],
        "target_family": routing["target_family"],
        "priority_paths": routing["priority_paths"],
        "priority_prefixes": routing["priority_prefixes"],
        "repo_map_hints": repo_map_seed_text(repo_map, task, limit=24),
        "repo_map": repo_map_seed_items(repo_map, task, limit=20),
        "likely_source_files": likely_source_files,
        "priority_file_matches": _priority_file_matches(root, terms, likely_source_files),
        "known_files_sample": files["matches"],
        "known_files_truncated": files["truncated"],
        "initial_grep_pattern": pattern,
        "initial_grep_matches": matches,
    }


def _task_grep_pattern(task: str) -> str:
    return "|".join(re.escape(term) for term in _task_terms(task)[:14])


def _seed_priority_paths(seed_context: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    for key in ("priority_paths", "likely_source_files"):
        value = seed_context.get(key, [])
        if not isinstance(value, list):
            continue
        for item in value:
            path = str(item).replace("\\", "/")
            if path and path not in ordered:
                ordered.append(path)
    return ordered


def _priority_file_matches(
    root: Path,
    terms: list[str],
    likely_source_files: list[str],
    *,
    file_limit: int = 6,
    per_file_limit: int = 4,
) -> list[dict[str, Any]]:
    useful_terms = [
        term
        for term in terms
        if len(term) >= 4 and term not in {"source", "local", "project", "implementation"}
    ][:18]
    if not useful_terms:
        return []
    matches: list[dict[str, Any]] = []
    for rel_path in likely_source_files[:file_limit]:
        try:
            path, safe_rel = _resolve_under_root(root, rel_path)
        except FastContextError:
            continue
        if not path.is_file():
            continue
        file_matches = _file_term_matches(
            path,
            safe_rel,
            useful_terms,
            limit=per_file_limit,
        )
        matches.extend(file_matches)
    return matches


def _file_term_matches(
    path: Path,
    safe_rel: str,
    terms: list[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    scored_results: list[tuple[int, int, dict[str, Any]]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line_number, text in enumerate(lines, start=1):
        searchable = text.lower().replace("-", "_")
        matched_terms = [term for term in terms if term in searchable]
        if not matched_terms:
            continue
        score = _file_term_match_score(text, matched_terms)
        scored_results.append(
            (
                -score,
                line_number,
                {
                    "path": safe_rel,
                    "line": line_number,
                    "citation": f"{safe_rel}:{line_number}-{line_number}",
                    "matched_terms": matched_terms[:5],
                    "score": score,
                    "text": text.strip()[:240],
                },
            )
        )
    return [item for _score, _line, item in sorted(scored_results)[:limit]]


def _file_term_match_score(text: str, matched_terms: list[str]) -> int:
    stripped = text.strip()
    lowered = stripped.lower()
    score = len(set(matched_terms)) * 4
    if lowered.startswith(("def ", "async def ", "class ")):
        score += 20
    if lowered.startswith(("return ", "if ", "for ", "while ", "with ")):
        score += 4
    if lowered.startswith(("from ", "import ")):
        score -= 12
    if re.match(r"^[A-Z0-9_]+\s*=", stripped):
        score -= 6
    if any(term in {"path", "source", "repository"} for term in matched_terms):
        score -= 2
    return score


def _task_terms(task: str) -> list[str]:
    raw_terms = [
        raw_term.lower().replace("-", "_") for raw_term in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{1,}", task)
    ]
    terms: list[str] = []

    def add_term(term: str) -> None:
        if term not in terms:
            terms.append(term)

    for term in raw_terms:
        if len(term) >= 3 and term not in LOCAL_TASK_STOPWORDS:
            add_term(term)
            if term.endswith("s") and len(term) > 4:
                add_term(term[:-1])
    for left, right in zip(raw_terms, raw_terms[1:]):
        joined = f"{left}{right}"
        if len(joined) >= 5 and left not in LOCAL_TASK_STOPWORDS and right not in LOCAL_TASK_STOPWORDS:
            add_term(joined)
    return terms


def _task_family_routing(terms: list[str]) -> dict[str, Any]:
    term_set = set(terms)
    if term_set & {"documentation", "docs", "readme", "agents", "usage"}:
        return {
            "task_type": "documentation_navigation",
            "target_family": "docs",
            "priority_paths": ["README.md", "AGENTS.md"],
            "priority_prefixes": ["docs/"],
        }
    if term_set & {"test", "tests", "pytest", "assert", "asserts", "verifies", "verify", "prove"}:
        return {
            "task_type": "test_navigation",
            "target_family": "tests",
            "priority_paths": [],
            "priority_prefixes": ["tests/"],
        }
    if term_set & {"mcp", "fastmcp"}:
        return {
            "task_type": "mcp_navigation",
            "target_family": "mcp",
            "priority_paths": [],
            "priority_prefixes": ["src/", "app/", "lib/", "tests/"],
        }
    if {"status", "server", "loaded", "load", "smoke"} & term_set and (
        {"lmstudio", "studio", "fastcontext"} & term_set
    ):
        return {
            "task_type": "cli_navigation",
            "target_family": "cli",
            "priority_paths": [],
            "priority_prefixes": ["src/", "app/", "lib/", "cli/", "tests/"],
        }
    if {
        "dataclass",
        "dataclasses",
        "model",
        "models",
        "result",
        "results",
        "shape",
        "shapes",
    } & term_set and {
        "candidate",
        "candidates",
        "bundle",
        "bundles",
        "outcome",
        "outcomes",
        "explore_local",
    } & term_set:
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [],
            "priority_prefixes": ["src/", "app/", "lib/"],
        }
    if {"github", "api", "rate_limit", "repository", "search", "calls", "requests"} & term_set and (
        {"github", "api", "rate_limit"} & term_set
    ):
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [],
            "priority_prefixes": ["src/", "app/", "lib/"],
        }
    if term_set & {"cli", "command", "commands", "parser", "argparse"}:
        return {
            "task_type": "cli_navigation",
            "target_family": "cli",
            "priority_paths": [],
            "priority_prefixes": ["src/", "app/", "lib/", "cli/", "scripts/", "tests/"],
        }
    if {"fastcontext", "explore_local", "exploration", "tool_loop", "tool"} & term_set and (
        {"fastcontext", "explore_local", "exploration"} & term_set
    ):
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [],
            "priority_prefixes": ["src/", "app/", "lib/", "tests/"],
        }
    if {"bundle", "bundles", "opened_bundle", "outcome", "outcomes"} & term_set:
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [],
            "priority_prefixes": ["src/", "app/", "lib/", "tests/"],
        }
    if {"profile", "profiles", "profiler", "repository_profile"} & term_set and (
        {"strict", "json", "card", "cards", "repository"} & term_set
    ):
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [],
            "priority_prefixes": ["src/", "app/", "lib/", "tests/"],
        }
    if {"evidence", "scanner", "scan", "dependency", "dependencies", "signal", "signals"} & term_set and (
        {"evidence", "scanner", "scan"} & term_set
    ):
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [],
            "priority_prefixes": ["src/", "app/", "lib/", "tests/"],
        }
    if {"eval", "evals", "evaluation", "suite"} & term_set and {
        "loaded",
        "scored",
        "score",
        "summarized",
        "summary",
        "runner",
        "exposed",
    } & term_set:
        return {
            "task_type": "eval_runner_navigation",
            "target_family": "eval_runner",
            "priority_paths": [],
            "priority_prefixes": ["src/", "evals/", "tests/"],
        }
    if {"catalog", "candidate", "candidates", "search_assets"} & term_set and (
        {
            "score",
            "scored",
            "scoring",
            "search",
            "searched",
            "capability",
            "intent",
            "profile",
            "signals",
        }
        & term_set
    ):
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [],
            "priority_prefixes": ["src/", "app/", "lib/", "tests/"],
        }
    if term_set & {"golden", "fixture", "fixtures", "suite", "eval", "evals", "evaluation"}:
        return {
            "task_type": "fixture_navigation",
            "target_family": "evals",
            "priority_paths": [],
            "priority_prefixes": ["evals/"],
        }
    if term_set & {"assessment", "assessor", "verdict", "reuse"}:
        return {
            "task_type": "assessment_navigation",
            "target_family": "assessment",
            "priority_paths": [],
            "priority_prefixes": ["src/", "tests/"],
        }
    return {
        "task_type": "source_navigation",
        "target_family": "src",
        "priority_paths": [],
        "priority_prefixes": ["src/"],
    }


def _likely_source_files(
    root: Path,
    terms: list[str],
    grep_matches: list[dict[str, Any]],
    limit: int = 18,
    routing: dict[str, Any] | None = None,
    repo_map_paths: list[str] | None = None,
) -> list[str]:
    scores: dict[str, int] = {}
    term_set = set(terms)
    active_routing = routing or _task_family_routing(terms)
    for index, rel_path in enumerate(repo_map_paths or []):
        scores[rel_path] = scores.get(rel_path, 0) + max(20, 96 - (index * 3))
    for match in grep_matches:
        path = match.get("path")
        if isinstance(path, str):
            priority, _ = _evidence_path_sort_key(path)
            scores[path] = scores.get(path, 0) + (3 if priority == 0 else 1)
            scores[path] += _task_family_path_bonus(path, active_routing)
            scores[path] += _source_task_bias(path, term_set)

    for path in _iter_files(root):
        rel_path = _relative_path(root, path)
        searchable = rel_path.lower().replace("-", "_")
        stem = path.stem.lower().replace("-", "_")
        score = sum(5 for term in term_set if term in searchable or term in stem)
        score += _task_file_bonus(rel_path, term_set)
        score += _task_family_path_bonus(rel_path, active_routing)
        if {"cli", "command", "commands"} & term_set and path.name in {"__main__.py", "cli.py"}:
            score += 6
        if {"mcp", "tool", "tools", "server"} & term_set and path.name in {"server.py"}:
            score += 5
        if {
            "model",
            "models",
            "result",
            "results",
            "shape",
            "shapes",
        } & term_set and path.name == "models.py":
            score += 6
        if score:
            if rel_path.startswith(PRIMARY_SOURCE_PREFIXES):
                score += 2
            score += _source_task_bias(rel_path, term_set)
            scores[rel_path] = scores.get(rel_path, 0) + score

    ranked = sorted(
        scores,
        key=lambda path: (
            _seed_path_priority(path, term_set, active_routing),
            -scores[path],
            _evidence_path_sort_key(path)[1],
        ),
    )
    return ranked[:limit]


def _task_family_path_bonus(rel_path: str, routing: dict[str, Any]) -> int:
    normalized = rel_path.replace("\\", "/")
    bonus = 0
    if normalized in set(routing.get("priority_paths", [])):
        bonus += 40
    for prefix in routing.get("priority_prefixes", []):
        if normalized.startswith(str(prefix)):
            bonus += 18
            break
    target_family = str(routing.get("target_family", ""))
    if (
        target_family == "tests"
        and normalized.startswith("tests/")
        and Path(normalized).name.startswith("test_")
    ):
        bonus += 12
    if target_family == "evals" and normalized.startswith("evals/"):
        bonus += 14
    if target_family == "cli":
        if Path(normalized).name in {"__main__.py", "cli.py"} or "/cli" in normalized:
            bonus += 12
    if target_family == "mcp":
        if "server" in normalized or "mcp" in normalized:
            bonus += 10
    if target_family == "assessment" and ("assessor" in normalized or "assessment" in normalized):
        bonus += 16
    if target_family == "eval_runner" and ("eval" in normalized or normalized.startswith("evals/")):
        if normalized.startswith("src/"):
            bonus += 22
        elif normalized.startswith("tests/"):
            bonus += 16
        else:
            bonus += 6
    return bonus


def _source_task_bias(rel_path: str, term_set: set[str]) -> int:
    normalized = rel_path.replace("\\", "/").lower()
    if normalized.startswith(("src/", "app/", "components/", "lib/")):
        return 10
    if normalized.startswith("tests/") and not (term_set & {"test", "tests", "pytest", "spec"}):
        return -24
    if normalized.startswith("evals/") and not (
        term_set & {"eval", "evals", "evaluation", "fixture", "fixtures", "golden"}
    ):
        return -24
    if normalized.startswith("docs/") and not (term_set & {"docs", "documentation", "readme", "usage"}):
        return -24
    if "/fixtures/" in normalized and not (term_set & {"fixture", "fixtures", "golden"}):
        return -24
    return 0


def _task_file_bonus(rel_path: str, term_set: set[str]) -> int:
    normalized = rel_path.replace("\\", "/")
    bonus = _generic_local_task_file_bonus(normalized, term_set)
    if normalized == "README.md" and {"documentation", "docs", "readme", "usage"} & term_set:
        bonus += 18
    if normalized == "AGENTS.md" and {"documentation", "docs", "agents", "usage"} & term_set:
        bonus += 12
    return bonus


def _generic_local_task_file_bonus(normalized: str, term_set: set[str]) -> int:
    bonus = 0
    stem = Path(normalized).stem.lower().replace("-", "_")
    parts = set(normalized.lower().replace("-", "_").replace("/", "_").split("_"))
    if stem in term_set or parts & term_set:
        bonus += 6
    if normalized.startswith(("src/", "app/", "components/", "lib/")):
        bonus += 8
    if normalized.startswith("lib/"):
        bonus += 6
    if {"api", "route", "handler", "endpoint"} & term_set and (
        "/api/" in normalized or normalized.endswith(("/route.ts", "/route.js", "/route.py"))
    ):
        bonus += 18
    if {"cli", "command", "commands", "argparse", "script"} & term_set and (
        Path(normalized).name in {"__main__.py", "cli.py"}
        or "/cli" in normalized
        or "/scripts/" in normalized
    ):
        bonus += 18
    if {"mcp", "tool", "tools", "server"} & term_set and ("mcp" in normalized or "server" in normalized):
        bonus += 16
    if {"eval", "evals", "evaluation", "suite", "golden"} & term_set and (
        "eval" in normalized or normalized.startswith("evals/")
    ):
        bonus += 16
    if {"test", "tests", "pytest", "spec"} & term_set and (
        normalized.startswith("tests/") or Path(normalized).name.startswith("test_") or ".spec." in normalized
    ):
        bonus += 16
    if {"model", "models", "schema", "dataclass", "types"} & term_set and any(
        part in normalized for part in ("model", "schema", "types", "dataclass")
    ):
        bonus += 14
    if {"dataclass", "dataclasses", "model", "models", "result", "results", "shape", "types"} & term_set:
        if Path(normalized).stem.lower() in {"model", "models", "schema", "schemas", "type", "types"}:
            bonus += 24
    if {"catalog", "store", "storage", "database", "duckdb", "cache"} & term_set and any(
        part in normalized for part in ("catalog", "store", "storage", "database", "db", "cache")
    ):
        bonus += 14
    if {"bundle", "bundles", "artifact", "manifest"} & term_set and any(
        part in normalized for part in ("bundle", "artifact", "manifest")
    ):
        bonus += 14
    if {"llm", "model", "openai", "lmstudio", "studio", "fastcontext"} & term_set and any(
        part in normalized for part in ("llm", "model", "openai", "lmstudio", "studio", "fastcontext")
    ):
        bonus += 14
    if {"rag", "retrieval", "search", "vector", "embedding"} & term_set and any(
        part in normalized for part in ("rag", "retrieval", "search", "vector", "embed")
    ):
        bonus += 14
    if {
        "active",
        "component",
        "form",
        "layout",
        "navigation",
        "page",
        "registration",
        "section",
        "shell",
        "tabs",
        "ui",
    } & term_set and normalized.endswith((".tsx", ".jsx")):
        bonus += 16
    if {"manual", "lookup", "search", "direct", "ui"} & term_set:
        if normalized.startswith("components/"):
            bonus += 12
        if "/api/" in normalized or "client" in normalized:
            bonus += 16
    if {"routing", "navigation", "redirect", "layout", "links", "shell"} & term_set:
        if Path(normalized).name in {"layout.tsx", "layout.jsx", "layout.ts", "layout.js"}:
            bonus += 24
        if Path(normalized).name in {"page.tsx", "page.jsx", "page.ts", "page.js"}:
            bonus += 16
        if "nav" in normalized or "navigation" in normalized:
            bonus += 24
    return bonus


def _seed_path_priority(
    path: str,
    term_set: set[str],
    routing: dict[str, Any] | None = None,
) -> int:
    active_routing = routing or {}
    normalized = path.replace("\\", "/")
    priority_paths = [str(item) for item in active_routing.get("priority_paths", [])]
    if normalized in priority_paths:
        return -20 + priority_paths.index(normalized)
    if active_routing.get("target_family") == "eval_runner":
        if normalized.startswith("src/") and "eval" in normalized:
            return -3
        if normalized.startswith("tests/") and "eval" in normalized:
            return -2
        if normalized.startswith("evals/"):
            return -1
    for prefix in active_routing.get("priority_prefixes", []):
        if normalized.startswith(str(prefix)):
            return -1
    if {"documentation", "docs", "readme", "usage"} & term_set:
        if normalized in {"README.md", "AGENTS.md"} or normalized.startswith("docs/"):
            return -1
    return _evidence_path_sort_key(path)[0]
