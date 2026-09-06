"""Source Scout CLI. Check imports only the local check runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import cli_checks as _cli_checks

_check_commands = _cli_checks._check_commands
_run_check_commands = _cli_checks._run_check_commands
RETIRED_COMMANDS = {
    "scout",
    "qualify",
    "evidence",
    "profile",
    "assess",
    "refine-evidence",
    "eval",
    "eval-reuse-loop",
    "eval-assess",
    "gc",
    "audit",
}


def _run_mcp(transport: str, port: int, profile: str = "default") -> None:
    from fastmcp import settings

    from .server import create_server

    settings.check_for_updates = "off"
    mcp = create_server(profile)
    if transport == "http":
        mcp.run(transport="http", host="127.0.0.1", port=port, show_banner=False)
    else:
        mcp.run(show_banner=False)


def main() -> None:
    from .exploration_policy import LOCAL_METHODS, SELECTIVE_USE_CASES
    from .fastcontext_constants import DEFAULT_MAX_TURNS

    # Transition messages must not import or invoke the retired chain.
    if len(sys.argv) > 1 and sys.argv[1] in RETIRED_COMMANDS:
        print(
            "This workflow is retired. Use 'references add|find|context|github-search' "
            "or 'references export-data' for historical records. No data was changed.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    parser = argparse.ArgumentParser(
        description=(
            "Source Scout: Implementation References, selective Code Investigation and this project's checks."
        )
    )
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8000)
    sub = parser.add_subparsers(dest="command")
    check = sub.add_parser("check", help="Run Source Scout's own Ruff, mypy and offline pytest checks")
    check.add_argument("--format", choices=["text", "json"], default="text")
    check.add_argument("--timeout-seconds", type=float, default=300.0)

    refs = sub.add_parser("references", help="Manage and inspect explicit implementation references")
    refsub = refs.add_subparsers(dest="reference_command", required=True)
    add = refsub.add_parser("add", help="Pin one explicitly selected local/GitHub repository")
    add.add_argument("--source", required=True)
    add.add_argument("--kind", choices=["personal", "curated"], default="personal")
    add.add_argument("--commit")
    find = refsub.add_parser("find", help="Find source evidence, or abstain")
    find.add_argument("--task", required=True)
    find.add_argument("--target-project-path")
    find.add_argument("--max-results", type=int, choices=range(1, 4), default=3)
    context = refsub.add_parser("context", help="Read verified Git-blob context")
    context.add_argument("--reference-id", required=True)
    context.add_argument("--task", default="")
    context.add_argument("--target-project-path")
    search = refsub.add_parser("github-search", help="Explicit temporary GitHub repository leads")
    search.add_argument("--task", required=True)
    search.add_argument("--max-results", type=int, choices=range(1, 4), default=3)
    inspect = refsub.add_parser("inspect", help="Temporarily inspect selected GitHub files at a commit")
    inspect.add_argument("--source", required=True)
    inspect.add_argument("--commit", required=True)
    inspect.add_argument("--path", action="append", required=True)
    export = refsub.add_parser("export-data", help="Read/export unchanged historical table records")
    export.add_argument("--table", required=True)
    export.add_argument("--limit", type=int, default=100)
    export.add_argument("--offset", type=int, default=0)

    investigate = sub.add_parser(
        "investigate",
        aliases=["explore-local"],
        help="Investigate an unresolved relation after local navigation",
    )
    investigate.add_argument("--task", required=True)
    investigate.add_argument("--source-root", "--project-path", dest="source_root", required=True)
    investigate.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    investigate.add_argument("--format", choices=["json", "text"], default="json")
    investigate.add_argument("--trace-path")
    investigate.add_argument("--reason", default="")
    investigate.add_argument("--use-case", choices=SELECTIVE_USE_CASES)
    investigate.add_argument(
        "--attempted-local-method", dest="attempted_local_methods", action="append", choices=LOCAL_METHODS
    )
    investigate.add_argument("--anchor", action="append", help="Known source path[:start[-end]]")
    serve = sub.add_parser("serve-mcp", help="Run the three-tool MCP surface")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--profile", choices=["default", "investigator", "references", "sidecar"], default="default"
    )
    nav = sub.add_parser("eval-navigation", help="Explicit model-backed navigation diagnostic (paid)")
    nav.add_argument("--suite", required=True)
    nav.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    nav.add_argument("--label")
    nav.add_argument("--output")
    nav.add_argument("--limit-tasks", type=int)
    nav.add_argument("--task-timeout-seconds", type=float, default=240.0)
    nav.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    if args.command in {None, "serve-mcp"}:
        _run_mcp(args.transport, args.port, getattr(args, "profile", "default"))
        return
    if args.command == "check":
        _run_check_commands(output_format=args.format, timeout_seconds=args.timeout_seconds)
        return
    try:
        if args.command == "references":
            result = _references(args)
        elif args.command in {"investigate", "explore-local"}:
            from . import fastcontext
            from .cli_output import _format_local_explore_text
            from .investigation_anchors import parse_cli_anchor

            local = asyncio.run(
                fastcontext.explore_local_project(
                    task=args.task,
                    project_path=args.source_root,
                    max_turns=args.max_turns,
                    trace_path=args.trace_path,
                    reason=args.reason,
                    use_case=args.use_case,
                    attempted_local_methods=args.attempted_local_methods,
                    anchors=[parse_cli_anchor(item) for item in args.anchor] if args.anchor else None,
                )
            )
            print(
                _format_local_explore_text(local)
                if args.format == "text"
                else json.dumps(asdict(local), sort_keys=True)
            )
            if local.status != "completed":
                raise SystemExit(1)
            return
        else:
            from .local_explore_eval import run_local_explore_eval

            result = asyncio.run(
                run_local_explore_eval(
                    suite=args.suite,
                    max_turns=args.max_turns,
                    label=args.label,
                    output_path=Path(args.output) if args.output else None,
                    limit_tasks=args.limit_tasks,
                    task_timeout_seconds=args.task_timeout_seconds,
                    progress=args.progress,
                )
            )
    except (OSError, ValueError, RuntimeError) as exc:
        from .failures import failure_from_exception

        print(
            json.dumps(
                failure_from_exception(
                    exc,
                    stage="exploration" if args.command in {"investigate", "explore-local"} else args.command,
                ).to_dict()
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


def _references(args: argparse.Namespace) -> dict[str, Any]:
    from . import implementation_references as references

    command = args.reference_command
    result: Any
    if command == "add":
        result = asyncio.run(
            references.add_reference_source(args.source, selection_kind=args.kind, commit=args.commit)
        )
    elif command == "find":
        result = references.find_implementation_references(
            args.task, target_project_path=args.target_project_path, max_results=args.max_results
        )
    elif command == "context":
        result = references.get_implementation_reference(
            args.reference_id, task=args.task, target_project_path=args.target_project_path
        )
    elif command == "github-search":
        result = asyncio.run(references.search_github_fallback(args.task, max_results=args.max_results))
    elif command == "inspect":
        result = asyncio.run(references.inspect_github_source(args.source, args.commit, paths=args.path))
    else:
        from .catalog import read_records

        return read_records(args.table, limit=args.limit, offset=args.offset)
    return references.reference_to_jsonable(result)


if __name__ == "__main__":
    main()
