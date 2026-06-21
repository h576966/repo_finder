import argparse
import asyncio
import json
import os
import sys


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
            "Repo Finder — catalog-first local reuse layer for Next.js/React UI code."
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

    profile_parser = subparsers.add_parser(
        "profile",
        help="Profile repository cards with Gemma via LM Studio",
    )
    profile_parser.add_argument("--limit", type=int, default=30)
    profile_parser.add_argument("--force", action="store_true")

    lmstudio_parser = subparsers.add_parser("lmstudio-status", help="Check local LM Studio connectivity")
    lmstudio_parser.add_argument("--start-server", action="store_true")
    lmstudio_parser.add_argument("--smoke-test", action="store_true")

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

    if args.command == "profile":
        from .profiler import profile_repository_cards

        result = asyncio.run(profile_repository_cards(args.limit, force=args.force))
        print(result)
        return

    if args.command == "lmstudio-status":
        status_result = asyncio.run(_lmstudio_status(args.start_server, args.smoke_test))
        print(json.dumps(status_result, indent=2, sort_keys=True))
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
                "base_url": config.base_url,
                "reachable": False,
                "error": str(exc),
                "hint": "Run repo-finder lmstudio-status --start-server",
            }
        lmstudio.start_server()
        started = True
        await asyncio.sleep(1)
        status = await lmstudio.validate_models(config)

    result: dict[str, object] = {"reachable": True, "started_server": started, **status}
    if smoke_test:
        try:
            result["smoke_test"] = await lmstudio.chat_json(
                model_id=config.gemma_model,
                messages=[
                    {"role": "system", "content": "Return only valid JSON."},
                    {"role": "user", "content": 'Return exactly {"ok": true}.'},
                ],
                config=config,
                max_tokens=100,
            )
        except lmstudio.LMStudioError as exc:
            result["smoke_test"] = {"ok": False, "error": str(exc)}
    return result


if __name__ == "__main__":
    main()
