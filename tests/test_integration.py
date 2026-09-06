"""Offline CLI/stdio integration, separate source roots and shared collection."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from source_scout import implementation_references
from source_scout.server import DEFAULT_MCP_TOOL_NAMES
from tests.test_implementation_references import _repository


@pytest.mark.asyncio
async def test_two_stdio_sessions_share_collection_and_keep_worktree_roots(tmp_path, isolated_catalog):
    root, commit = _repository(tmp_path)
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), commit],
        capture_output=True,
        check=True,
    )
    await implementation_references.add_reference_source(root)
    source_path = str(Path(__file__).resolve().parents[1] / "src")
    for project in (root, worktree):
        policy = project / ".source-scout.toml"
        policy.write_text('[remote_exploration]\nmode = "off"\n')

    def connect(project):
        env = {**os.environ, "PYTHONPATH": source_path, "SOURCE_SCOUT_HOME": str(isolated_catalog)}
        env.pop("DEEPSEEK_API_KEY", None)
        env.pop("SOURCE_SCOUT_REMOTE_EXPLORATION", None)
        return Client(
            StdioTransport(
                sys.executable,
                ["-m", "source_scout", "serve-mcp"],
                cwd=str(project),
                env=env,
                keep_alive=False,
            ),
            timeout=20,
        )

    async with connect(root) as first, connect(worktree) as second:
        for client in (first, second):
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == set(DEFAULT_MCP_TOOL_NAMES)
            schema = next(tool.inputSchema for tool in tools if tool.name == "investigate_code")
            assert "source_root" in schema["properties"] and "anchors" in schema["properties"]
        results = await asyncio.gather(
            *[
                client.call_tool(
                    "find_implementation_references",
                    {
                        "task": "bounded decorrelated jitter retry schedule",
                        "target_project_path": str(project),
                    },
                )
                for client, project in ((first, root), (second, worktree))
            ]
        )
        matches = [json.loads(result.content[0].text) for result in results]
        reference_id = matches[0]["results"][0]["reference_id"]
        assert matches[1]["results"][0]["reference_id"] == reference_id
        context = await second.call_tool("get_implementation_reference", {"reference_id": reference_id})
        assert json.loads(context.content[0].text)["commit_sha"] == commit
        for client, project in ((first, root), (second, worktree)):
            result = await client.call_tool(
                "investigate_code",
                {
                    "task": "trace missing runtime mapping",
                    "source_root": str(project),
                    "anchors": [{"path": "../not-permitted.py"}],
                },
            )
            output = json.loads(result.content[0].text)
            assert output["status"] == "disabled" and output["model_id"] == ""
            assert Path(output["project_path"]) == project


def test_check_import_and_help_without_optional_dependencies():
    script = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'openai', 'duckdb', 'httpx', 'fastmcp', 'git', 'pydantic'}:
            raise AssertionError('Unused dependency imported: ' + fullname)
sys.meta_path.insert(0, Block())
from source_scout.__main__ import main
sys.argv = ['source-scout', 'check', '--help']
main()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--timeout-seconds" in result.stdout
