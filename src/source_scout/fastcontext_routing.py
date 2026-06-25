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


def _local_seed_context(root: Path, task: str) -> dict[str, Any]:
    files = glob_paths(root, "**/*", limit=LOCAL_CONTEXT_FILE_LIMIT)
    pattern = _task_grep_pattern(task)
    matches: list[dict[str, Any]] = []
    if pattern:
        matches = grep_paths(root, pattern, limit=LOCAL_CONTEXT_GREP_LIMIT)["matches"]
    terms = _task_terms(task)
    routing = _task_family_routing(terms)
    likely_source_files = _likely_source_files(root, terms, matches, routing=routing)
    return {
        "task_type": routing["task_type"],
        "target_family": routing["target_family"],
        "priority_paths": routing["priority_paths"],
        "priority_prefixes": routing["priority_prefixes"],
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
        priority_paths: list[str] = []
        if {"fastcontext", "explore_local", "exploration", "eval_local_explore"} & term_set:
            priority_paths = [
                "tests/test_fastcontext_local_explore.py",
                "tests/test_fastcontext_cli.py",
                "tests/test_local_explore_eval.py",
            ]
        return {
            "task_type": "test_navigation",
            "target_family": "tests",
            "priority_paths": priority_paths,
            "priority_prefixes": ["tests/"],
        }
    if term_set & {"mcp", "fastmcp"}:
        return {
            "task_type": "mcp_navigation",
            "target_family": "mcp",
            "priority_paths": [
                "src/source_scout/server.py",
                "src/source_scout/models.py",
                "tests/test_server.py",
            ],
            "priority_prefixes": ["tests/"],
        }
    if {"status", "server", "loaded", "load", "smoke"} & term_set and (
        {"lmstudio", "studio", "fastcontext"} & term_set
    ):
        return {
            "task_type": "cli_navigation",
            "target_family": "cli",
            "priority_paths": [
                "src/source_scout/__main__.py",
                "src/source_scout/cli_status.py",
                "src/source_scout/lmstudio.py",
            ],
            "priority_prefixes": ["src/source_scout/cli_"],
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
            "priority_paths": ["src/source_scout/models.py"],
            "priority_prefixes": ["src/source_scout/"],
        }
    if {"github", "api", "rate_limit", "repository", "search", "calls", "requests"} & term_set and (
        {"github", "api", "rate_limit"} & term_set
    ):
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": ["src/source_scout/github_client.py"],
            "priority_prefixes": ["src/source_scout/"],
        }
    if term_set & {"cli", "command", "commands", "parser", "argparse"}:
        priority_paths = ["src/source_scout/__main__.py"]
        if {"eval", "evals", "evaluation", "suite"} & term_set:
            priority_paths.append("src/source_scout/local_explore_eval.py")
        if {"status", "lmstudio", "fastcontext_status"} & term_set:
            priority_paths.append("src/source_scout/cli_status.py")
        return {
            "task_type": "cli_navigation",
            "target_family": "cli",
            "priority_paths": priority_paths,
            "priority_prefixes": ["src/source_scout/cli_"],
        }
    if {"fastcontext", "explore_local", "exploration", "tool_loop", "tool"} & term_set and (
        {"fastcontext", "explore_local", "exploration"} & term_set
    ):
        priority_paths = ["src/source_scout/fastcontext.py"]
        if {"localexploreresult", "result", "returns", "dataclass", "dataclasses"} & term_set:
            priority_paths.append("src/source_scout/models.py")
        if {"structured", "output", "schema", "response_format", "json"} & term_set:
            priority_paths = [
                "src/source_scout/lmstudio.py",
                *priority_paths,
            ]
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": priority_paths,
            "priority_prefixes": ["src/source_scout/"],
        }
    if {"bundle", "bundles", "opened_bundle", "outcome", "outcomes"} & term_set:
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [
                "src/source_scout/bundles.py",
                "src/source_scout/server.py",
                "src/source_scout/catalog.py",
                "src/source_scout/models.py",
            ],
            "priority_prefixes": ["src/source_scout/"],
        }
    if {"gemma", "profile", "profiles", "profiler", "gemma_profile"} & term_set and (
        {"strict", "json", "card", "cards", "repository"} & term_set
    ):
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [
                "src/source_scout/profiler.py",
                "src/source_scout/catalog.py",
                "src/source_scout/lmstudio.py",
            ],
            "priority_prefixes": ["src/source_scout/"],
        }
    if {"evidence", "scanner", "scan", "dependency", "dependencies", "signal", "signals"} & term_set and (
        {"evidence", "scanner", "scan"} & term_set
    ):
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [
                "src/source_scout/evidence.py",
                "src/source_scout/catalog.py",
            ],
            "priority_prefixes": ["src/source_scout/"],
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
        if {"catalog", "top_1", "avoid"} & term_set:
            priority_paths = [
                "src/source_scout/eval_runner.py",
                "tests/test_eval_runner.py",
                "src/source_scout/local_explore_eval.py",
                "tests/test_local_explore_eval.py",
                "src/source_scout/assessment_eval.py",
                "tests/test_assessment_eval.py",
            ]
        elif {"assessment", "assessor", "smoke"} & term_set:
            priority_paths = [
                "src/source_scout/assessment_eval.py",
                "tests/test_assessment_eval.py",
                "src/source_scout/eval_runner.py",
                "tests/test_eval_runner.py",
                "src/source_scout/local_explore_eval.py",
                "tests/test_local_explore_eval.py",
            ]
        else:
            priority_paths = [
                "src/source_scout/local_explore_eval.py",
                "tests/test_local_explore_eval.py",
                "src/source_scout/eval_runner.py",
                "tests/test_eval_runner.py",
                "src/source_scout/assessment_eval.py",
                "tests/test_assessment_eval.py",
            ]
        return {
            "task_type": "eval_runner_navigation",
            "target_family": "eval_runner",
            "priority_paths": priority_paths,
            "priority_prefixes": ["src/source_scout/", "tests/"],
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
            "gemma",
            "profile",
            "signals",
        }
        & term_set
    ):
        return {
            "task_type": "source_navigation",
            "target_family": "src",
            "priority_paths": [
                "src/source_scout/catalog.py",
                "src/source_scout/capabilities.py",
                "src/source_scout/evidence.py",
            ],
            "priority_prefixes": ["src/source_scout/"],
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
            "priority_paths": [
                "src/source_scout/assessor.py",
                "src/source_scout/assessment_eval.py",
                "tests/test_assessor.py",
                "tests/test_assessment_eval.py",
            ],
            "priority_prefixes": ["tests/"],
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
) -> list[str]:
    scores: dict[str, int] = {}
    term_set = set(terms)
    active_routing = routing or _task_family_routing(terms)
    for match in grep_matches:
        path = match.get("path")
        if isinstance(path, str):
            priority, _ = _evidence_path_sort_key(path)
            scores[path] = scores.get(path, 0) + (3 if priority == 0 else 1)
            scores[path] += _task_family_path_bonus(path, active_routing)

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
    if target_family == "eval_runner" and normalized in {
        "src/source_scout/local_explore_eval.py",
        "src/source_scout/eval_runner.py",
        "src/source_scout/assessment_eval.py",
        "tests/test_local_explore_eval.py",
        "tests/test_eval_runner.py",
        "tests/test_assessment_eval.py",
    }:
        bonus += 24
    if target_family == "cli":
        if normalized == "src/source_scout/__main__.py":
            bonus += 18
        if normalized.startswith("tests/") and "cli" in normalized:
            bonus += 10
    if target_family == "mcp":
        if normalized in {"src/source_scout/server.py", "tests/test_server.py"}:
            bonus += 18
    if target_family == "assessment" and ("assessor" in normalized or "assessment" in normalized):
        bonus += 16
    return bonus


def _task_file_bonus(rel_path: str, term_set: set[str]) -> int:
    normalized = rel_path.replace("\\", "/")
    bonus = _generic_local_task_file_bonus(normalized, term_set)
    if normalized == "src/source_scout/pipeline.py":
        if {"scout", "freshness", "created", "pushed", "query", "queries"} & term_set:
            bonus += 14
        if {
            "qualification",
            "rejects",
            "reject",
            "archived",
            "forked",
            "template",
            "mirror",
            "oversized",
            "docs_only",
            "vendor_heavy",
        } & term_set:
            bonus += 14
    if (
        normalized == "src/source_scout/constants.py"
        and {
            "freshness",
            "created",
            "pushed",
            "size",
            "stale",
        }
        & term_set
    ):
        bonus += 10
    if (
        normalized == "src/source_scout/catalog.py"
        and {
            "catalog",
            "assets",
            "asset",
            "searched",
            "scored",
            "search",
            "score",
            "gemma",
            "profile",
            "capability",
            "intent",
        }
        & term_set
    ):
        bonus += 100
    if (
        normalized == "src/source_scout/evidence.py"
        and {
            "evidence",
            "scanner",
            "scan",
            "dependency",
            "dependencies",
            "signal",
            "signals",
        }
        & term_set
    ):
        bonus += 100
    if (
        normalized == "src/source_scout/profiler.py"
        and {
            "gemma",
            "profile",
            "profiles",
            "profiler",
            "strict",
            "json",
        }
        & term_set
    ):
        bonus += 100
    if (
        normalized == "src/source_scout/local_explore_eval.py"
        and {
            "local_explore",
            "explore_local",
            "exploration",
            "eval",
            "suite",
            "scored",
            "loaded",
        }
        & term_set
    ):
        bonus += 80
    if (
        normalized == "src/source_scout/eval_runner.py"
        and {
            "catalog",
            "golden",
            "eval",
            "suite",
            "scored",
            "loaded",
            "summarized",
        }
        & term_set
    ):
        bonus += 90
    if (
        normalized == "src/source_scout/assessment_eval.py"
        and {
            "assessment",
            "assessor",
            "eval",
            "suite",
            "smoke",
        }
        & term_set
    ):
        bonus += 50
    if (
        normalized == "src/source_scout/server.py"
        and {
            "mcp",
            "tool",
            "tools",
            "server",
            "exposed",
            "read_only",
        }
        & term_set
    ):
        bonus += 14
    if (
        normalized == "src/source_scout/bundles.py"
        and {
            "bundle",
            "bundles",
            "opened_bundle",
            "source",
        }
        & term_set
    ):
        if {"source", "created", "opened_bundle", "recorded"} & term_set:
            bonus += 100
        else:
            bonus += 20
    if (
        normalized == "src/source_scout/models.py"
        and {
            "dataclass",
            "dataclasses",
            "model",
            "models",
            "result",
            "results",
            "shape",
            "shapes",
            "candidate",
            "bundle",
            "outcome",
            "explore_local",
        }
        & term_set
    ):
        bonus += 100
    if (
        normalized == "src/source_scout/fastcontext.py"
        and {
            "fastcontext",
            "explore_local",
            "exploration",
            "tool_loop",
            "loop",
            "sandbox",
            "sandboxed",
            "structured",
            "output",
            "schema",
            "read_only",
        }
        & term_set
    ):
        bonus += 100
    if (
        normalized == "src/source_scout/lmstudio.py"
        and {
            "lm",
            "studio",
            "lmstudio",
            "structured",
            "output",
            "schema",
            "json",
            "response_format",
        }
        & term_set
    ):
        bonus += 80
    if normalized == "README.md" and {"documentation", "docs", "readme", "usage"} & term_set:
        bonus += 18
    if normalized == "AGENTS.md" and {"documentation", "docs", "agents", "usage"} & term_set:
        bonus += 12
    if (
        normalized == "tests/test_fastcontext_local_explore.py"
        and {
            "exploration",
            "explore_local",
            "read_only",
            "citation",
            "citations",
            "validates",
        }
        & term_set
    ):
        bonus += 70
    if (
        normalized == "tests/test_local_explore_eval.py"
        and {
            "local_explore",
            "eval",
            "eval_local_explore",
            "exploration",
        }
        & term_set
    ):
        bonus += 60
    if (
        normalized == "tests/test_fastcontext_cli.py"
        and {
            "cli",
            "command",
            "commands",
            "explore_local",
            "fastcontext_status",
        }
        & term_set
    ):
        bonus += 60
    if (
        normalized == "tests/test_eval_runner.py"
        and {
            "catalog",
            "golden",
            "eval",
            "suite",
        }
        & term_set
    ):
        bonus += 60
    return bonus


def _generic_local_task_file_bonus(normalized: str, term_set: set[str]) -> int:
    bonus = 0
    stem = Path(normalized).stem.lower().replace("-", "_")
    parts = set(normalized.lower().replace("-", "_").replace("/", "_").split("_"))
    if stem in term_set or parts & term_set:
        bonus += 6
    if normalized.startswith(("app/", "components/", "lib/")):
        bonus += 8
    if normalized.startswith("lib/"):
        bonus += 6
    if (
        normalized.endswith("protein-requirements.ts")
        and {
            "protein",
            "requirement",
            "requirements",
            "grams",
            "energy_percent",
        }
        & term_set
    ):
        bonus += 120
    if (
        normalized.endswith(("nutrition-risk.ts", "nutrition-risk-banner.tsx"))
        and {
            "nutrition",
            "risk",
            "screening",
            "previous",
            "weight",
            "bmi",
        }
        & term_set
    ):
        bonus += 120
    if (
        normalized.endswith("bmi-form.tsx")
        and {
            "bmi",
            "calculator",
            "submit",
            "form",
            "tdee",
            "mifflin",
            "risk",
        }
        & term_set
    ):
        bonus += 90
    if (
        normalized.endswith("bmi-math.ts")
        and {
            "bmi",
            "mifflin",
            "tdee",
            "henry",
            "nasem",
            "energy",
        }
        & term_set
    ):
        bonus += 120
    if (
        normalized.endswith("inntak-form.tsx")
        and {
            "intake",
            "inntak",
            "registration",
            "meal",
            "meals",
            "summary",
            "active",
        }
        & term_set
    ):
        bonus += 120
    if (
        normalized.endswith("use-intake-form-state.ts")
        and {
            "state",
            "active",
            "day",
            "meal",
            "copy",
            "update",
            "item",
            "localstorage",
            "lifecycle",
        }
        & term_set
    ):
        bonus += 120
    if (
        normalized.endswith("storage-lifecycle.ts")
        and {
            "storage",
            "localstorage",
            "lifecycle",
            "stored",
            "write",
            "read",
        }
        & term_set
    ):
        bonus += 100
    if (
        normalized.endswith("app/api/parse-food/route.ts")
        and {
            "parse",
            "food",
            "api",
            "route",
            "openai",
            "trace",
        }
        & term_set
    ):
        bonus += 120
    if (
        normalized.endswith("openai-parser.ts")
        and {
            "openai",
            "parser",
            "parse",
            "schema",
            "prompt",
            "normalization",
            "normalize",
            "reject",
            "malformed",
        }
        & term_set
    ):
        bonus += 120
    if (
        normalized.endswith("matcher.ts")
        and {
            "matching",
            "matcher",
            "lexical",
            "alias",
            "semantic",
            "modifier",
            "portion",
            "decision",
        }
        & term_set
    ):
        bonus += 120
    if (
        normalized.endswith("decision.ts")
        and {
            "decision",
            "auto_select",
            "needs_review",
            "manual_required",
            "threshold",
        }
        & term_set
    ):
        bonus += 110
    if (
        normalized.endswith("semantic-search.ts")
        and {
            "semantic",
            "supabase",
            "pgvector",
            "embedding",
            "embeds",
            "rpc",
        }
        & term_set
    ):
        bonus += 130
    if (
        normalized.endswith(("app/api/food-search/route.ts", "food-search-client.ts"))
        and {
            "food_search",
            "manual",
            "matvaretabellen",
            "lookup",
            "direct",
            "api",
        }
        & term_set
    ):
        bonus += 120
    if (
        normalized.endswith("meal-section.tsx")
        and {
            "meal",
            "manual",
            "search",
            "lookup",
            "food",
        }
        & term_set
    ):
        bonus += 90
    if (
        normalized.endswith(("daily-summary-view.tsx", "daily-totals.tsx", "intake-math.ts"))
        and {
            "daily",
            "average",
            "summary",
            "macro",
            "micro",
            "nutrient",
            "totals",
            "safety",
        }
        & term_set
    ):
        bonus += 110
    if (
        normalized.endswith(
            (
                "intake-export-panel.tsx",
                "session.ts",
                "intake-print-view.tsx",
                "report-html.ts",
            )
        )
        and {
            "export",
            "print",
            "report",
            "session",
            "comparison",
            "summary",
        }
        & term_set
    ):
        bonus += 110
    if (
        normalized.endswith("tests/inntak-ui.spec.ts")
        and {
            "seeded",
            "intake",
            "inntak",
            "manual",
            "styling",
            "portion",
            "helpers",
        }
        & term_set
    ):
        bonus += 140
    if (
        normalized.endswith("tests/kalkulator-ui.spec.ts")
        and {
            "calculator",
            "kalkulator",
            "desktop",
            "mobile",
            "protein",
            "clinical",
            "source",
        }
        & term_set
    ):
        bonus += 140
    if (
        normalized.endswith(("app/layout.tsx", "components/ui/app-nav.tsx", "app/page.tsx"))
        and {
            "routing",
            "navigation",
            "redirect",
            "layout",
            "links",
            "shell",
        }
        & term_set
    ):
        bonus += 100
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
    for prefix in active_routing.get("priority_prefixes", []):
        if normalized.startswith(str(prefix)):
            return -1
    if {"documentation", "docs", "readme", "usage"} & term_set:
        if normalized in {"README.md", "AGENTS.md"} or normalized.startswith("docs/"):
            return -1
    return _evidence_path_sort_key(path)[0]
