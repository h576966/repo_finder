import json
import tomllib
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from source_scout import deepseek, fastcontext, server
from source_scout.exploration_policy import remote_exploration_mode
from tests.fastcontext_helpers import _response_tool_call_json


def test_committed_default_and_codex_override(monkeypatch):
    monkeypatch.delenv("SOURCE_SCOUT_REMOTE_EXPLORATION", raising=False)
    root = Path(__file__).resolve().parents[1]
    assert remote_exploration_mode(root) == "selective"
    config = tomllib.loads((root / ".codex/config.toml").read_text())
    scout = config["mcp_servers"]["source_scout"]
    assert scout["enabled_tools"] == ["explore_local_code"]
    assert "SOURCE_SCOUT_REMOTE_EXPLORATION" not in scout.get("env", {})


@pytest.mark.parametrize(("value", "mode"), [
    ("off", "off"), ("selective", "selective"), ("on", "on"),
    ("false", "off"), ("0", "off"), ("no", "off"),
    ("true", "on"), ("1", "on"), ("yes", "on"), (" ON ", "on"),
    ("invalid", "off"), ("", "off"), ("enabled", "off"),
])
def test_environment_overrides_project(monkeypatch, tmp_path, value, mode):
    (tmp_path / ".source-scout.toml").write_text('[remote_exploration]\nmode = "on"\n')
    monkeypatch.setenv("SOURCE_SCOUT_REMOTE_EXPLORATION", value)
    assert remote_exploration_mode(tmp_path) == mode


@pytest.mark.parametrize(("config", "mode"), [
    ('mode = "selective"', "selective"), ('mode = "off"', "off"), ('mode = "on"', "on"),
    ("enabled = false", "off"), ("enabled = true", "on"),
    ('mode = "selective"\nenabled = false', "selective"),
    ('mode = "off"\nenabled = true', "off"),
    ('mode = "invalid"\nenabled = true', "off"),
    ('mode = true\nenabled = true', "off"), ('mode = 1', "off"),
    ('mode = ["on"]', "off"), ('enabled = "true"', "off"), ("broken toml", "off"),
])
def test_project_modes_and_legacy(monkeypatch, tmp_path, config, mode):
    monkeypatch.delenv("SOURCE_SCOUT_REMOTE_EXPLORATION", raising=False)
    (tmp_path / ".source-scout.toml").write_text("[remote_exploration]\n" + config)
    assert remote_exploration_mode(tmp_path) == mode


@pytest.mark.parametrize("config", ["", "remote_exploration = 1", 'remote_exploration = ["on"]'])
def test_missing_or_malformed_section_fails_closed(monkeypatch, tmp_path, config):
    monkeypatch.delenv("SOURCE_SCOUT_REMOTE_EXPLORATION", raising=False)
    assert remote_exploration_mode(tmp_path) == "off"
    (tmp_path / ".source-scout.toml").write_text(config)
    assert remote_exploration_mode(tmp_path) == "off"


@pytest.fixture
def no_remote_or_source(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Rejected routes must precede model configuration, key access and source/network work")
    monkeypatch.setattr(deepseek, "get_config", forbidden)
    monkeypatch.setattr(deepseek, "validate_model", forbidden)
    monkeypatch.setattr(fastcontext, "_local_seed_context", forbidden)
    monkeypatch.setattr(fastcontext, "_run_tool_loop", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "invalid"])
async def test_off_does_no_work(monkeypatch, tmp_path, no_remote_or_source, mode):
    monkeypatch.setenv("SOURCE_SCOUT_REMOTE_EXPLORATION", mode)
    result = await fastcontext.explore_local_project("", tmp_path, validate_model=True)
    assert result.status == "disabled" and result.stop_reason == "policy_disabled"
    assert not (tmp_path / ".source_scout").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("arguments", "error"), [
    ({"use_case": None}, "use_case"),
    ({"use_case": "review"}, "use_case"),
    ({"use_case": "unknown"}, "use_case"),
    ({"reason": " \n "}, "reason"),
    ({"task": " "}, "task"),
    ({"attempted_local_methods": None}, "attempted_local_methods"),
    ({"attempted_local_methods": []}, "attempted_local_methods"),
    ({"attempted_local_methods": ["model_status"]}, "attempted_local_methods"),
    ({"attempted_local_methods": "rg"}, "attempted_local_methods"),
])
async def test_selective_rejects_before_access(monkeypatch, tmp_path, no_remote_or_source, arguments, error):
    monkeypatch.setenv("SOURCE_SCOUT_REMOTE_EXPLORATION", "selective")
    route = dict(task="Trace caller contract", reason="Local references leave the return contract unclear",
                 use_case="cross_file_contract", attempted_local_methods=["rg"])
    route.update(arguments)
    with pytest.raises(fastcontext.FastContextError, match=error):
        await fastcontext.explore_local_project(project_path=tmp_path, validate_model=True, **route)
    assert not (tmp_path / ".source_scout").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "use_case", "attempts"), [
    ("selective", "cross_file_contract", ["rg"]),
    ("selective", "indirect_runtime_flow", ["direct_read"]),
    ("selective", "ambiguous_ownership", ["serena"]),
    ("selective", "architecture_trace", ["rg", "direct_read", "rg"]),
    ("on", None, None),
])
async def test_allowed_routes_and_journal(monkeypatch, tmp_path, mode, use_case, attempts):
    monkeypatch.setenv("SOURCE_SCOUT_REMOTE_EXPLORATION", mode)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    (tmp_path / "a.py").write_text("def actual_symbol():\n    return 1\n")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=_response_tool_call_json(
            "Read", {"path": "a.py", "offset": 1, "limit": 2}
        ))
    reason = "  Local references leave the return contract unclear  "
    result = await fastcontext.explore_local_project(
        "Trace actual_symbol's caller contract", tmp_path, max_turns=1, reason=reason,
        use_case=use_case, attempted_local_methods=attempts, transport=httpx.MockTransport(handler),
    )
    assert len(calls) == 1 and result.stop_reason == "call_budget"
    report = json.loads(Path(result.report_path).read_text())
    assert report["report_schema_version"] == "source-scout-exploration-v2"
    assert report["policy_mode"] == mode and report["use_case"] == use_case
    assert report["attempted_local_methods"] == list(dict.fromkeys(attempts or []))
    assert report["reason"] == reason.strip()
    assert report["accounting"]["request_count"] == 1
    assert report["sdk_max_retries"] == 0
    assert result.tool_trace == []


@pytest.mark.asyncio
async def test_mcp_validates_routing_schema(monkeypatch, tmp_path, no_remote_or_source):
    monkeypatch.setenv("SOURCE_SCOUT_REMOTE_EXPLORATION", "selective")
    async with Client(server.create_server()) as client:
        for arguments in ({"use_case": "review"}, {"attempted_local_methods": ["unknown"]}):
            with pytest.raises(Exception, match="use_case|attempted_local_methods"):
                await client.call_tool("explore_local_code", {
                    "task": "trace", "reason": "unresolved call", "project_path": str(tmp_path),
                    **arguments,
                })
