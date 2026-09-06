import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from source_scout import deepseek, fastcontext, server
from source_scout.cli_status import _api_status
from source_scout.exploration_policy import remote_exploration_enabled
from source_scout.exploration_trace import summarize_requests
from source_scout.fastcontext_types import FastContextCitation, ObservationSupport
from source_scout.fastcontext_validation import _merge_observation_support, _observation_support
from tests.fastcontext_helpers import _response_message_json, _response_tool_call_json


@pytest.mark.asyncio
async def test_profiles_expose_only_selected_tools():
    assert server.DEFAULT_MCP_TOOL_NAMES == ("explore_local_code",)
    async with Client(server.create_server()) as client:
        assert {t.name for t in await client.list_tools()} == set(server.DEFAULT_MCP_TOOL_NAMES)
        with pytest.raises(Exception, match="[Uu]nknown|not found"):
            await client.call_tool("find_reusable_code", {"task": "x"})
        with pytest.raises(Exception, match="[Uu]nknown|not found"):
            await client.call_tool("model_status", {})
    async with Client(server.create_server("reuse")) as client:
        assert {t.name for t in await client.list_tools()} == {
            "explore_local_code", "model_status", "find_reusable_code", "assess_reusable_code",
            "get_source_bundle", "record_reuse_outcome",
        } == set(server.REUSE_MCP_TOOL_NAMES)
    with pytest.raises(ValueError):
        server.create_server("other")


def test_default_server_does_not_import_catalog():
    run = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import source_scout.server; "
            'assert "source_scout.catalog" not in sys.modules; assert "duckdb" not in sys.modules',
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr


@pytest.mark.asyncio
async def test_disabled_policy_precedes_model_and_source_access(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_SCOUT_REMOTE_EXPLORATION", "false")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key-is-not-activation")

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled exploration must not collect source or access model configuration/network")

    monkeypatch.setattr(deepseek, "get_config", forbidden)
    monkeypatch.setattr(deepseek, "validate_model", forbidden)
    monkeypatch.setattr(fastcontext, "_local_seed_context", forbidden)
    result = await fastcontext.explore_local_project("trace", tmp_path, validate_model=True)
    assert result.status == "disabled"
    assert (await server.explore_local_code("trace", str(tmp_path))).status == "disabled"
    assert (await server.model_status(True))["status"] == "disabled"
    assert (await _api_status(True))["status"] == "disabled"
    assert not (tmp_path / ".source_scout").exists()


def test_disabled_cli_is_clean_json(monkeypatch, capsys, tmp_path):
    from source_scout.__main__ import main

    monkeypatch.setenv("SOURCE_SCOUT_REMOTE_EXPLORATION", "0")
    monkeypatch.setattr(
        sys, "argv", ["source-scout", "explore-local", "--task", "trace", "--project-path", str(tmp_path)]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"


def test_project_policy_and_process_override(monkeypatch, tmp_path):
    monkeypatch.delenv("SOURCE_SCOUT_REMOTE_EXPLORATION", raising=False)
    assert not remote_exploration_enabled(tmp_path)
    policy = tmp_path / ".source-scout.toml"
    policy.write_text("[remote_exploration]\nenabled = true\n")
    assert remote_exploration_enabled(tmp_path)
    monkeypatch.setenv("SOURCE_SCOUT_REMOTE_EXPLORATION", "false")
    assert not remote_exploration_enabled(tmp_path)
    monkeypatch.delenv("SOURCE_SCOUT_REMOTE_EXPLORATION")
    policy.write_text("invalid toml")
    assert not remote_exploration_enabled(tmp_path)


@pytest.mark.parametrize(
    ("ranges", "accepted"),
    [
        ([(3, 4)], False),
        ([(1, 2), (3, 5)], True),
        ([(1, 2), (4, 5)], False),
        ([(1, 5)], True),
        ([(1, 20)], True),
        ([], False),
    ],
)
def test_citations_require_full_coverage(ranges, accepted):
    support = ObservationSupport({"a.py"}, {"a.py": ranges})
    note = fastcontext._support_validation_note("a.py", FastContextCitation("a.py", 1, 5), support)
    assert (note is None) == accepted


def test_metadata_and_truncated_grep_do_not_count_as_read_lines(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("first\n" + "long" * 100 + "\nlast\n")
    support = _observation_support(
        [{"ok": True, "tool": "Read", "result": {"path": "a.py", "start_line": 1, "end_line": 3}}]
    )
    assert not support.ranges
    observed = fastcontext.execute_tool(tmp_path, {"tool": "Grep", "args": {"pattern": "first", "-C": 2}})
    support = _observation_support([observed])
    assert fastcontext._support_validation_note("a.py", FastContextCitation("a.py", 1, 3), support)
    observed = fastcontext.execute_tool(tmp_path, {"tool": "Grep", "args": {"pattern": "long"}})
    assert not _observation_support([observed]).ranges


def test_union_of_reads_and_stale_content(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("one\ntwo\nthree\nfour\n")

    def read(start, limit):
        return fastcontext.execute_tool(
            tmp_path, {"tool": "Read", "args": {"path": "a.py", "offset": start, "limit": limit}}
        )

    first, second = read(1, 2), read(3, 2)
    support = _merge_observation_support(_observation_support([first]), _observation_support([second]))
    citation = FastContextCitation("a.py", 1, 4)
    assert fastcontext._validated_evidence_paths(tmp_path, [citation], support) == (["a.py:1-4"], [])
    source.write_text("new\ntwo\nthree\nfour\n")
    paths, notes = fastcontext._validated_evidence_paths(tmp_path, [citation], support)
    assert not paths and "stale" in notes[0]
    mixed = _merge_observation_support(support, _observation_support([read(3, 2)]))
    assert "a.py" in mixed.stale_files and "a.py" not in mixed.ranges


@pytest.mark.parametrize("name", [".env", ".env.local", "private.key", "credentials.json", ".ssh/id_rsa"])
def test_sensitive_files_blocked_in_tools_and_seed(tmp_path, name):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('secret = "do not transmit"')
    read = fastcontext.execute_tool(tmp_path, {"tool": "Read", "args": {"path": name}})
    assert read["ok"] is False
    seed = fastcontext._local_seed_context(tmp_path, "secret credentials")
    assert "do not transmit" not in json.dumps(seed)
    assert not fastcontext.grep_paths(tmp_path, "secret")["matches"]


def test_symlink_outside_root_is_not_seed_context(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def secret_outside_root(): pass\n")
    link = root / "link.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable on host: {exc}")
    assert not fastcontext.execute_tool(root, {"tool": "Read", "args": {"path": "link.py"}})["ok"]
    assert "secret_outside_root" not in json.dumps(fastcontext._local_seed_context(root, "secret outside"))


@pytest.fixture
def enabled_project(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_SCOUT_REMOTE_EXPLORATION", "true")
    (tmp_path / "a.py").write_text("def actual_symbol():\n    return 1\n")
    return tmp_path


@pytest.mark.asyncio
async def test_reason_is_required_when_enabled(enabled_project):
    with pytest.raises(fastcontext.FastContextError, match="reason"):
        await fastcontext.explore_local_project("trace", enabled_project)


@pytest.mark.asyncio
async def test_call_budget_includes_finalization_and_missing_usage(enabled_project):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        response = _response_tool_call_json("Read", {"path": "a.py", "offset": 1, "limit": 2})
        response.pop("usage", None)
        return httpx.Response(200, json=response)

    result = await fastcontext.explore_local_project(
        "trace actual_symbol",
        enabled_project,
        max_turns=1,
        reason="rg left an unresolved caller",
        transport=httpx.MockTransport(handler),
    )
    assert calls == 1 and result.status == "incomplete" and result.missing_context
    assert result.stop_reason == "call_budget"
    report = json.loads(Path(result.report_path).read_text())
    assert report["accounting"]["usage"]["input_tokens"] is None
    assert report["accounting"]["request_count"] == 1
    assert report["accounting"]["known_retries"] == 0
    assert report["reason"] == "rg left an unresolved caller"
    assert result.tool_trace == []


@pytest.mark.asyncio
async def test_deadline_preserves_accounting_and_returns_controlled_result(enabled_project):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=_response_message_json("{}"))

    result = await fastcontext.explore_local_project(
        "trace",
        enabled_project,
        reason="unresolved caller",
        deadline_seconds=0.05,
        transport=httpx.MockTransport(handler),
    )
    assert result.status == "incomplete" and result.stop_reason == "deadline"
    report = json.loads(Path(result.report_path).read_text())
    assert report["accounting"]["request_count"] <= 1
    assert report["accounting"]["usage"]["input_tokens"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 404, 429, 503])
async def test_unavailable_does_not_retry_or_switch_models(enabled_project, status_code):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(status_code, json={"error": {"message": "unavailable"}})

    result = await fastcontext.explore_local_project(
        "trace", enabled_project, reason="unresolved cross-file trace", transport=httpx.MockTransport(handler)
    )
    assert result.status == "unavailable" and calls == [deepseek.DEEPSEEK_MODEL]
    report = json.loads(Path(result.report_path).read_text())
    assert report["accounting"]["request_count"] == 1


@pytest.mark.asyncio
async def test_missing_key_is_unavailable(enabled_project, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    result = await fastcontext.explore_local_project("trace", enabled_project, reason="unresolved caller")
    assert result.status == "unavailable"
    report = json.loads(Path(result.report_path).read_text())
    assert report["accounting"]["request_count"] == 0
    assert report["accounting"]["usage"]["input_tokens"] is None


def test_usage_counts_requests_not_synthetic_fallbacks():
    trajectory = [
        {
            "request_index": 1,
            "response_model": "returned",
            "usage": {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 4}, "output_tokens": 3},
        },
        {"finish_reason": "observation_fallback"},
        {
            "request_index": 2,
            "response_model": "returned",
            "usage": {"input_tokens": 20, "input_tokens_details": {"cached_tokens": 8}, "output_tokens": 6},
        },
    ]
    summary = summarize_requests(trajectory)
    assert summary["usage"] == {"input_tokens": 30, "cached_input_tokens": 12, "output_tokens": 9}
    assert summary["request_count"] == 2 and summary["returned_models"] == ["returned"]
    trajectory.append({"request_index": 3, "usage": None})
    assert all(v is None for v in summarize_requests(trajectory)["usage"].values())


@pytest.mark.asyncio
async def test_final_answer_can_report_missing_context(enabled_project):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=_response_tool_call_json("Read", {"path": "a.py"}))
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "evidence": [{"path": "a.py", "start_line": 1, "end_line": 2}],
                            "missing_context": True,
                            "notes": ["The caller contract remains unobserved."],
                        }
                    }
                )
            ),
        )

    result = await fastcontext.explore_local_project(
        "trace", enabled_project, reason="unresolved caller", transport=httpx.MockTransport(handler)
    )
    assert result.status == "incomplete" and result.missing_context and calls == 2


@pytest.mark.asyncio
async def test_cancelled_exploration_saves_journal(enabled_project):
    async def handler(request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await fastcontext.explore_local_project(
            "trace", enabled_project, reason="unresolved caller", transport=httpx.MockTransport(handler)
        )
    reports = list((enabled_project / ".source_scout/explorations").glob("*/report.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["status"] == "cancelled" and report["accounting"]["request_count"] == 1


def test_full_coverage_not_lost_at_compact_choice_boundary():
    support = ObservationSupport({"a.py"}, {"a.py": [(1, 160), (161, 320)]})
    assert (
        fastcontext._support_validation_note("a.py", FastContextCitation("a.py", 250, 300), support) is None
    )


@pytest.mark.asyncio
async def test_stale_fallback_cannot_escape_final_validation(enabled_project):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=_response_tool_call_json("Read", {"path": "a.py"}))
        (enabled_project / "a.py").write_text("def changed_symbol():\n    return 9\n")
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps({"final_answer": {"evidence": [{"path": "a.py", "start_line": 1, "end_line": 2}]}})
            ),
        )

    result = await fastcontext.explore_local_project(
        "trace",
        enabled_project,
        max_turns=2,
        reason="unresolved caller",
        transport=httpx.MockTransport(handler),
    )
    assert result.status == "incomplete" and not result.evidence_paths
    assert any("stale" in note for note in result.notes)


@pytest.mark.asyncio
async def test_validation_consumes_call_budget_before_source_collection(enabled_project, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"data": [{"id": deepseek.DEEPSEEK_MODEL}]})

    def forbidden(*args):
        pytest.fail("No source collection after validation exhausts the call budget")

    monkeypatch.setattr(fastcontext, "_local_seed_context", forbidden)
    result = await fastcontext.explore_local_project(
        "trace",
        enabled_project,
        reason="unresolved caller",
        max_turns=1,
        validate_model=True,
        transport=httpx.MockTransport(handler),
    )
    assert result.status == "incomplete" and result.stop_reason == "call_budget"
    assert requests == ["/models"]


def test_mcp_launcher_disables_update_requests(monkeypatch):
    from fastmcp import settings

    from source_scout.__main__ import _run_mcp

    monkeypatch.setattr(settings, "check_for_updates", "stable")
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))
    _run_mcp("stdio", 8000)
    assert settings.check_for_updates == "off"
    assert calls == [{"show_banner": False}]
