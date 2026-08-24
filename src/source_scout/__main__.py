import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict

from . import cli_checks as _cli_checks
from . import fastcontext
from .cli_output import _format_local_explore_text
from .cli_status import _api_status, status_is_healthy
from .failures import failure_from_exception

_check_commands = _cli_checks._check_commands
_run_check_commands = _cli_checks._run_check_commands


def _require_github_token() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return
    print("ERROR: GITHUB_TOKEN environment variable is required for this command.", file=sys.stderr)
    print("Set it via: set GITHUB_TOKEN=ghp_your_token  (Windows)", file=sys.stderr)
    print("         or: export GITHUB_TOKEN=ghp_your_token  (Unix)", file=sys.stderr)
    sys.exit(1)


def _run_mcp(transport: str, port: int) -> None:
    from .server import mcp

    if transport == "http":
        print(f"Starting MCP server on http://127.0.0.1:{port}/mcp")
        mcp.run(transport="http", host="127.0.0.1", port=port)
    else:
        mcp.run()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Source Scout - catalog-first local reuse layer for TS/JS/Python source."),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP transport (default: 8000)",
    )
    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser("check", help="Run local development checks")
    check_parser.add_argument("--with-local-explore-eval", action="store_true")

    scout_parser = subparsers.add_parser("scout", help="Discover raw candidate repositories")
    scout_parser.add_argument("--domain", default="personal-code", choices=["personal-code", "nextjs-ui"])
    scout_parser.add_argument("--limit", type=int, default=500)

    qualify_parser = subparsers.add_parser("qualify", help="Clone and qualify repositories into snapshots")
    qualify_parser.add_argument("--limit", type=int, default=100)

    evidence_parser = subparsers.add_parser("evidence", help="Create deterministic evidence assets")
    evidence_parser.add_argument("--capability")
    evidence_parser.add_argument("--domain", choices=["personal-code", "nextjs-ui"])
    evidence_parser.add_argument("--limit", type=int, default=30)

    eval_parser = subparsers.add_parser("eval", help="Run a local golden eval suite")
    eval_parser.add_argument("--suite", default="ui-reuse")
    eval_parser.add_argument("--top-k", type=int, default=5)
    eval_parser.add_argument("--label", default=None)
    eval_parser.add_argument("--output", default=None)

    reuse_loop_parser = subparsers.add_parser(
        "eval-reuse-loop",
        help="Run a compact quality report for find -> assess -> bundle",
    )
    reuse_loop_parser.add_argument("--suite", default="ui-reuse")
    reuse_loop_parser.add_argument("--top-k", type=int, default=3)
    reuse_loop_parser.add_argument("--label", default=None)
    reuse_loop_parser.add_argument("--output", default=None)
    reuse_loop_parser.add_argument("--limit-tasks", type=int, default=None)
    reuse_loop_parser.add_argument(
        "--fastcontext-policy",
        choices=["auto", "always", "never"],
        default="never",
    )
    reuse_loop_parser.add_argument("--max-evidence-rounds", type=int, default=0)
    reuse_loop_parser.add_argument("--use-cache", action="store_true")

    local_eval_parser = subparsers.add_parser(
        "eval-local-explore",
        help="Run a FastContext local exploration golden eval suite",
    )
    local_eval_parser.add_argument("--suite", default="source-scout")
    local_eval_parser.add_argument("--max-turns", type=int, default=fastcontext.DEFAULT_MAX_TURNS)
    local_eval_parser.add_argument("--label", default=None)
    local_eval_parser.add_argument("--output", default=None)
    local_eval_parser.add_argument("--limit-tasks", type=int, default=None)
    local_eval_parser.add_argument("--task-timeout-seconds", type=float, default=None)
    local_eval_parser.add_argument("--progress", action="store_true")

    assess_eval_parser = subparsers.add_parser(
        "eval-assess",
        help="Run a mocked golden eval suite for task-specific reuse assessment",
    )
    assess_eval_parser.add_argument("--suite", default="assessment-smoke")
    assess_eval_parser.add_argument("--label", default=None)
    assess_eval_parser.add_argument("--output", default=None)
    assess_eval_parser.add_argument("--deterministic-only", action="store_true")

    profile_parser = subparsers.add_parser(
        "profile",
        help="Profile repository cards with the configured assessment model",
    )
    profile_parser.add_argument("--limit", type=int, default=30)
    profile_parser.add_argument("--force", action="store_true")
    profile_parser.add_argument("--priority", choices=["created-at", "audit"], default="created-at")
    profile_parser.add_argument("--scope", choices=["downloaded", "cataloged", "all"], default="downloaded")

    audit_parser = subparsers.add_parser(
        "audit",
        help="Audit catalog quality and cleanup candidates",
    )
    audit_parser.add_argument("--limit", type=int, default=10, help="Max repos per bucket")
    audit_parser.add_argument("--bucket", default=None, help="Optional bucket filter")
    audit_parser.add_argument("--scope", choices=["downloaded", "cataloged", "all"], default="downloaded")

    assess_parser = subparsers.add_parser(
        "assess",
        help="Assess one reusable-code candidate for a task",
    )
    assess_parser.add_argument("--candidate-id", required=True)
    assess_parser.add_argument("--task", required=True)
    assess_parser.add_argument(
        "--project-path",
        default=None,
        help="Optional target project path for deterministic compatibility profiling",
    )
    assess_parser.add_argument(
        "--fastcontext-policy",
        choices=["auto", "always", "never"],
        default="auto",
    )
    assess_parser.add_argument("--max-evidence-rounds", type=int, default=1)
    assess_parser.add_argument("--force", action="store_true")

    api_parser = subparsers.add_parser(
        "model-status",
        help="Check the configured model API",
    )
    api_parser.add_argument("--smoke-test", action="store_true")

    refine_parser = subparsers.add_parser(
        "refine-evidence",
        help="Use FastContext to refine evidence for a catalog candidate",
    )
    refine_parser.add_argument("--candidate-id")
    refine_parser.add_argument("--task")
    refine_parser.add_argument("--suite")
    refine_parser.add_argument("--top-k", type=int, default=3)
    refine_parser.add_argument("--label", default=None)
    refine_parser.add_argument("--output", default=None)
    refine_parser.add_argument("--limit-tasks", type=int, default=None)
    refine_parser.add_argument("--max-turns", type=int, default=fastcontext.DEFAULT_MAX_TURNS)

    explore_local_parser = subparsers.add_parser(
        "explore-local",
        help="Use FastContext to find relevant files and lines in a local project",
    )
    explore_local_parser.add_argument("--task", required=True)
    explore_local_parser.add_argument("--project-path", default=".")
    explore_local_parser.add_argument("--max-turns", type=int, default=fastcontext.DEFAULT_MAX_TURNS)
    explore_local_parser.add_argument("--format", choices=["json", "text"], default="json")
    explore_local_parser.add_argument("--trace-path", default=None)

    serve_parser = subparsers.add_parser("serve-mcp", help="Run the MCP server")
    serve_parser.add_argument("--transport", choices=["stdio", "http"], default=None)
    serve_parser.add_argument("--port", type=int, default=None)

    gc_parser = subparsers.add_parser("gc", help="Garbage-collect old local snapshots")
    gc_parser.add_argument("--keep-per-repo", type=int, default=2)

    args = parser.parse_args()

    if args.command in (None, "serve-mcp"):
        transport = args.transport
        port = args.port
        if args.command == "serve-mcp":
            transport = args.transport or parser.get_default("transport")
            port = args.port or parser.get_default("port")
        _run_mcp(str(transport), int(port))
        return

    if args.command == "check":
        _run_check_commands(args.with_local_explore_eval)
        return

    if args.command == "scout":
        _require_github_token()
        from .pipeline import scout

        result = asyncio.run(scout(args.domain, args.limit))
        print(result)
        return

    if args.command == "qualify":
        _require_github_token()
        from .pipeline import qualify

        result = asyncio.run(qualify(args.limit))
        print(result)
        return

    if args.command == "evidence":
        from .evidence import run_evidence, run_evidence_domain

        if args.domain and args.capability:
            evidence_parser.error("--domain cannot be combined with --capability.")
        if args.domain:
            result = run_evidence_domain(args.domain, args.limit)
        elif args.capability:
            result = run_evidence(args.capability, args.limit)
        else:
            evidence_parser.error("Either --capability or --domain is required.")
        print(result)
        return

    if args.command == "eval":
        from pathlib import Path

        from .eval_runner import run_eval

        output_path = Path(args.output) if args.output else None
        result = run_eval(args.suite, args.top_k, label=args.label, output_path=output_path)
        summary = {
            "suite_id": result["suite_id"],
            "label": result["label"],
            "passed": result["passed"],
            "metrics": result["metrics"],
            "report_path": result["report_path"],
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return

    if args.command == "eval-reuse-loop":
        from pathlib import Path

        from .eval_runner import run_reuse_loop_report

        output_path = Path(args.output) if args.output else None
        result = asyncio.run(
            run_reuse_loop_report(
                args.suite,
                args.top_k,
                label=args.label,
                output_path=output_path,
                limit_tasks=args.limit_tasks,
                fastcontext_policy=args.fastcontext_policy,
                max_evidence_rounds=args.max_evidence_rounds,
                force_assessment=not args.use_cache,
            )
        )
        summary = {
            "suite_id": result["suite_id"],
            "label": result["label"],
            "passed": result["passed"],
            "metrics": result["metrics"],
            "report_path": result["report_path"],
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return

    if args.command == "eval-local-explore":
        from pathlib import Path

        from .local_explore_eval import run_local_explore_eval

        output_path = Path(args.output) if args.output else None
        result = asyncio.run(
            run_local_explore_eval(
                suite=args.suite,
                max_turns=args.max_turns,
                label=args.label,
                output_path=output_path,
                limit_tasks=args.limit_tasks,
                task_timeout_seconds=args.task_timeout_seconds,
                progress=args.progress,
            )
        )
        summary = {
            "suite_id": result["suite_id"],
            "label": result["label"],
            "passed": result["passed"],
            "metrics": result["metrics"],
            "report_path": result["report_path"],
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return

    if args.command == "eval-assess":
        from pathlib import Path

        from .assessment_eval import run_assessment_eval

        output_path = Path(args.output) if args.output else None
        result = asyncio.run(
            run_assessment_eval(
                suite=args.suite,
                label=args.label,
                output_path=output_path,
                deterministic_only=args.deterministic_only,
            )
        )
        summary = {
            "suite_id": result["suite_id"],
            "label": result["label"],
            "passed": result["passed"],
            "metrics": result["metrics"],
            "failure_examples": result["failure_examples"],
            "report_path": result["report_path"],
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return

    if args.command == "profile":
        from .profiler import profile_repository_cards

        result = asyncio.run(
            profile_repository_cards(
                args.limit,
                force=args.force,
                priority=args.priority,
                scope=args.scope,
            )
        )
        print(result)
        return

    if args.command == "audit":
        from .catalog_audit import audit_catalog

        try:
            result = audit_catalog(limit_per_bucket=args.limit, bucket=args.bucket, scope=args.scope)
        except ValueError as exc:
            audit_parser.error(str(exc))
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if args.command == "assess":
        if args.max_evidence_rounds < 0 or args.max_evidence_rounds > 2:
            assess_parser.error("--max-evidence-rounds must be between 0 and 2.")
        from .assessor import AssessorError, assess_candidate, assessment_to_jsonable
        from .deepseek import ModelError

        try:
            assessment_result = asyncio.run(
                assess_candidate(
                    candidate_id=args.candidate_id,
                    task=args.task,
                    project_path=args.project_path,
                    fastcontext_policy=args.fastcontext_policy,
                    max_evidence_rounds=args.max_evidence_rounds,
                    force=args.force,
                )
            )
        except (AssessorError, ModelError, OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(assessment_to_jsonable(assessment_result), sort_keys=True))
        return

    if args.command == "model-status":
        status_result = asyncio.run(_api_status(args.smoke_test))
        print(json.dumps(status_result, indent=2, sort_keys=True))
        if not status_is_healthy(status_result):
            sys.exit(1)
        return

    if args.command == "refine-evidence":
        from pathlib import Path

        if args.suite:
            if args.candidate_id or args.task:
                refine_parser.error("--suite cannot be combined with --candidate-id or --task.")
            output_path = Path(args.output) if args.output else None
            result = asyncio.run(
                fastcontext.refine_suite(
                    suite=args.suite,
                    top_k=args.top_k,
                    label=args.label,
                    output_path=output_path,
                    max_turns=args.max_turns,
                    limit_tasks=args.limit_tasks,
                )
            )
            summary = {
                "suite_id": result["suite_id"],
                "label": result["label"],
                "metrics": result["metrics"],
                "scoring_recommendation": result["scoring_recommendation"],
                "report_path": result["report_path"],
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
            return
        if not args.candidate_id or not args.task:
            refine_parser.error("--candidate-id and --task are required unless --suite is used.")
        result = asyncio.run(
            fastcontext.refine_candidate(
                candidate_id=args.candidate_id,
                task=args.task,
                max_turns=args.max_turns,
            )
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    if args.command == "explore-local":
        from .deepseek import ModelError

        try:
            local_result = asyncio.run(
                fastcontext.explore_local_project(
                    task=args.task,
                    project_path=args.project_path,
                    max_turns=args.max_turns,
                    trace_path=args.trace_path,
                )
            )
        except (fastcontext.FastContextError, ModelError, OSError, ValueError, TimeoutError) as exc:
            failure = failure_from_exception(exc, stage="exploration")
            print(json.dumps(failure.to_dict(), sort_keys=True), file=sys.stderr)
            sys.exit(1)
        if args.format == "text":
            print(_format_local_explore_text(local_result))
        else:
            print(json.dumps(asdict(local_result), indent=2, sort_keys=True))
        return

    if args.command == "gc":
        from .pipeline import gc

        result = gc(args.keep_per_repo)
        print(result)
        return


if __name__ == "__main__":
    main()
