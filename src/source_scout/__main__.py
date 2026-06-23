import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict

from . import cli_checks as _cli_checks
from .cli_output import _format_local_explore_text
from .cli_status import _fastcontext_status, _lmstudio_status

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
        description=(
            "Source Scout — catalog-first local reuse layer for Next.js/React UI code."
        ),
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

    scout_parser = subparsers.add_parser("scout", help="Discover raw Next.js UI candidate repositories")
    scout_parser.add_argument("--domain", default="nextjs-ui", choices=["nextjs-ui"])
    scout_parser.add_argument("--limit", type=int, default=500)

    qualify_parser = subparsers.add_parser("qualify", help="Clone and qualify repositories into snapshots")
    qualify_parser.add_argument("--limit", type=int, default=100)

    evidence_parser = subparsers.add_parser("evidence", help="Create deterministic evidence assets")
    evidence_parser.add_argument("--capability", required=True)
    evidence_parser.add_argument("--limit", type=int, default=30)

    eval_parser = subparsers.add_parser("eval", help="Run a local golden eval suite")
    eval_parser.add_argument("--suite", default="ui-reuse")
    eval_parser.add_argument("--top-k", type=int, default=5)
    eval_parser.add_argument("--label", default=None)
    eval_parser.add_argument("--output", default=None)

    local_eval_parser = subparsers.add_parser(
        "eval-local-explore",
        help="Run a FastContext local exploration golden eval suite",
    )
    local_eval_parser.add_argument("--suite", default="source-scout")
    local_eval_parser.add_argument("--max-turns", type=int, default=6)
    local_eval_parser.add_argument("--label", default=None)
    local_eval_parser.add_argument("--output", default=None)
    local_eval_parser.add_argument("--limit-tasks", type=int, default=None)

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
        help="Profile repository cards with Gemma via LM Studio",
    )
    profile_parser.add_argument("--limit", type=int, default=30)
    profile_parser.add_argument("--force", action="store_true")

    assess_parser = subparsers.add_parser(
        "assess",
        help="Assess one reusable-code candidate for a task",
    )
    assess_parser.add_argument("--candidate-id", required=True)
    assess_parser.add_argument("--task", required=True)
    assess_parser.add_argument(
        "--fastcontext-policy",
        choices=["auto", "always", "never"],
        default="auto",
    )
    assess_parser.add_argument("--max-evidence-rounds", type=int, default=1)
    assess_parser.add_argument("--force", action="store_true")

    lmstudio_parser = subparsers.add_parser("lmstudio-status", help="Check local LM Studio connectivity")
    lmstudio_parser.add_argument("--start-server", action="store_true")
    lmstudio_parser.add_argument("--smoke-test", action="store_true")
    lmstudio_parser.add_argument("--load-gemma", action="store_true")
    lmstudio_parser.add_argument("--gemma-context-length", type=int, default=32_768)
    lmstudio_parser.add_argument("--gemma-gpu", default="max")

    fastcontext_parser = subparsers.add_parser(
        "fastcontext-status",
        help="Check local FastContext model connectivity through LM Studio",
    )
    fastcontext_parser.add_argument("--start-server", action="store_true")
    fastcontext_parser.add_argument("--smoke-test", action="store_true")
    fastcontext_parser.add_argument("--load-model", action="store_true")
    fastcontext_parser.add_argument("--context-length", type=int, default=65_536)
    fastcontext_parser.add_argument("--gpu", default="max")

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
    refine_parser.add_argument("--max-turns", type=int, default=6)

    explore_local_parser = subparsers.add_parser(
        "explore-local",
        help="Use FastContext to find relevant files and lines in a local project",
    )
    explore_local_parser.add_argument("--task", required=True)
    explore_local_parser.add_argument("--project-path", default=".")
    explore_local_parser.add_argument("--max-turns", type=int, default=6)
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
        from .evidence import run_evidence

        result = run_evidence(args.capability, args.limit)
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

        result = asyncio.run(profile_repository_cards(args.limit, force=args.force))
        print(result)
        return

    if args.command == "assess":
        if args.max_evidence_rounds < 0 or args.max_evidence_rounds > 2:
            assess_parser.error("--max-evidence-rounds must be between 0 and 2.")
        from .assessor import AssessorError, assess_candidate, assessment_to_jsonable
        from .lmstudio import LMStudioError

        try:
            assessment_result = asyncio.run(
                assess_candidate(
                    candidate_id=args.candidate_id,
                    task=args.task,
                    fastcontext_policy=args.fastcontext_policy,
                    max_evidence_rounds=args.max_evidence_rounds,
                    force=args.force,
                )
            )
        except (AssessorError, LMStudioError, OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(assessment_to_jsonable(assessment_result), sort_keys=True))
        return

    if args.command == "lmstudio-status":
        status_result = asyncio.run(
            _lmstudio_status(
                args.start_server,
                args.smoke_test,
                load_gemma=args.load_gemma,
                gemma_context_length=args.gemma_context_length,
                gemma_gpu=args.gemma_gpu,
            )
        )
        print(json.dumps(status_result, indent=2, sort_keys=True))
        return

    if args.command == "fastcontext-status":
        status_result = asyncio.run(
            _fastcontext_status(
                args.start_server,
                args.smoke_test,
                load_model=args.load_model,
                context_length=args.context_length,
                gpu=args.gpu,
            )
        )
        print(json.dumps(status_result, indent=2, sort_keys=True))
        return

    if args.command == "refine-evidence":
        from pathlib import Path

        from . import fastcontext

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
        from . import fastcontext

        local_result = asyncio.run(
            fastcontext.explore_local_project(
                task=args.task,
                project_path=args.project_path,
                max_turns=args.max_turns,
                trace_path=args.trace_path,
            )
        )
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
