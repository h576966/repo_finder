import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict


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
        status_result = asyncio.run(_lmstudio_status(args.start_server, args.smoke_test))
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


async def _lmstudio_status(start_server: bool, smoke_test: bool) -> dict[str, object]:
    from . import lmstudio

    config = lmstudio.get_config()
    started = False
    try:
        status = await lmstudio.validate_models(config)
    except lmstudio.LMStudioError as exc:
        if not start_server:
            return {
                "reachable": False,
                "error": str(exc),
                "hint": "Run source-scout lmstudio-status --start-server",
                **_status_with_inventory(_offline_status(config), config),
            }
        try:
            lmstudio.start_server(config)
        except lmstudio.LMStudioError as start_exc:
            return {
                "reachable": False,
                "started_server": False,
                "error": str(exc),
                "start_error": str(start_exc),
                "hint": "Start LM Studio Local Server from the LM Studio UI, then rerun this command.",
                **_status_with_inventory(_offline_status(config), config),
            }
        started = True
        await asyncio.sleep(1)
        status = await lmstudio.validate_models(config)

    result: dict[str, object] = {
        "reachable": True,
        "started_server": started,
        **_status_with_inventory(status, config),
    }
    if smoke_test:
        try:
            smoke_result = await lmstudio.chat_json(
                model_id=config.gemma_model,
                messages=[
                    {"role": "system", "content": "Return only valid JSON."},
                    {"role": "user", "content": 'Return exactly {"ok": true}.'},
                ],
                config=config,
                max_tokens=100,
            )
            result["gemma_smoke_test"] = {"completed": True, "response": smoke_result}
        except lmstudio.LMStudioError as exc:
            result["gemma_smoke_test"] = {"completed": False, "error": str(exc)}
    return result


def _format_local_explore_text(result: object) -> str:
    evidence_paths = getattr(result, "evidence_paths")
    notes = getattr(result, "notes")
    lines = [
        f"Task: {getattr(result, 'task')}",
        f"Project: {getattr(result, 'project_path')}",
        f"Status: {getattr(result, 'status')}",
        "",
        "Citations:",
    ]
    lines.extend(f"- {path}" for path in evidence_paths)
    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines)


async def _fastcontext_status(
    start_server: bool,
    smoke_test: bool,
    load_model: bool = False,
    context_length: int = 65_536,
    gpu: str = "max",
) -> dict[str, object]:
    from . import fastcontext, lmstudio

    config = lmstudio.get_config()
    started = False
    try:
        status = await lmstudio.validate_models(config)
    except lmstudio.LMStudioError as exc:
        if not start_server:
            return {
                "reachable": False,
                "error": str(exc),
                "hint": "Run source-scout fastcontext-status --start-server",
                **_status_with_inventory(_offline_status(config), config),
            }
        try:
            lmstudio.start_server(config)
        except lmstudio.LMStudioError as start_exc:
            return {
                "reachable": False,
                "started_server": False,
                "error": str(exc),
                "start_error": str(start_exc),
                "hint": "Start LM Studio Local Server from the LM Studio UI, then rerun this command.",
                **_status_with_inventory(_offline_status(config), config),
            }
        started = True
        await asyncio.sleep(1)
        status = await lmstudio.validate_models(config)

    load_result: dict[str, object] | None = None
    inventory_status = _status_with_inventory(status, config)
    fastcontext_state = _configured_model_state(inventory_status, "fastcontext")
    if load_model and not bool(fastcontext_state.get("loaded")):
        try:
            load_result = lmstudio.load_fastcontext_model(
                config,
                context_length=context_length,
                gpu=gpu,
            )
            await asyncio.sleep(1)
            status = await lmstudio.validate_models(config)
            inventory_status = _status_with_inventory(status, config)
        except lmstudio.LMStudioError as exc:
            load_result = {
                "model_id": config.fastcontext_model,
                "context_length": context_length,
                "gpu": gpu,
                "loaded": False,
                "error": str(exc),
            }

    result: dict[str, object] = {
        "reachable": True,
        "started_server": started,
        "load_model_requested": load_model,
        **inventory_status,
    }
    if load_result is not None:
        result["load_model"] = load_result
    if smoke_test:
        try:
            smoke_result = await fastcontext.smoke_test(config)
            result["fastcontext_smoke_test"] = {"completed": True, "response": smoke_result}
        except (fastcontext.FastContextError, lmstudio.LMStudioError) as exc:
            result["fastcontext_smoke_test"] = {"completed": False, "error": str(exc)}
    return result


def _status_with_inventory(
    status: dict[str, object],
    config: object,
) -> dict[str, object]:
    from . import lmstudio

    result: dict[str, object] = dict(status)
    api_models = status.get("models")
    api_model_ids = set(api_models) if isinstance(api_models, list) else set()
    try:
        inventory = lmstudio.model_inventory(config if isinstance(config, lmstudio.LMStudioConfig) else None)
    except lmstudio.LMStudioError as exc:
        result["inventory_error"] = str(exc)
        inventory = {
            "downloaded_models": [],
            "loaded_models": [],
            "configured_models": {
                "gemma": {
                    "model_id": getattr(config, "gemma_model", lmstudio.DEFAULT_GEMMA_MODEL),
                    "downloaded": False,
                    "loaded": False,
                    "loaded_detail": None,
                },
                "fastcontext": {
                    "model_id": getattr(
                        config,
                        "fastcontext_model",
                        lmstudio.DEFAULT_FASTCONTEXT_MODEL,
                    ),
                    "downloaded": False,
                    "loaded": False,
                    "loaded_detail": None,
                },
            },
        }
    configured = inventory["configured_models"]
    if isinstance(configured, dict):
        for key, value in configured.items():
            if isinstance(value, dict):
                value["api_listed"] = value.get("model_id") in api_model_ids
    result.update(inventory)
    return result


def _offline_status(config: object) -> dict[str, object]:
    from . import lmstudio

    return {
        "base_url": getattr(config, "base_url", lmstudio.DEFAULT_BASE_URL),
        "models": [],
        "gemma_model": getattr(config, "gemma_model", lmstudio.DEFAULT_GEMMA_MODEL),
        "fastcontext_model": getattr(
            config,
            "fastcontext_model",
            lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        ),
        "gemma_available": False,
        "fastcontext_available": False,
    }


def _configured_model_state(status: dict[str, object], key: str) -> dict[str, object]:
    configured = status.get("configured_models")
    if not isinstance(configured, dict):
        return {}
    state = configured.get(key)
    return state if isinstance(state, dict) else {}


if __name__ == "__main__":
    main()
