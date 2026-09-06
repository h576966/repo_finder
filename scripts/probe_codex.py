"""Read actual local Codex discovery without starting a model turn."""

from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import threading
import tomllib
from pathlib import Path
from typing import Any


def probe(
    cwds: list[str], output: Path, *, mcp_inventory: bool = False, serena_fixture: bool = False
) -> dict[str, Any]:
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("Codex executable was not found.")
    messages: queue.Queue[dict[str, Any]] = queue.Queue()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_suffix(".stderr.log").open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
        )
        assert process.stdout and process.stdin

        def read() -> None:
            assert process.stdout
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    continue

        threading.Thread(target=read, daemon=True).start()

        def request(index: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert process.stdin
            process.stdin.write(json.dumps({"id": index, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            while True:
                response = messages.get(timeout=30)
                if response.get("id") == index:
                    return response

        try:
            initialized = request(
                1,
                "initialize",
                {
                    "clientInfo": {"name": "source-scout-probe", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            process.stdin.write('{"method":"initialized","params":{}}\n')
            process.stdin.flush()
            skills = request(2, "skills/list", {"cwds": cwds, "forceReload": True})
            plugins = request(3, "plugin/installed", {"cwds": cwds})
            result = {
                "initialized": initialized,
                "skills": skills,
                "plugins": plugins,
                "model_turn_started": False,
            }
            if mcp_inventory:
                # A local thread without turn/start performs discovery, no model inference.
                thread_params: dict[str, Any] = {"cwd": str(Path(cwds[0]).resolve()), "ephemeral": True}
                if serena_fixture:
                    # Separate fixture Git roots do not inherit this repo's local config.
                    repo_config = Path(__file__).resolve().parents[1] / ".codex/config.toml"
                    serena = tomllib.loads(repo_config.read_text())["mcp_servers"]["serena"]
                    thread_params["config"] = {"mcp_servers.serena": serena}
                thread = request(4, "thread/start", thread_params)
                result["thread_discovery"] = thread
                thread_id = thread.get("result", {}).get("thread", {}).get("id")
                if thread_id:
                    result["mcp"] = request(
                        5,
                        "mcpServerStatus/list",
                        {"threadId": thread_id, "detail": "toolsAndAuthOnly", "limit": 100},
                    )
                    if serena_fixture:
                        result["serena_symbol"] = request(
                            6,
                            "mcpServer/tool/call",
                            {
                                "threadId": thread_id,
                                "server": "serena",
                                "tool": "find_symbol",
                                "arguments": {
                                    "relative_path": "flow.py",
                                    "name_path_pattern": "handler",
                                    "include_body": True,
                                },
                            },
                        )
            output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mcp-inventory", action="store_true")
    parser.add_argument("--serena-fixture", action="store_true")
    args = parser.parse_args()
    result = probe(
        args.cwd, args.output, mcp_inventory=args.mcp_inventory, serena_fixture=args.serena_fixture
    )
    skills = result["skills"].get("result", {}).get("data", [])
    for group in skills:
        print(
            json.dumps(
                {
                    "cwd": group.get("cwd"),
                    "errors": group.get("errors"),
                    "skills": [
                        {"name": item.get("name"), "path": item.get("path"), "enabled": item.get("enabled")}
                        for item in group.get("skills", [])
                        if any(
                            word in str(item.get("name", ""))
                            for word in (
                                "investigate-code",
                                "implementation-references",
                                "fastcontext",
                                "source-scout-reuse",
                            )
                        )
                    ],
                }
            )
        )
    print(f"Discovery report: {args.output}")
