"""Observe pinned Serena stdio, root, symbols and callers for explicit fixture roots."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tomllib
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parents[1]


async def probe(roots: list[Path], output: Path) -> None:
    config = tomllib.loads((ROOT / ".codex/config.toml").read_text())["mcp_servers"]["serena"]
    output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for source_root in roots:
        args = list(config["args"])
        if "--project" in args:
            args[args.index("--project") + 1] = str(source_root.resolve())
        env = {**os.environ, **config.get("env", {})}
        env["SERENA_HOME"] = str((output.parent / ("serena-" + source_root.name)).resolve())
        env["UV_TOOL_DIR"] = str(ROOT / ".source_scout/tools/uv-tools")
        env["UV_OFFLINE"] = "1"
        transport = StdioTransport(
            config["command"],
            args,
            env=env,
            cwd=str(source_root),
            keep_alive=False,
            log_file=output.with_suffix(".stderr.log"),
        )
        async with Client(transport, timeout=50, init_timeout=50) as client:
            tools = [tool.name for tool in await client.list_tools()]
            assert set(tools) == set(config["enabled_tools"]), tools
            assert "activate_project" not in tools and "find_implementations" not in tools
            observed = {"source_root": str(source_root.resolve()), "tools": tools}
            for name, params in [
                ("get_current_config", {}),
                ("get_symbols_overview", {"relative_path": "flow.py", "depth": 1}),
                (
                    "find_symbol",
                    {"name_path_pattern": "handler", "relative_path": "flow.py", "include_body": True},
                ),
                ("find_referencing_symbols", {"name_path": "handler", "relative_path": "flow.py"}),
            ]:
                result = await client.call_tool(name, params)
                assert not result.is_error, result
                observed[name] = [block.text for block in result.content if hasattr(block, "text")]
            assert "handler" in json.dumps(observed["find_symbol"])
            assert "dispatch" in json.dumps(observed["find_referencing_symbols"])
            results.append(observed)
    output.write_text(json.dumps({"observed": results, "model_calls": 0}, indent=2), encoding="utf-8")
    print("Observed Serena sessions: " + str(len(results)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.source_root, args.output))
