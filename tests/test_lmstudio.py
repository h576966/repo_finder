import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from source_scout import catalog, lmstudio, pipeline, profiler


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(tmp_path / ".source_scout"))
    catalog.reset_connection()
    yield
    catalog.reset_connection()


def test_lmstudio_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("LM_STUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("SOURCE_SCOUT_GEMMA_MODEL", raising=False)
    monkeypatch.delenv("SOURCE_SCOUT_FASTCONTEXT_MODEL", raising=False)
    monkeypatch.delenv("SOURCE_SCOUT_LMSTUDIO_TIMEOUT", raising=False)

    config = lmstudio.get_config()
    assert config.base_url == "http://127.0.0.1:1234/v1"
    assert config.gemma_model == "google/gemma-4-12b-qat"
    assert config.fastcontext_model == "fastcontext-1.0-4b-rl"
    assert config.timeout_seconds == 30.0


def test_lmstudio_config_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://localhost:9999/v1/")
    monkeypatch.setenv("SOURCE_SCOUT_GEMMA_MODEL", "gemma-local")
    monkeypatch.setenv("SOURCE_SCOUT_FASTCONTEXT_MODEL", "fastcontext-local")
    monkeypatch.setenv("SOURCE_SCOUT_LMSTUDIO_TIMEOUT", "7")

    config = lmstudio.get_config()
    assert config.base_url == "http://localhost:9999/v1"
    assert config.gemma_model == "gemma-local"
    assert config.fastcontext_model == "fastcontext-local"
    assert config.timeout_seconds == 7.0


@pytest.mark.asyncio
async def test_list_models_parses_openai_compatible_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]})

    transport = httpx.MockTransport(handler)
    models = await lmstudio.list_models(transport=transport)
    assert models == ["model-a", "model-b"]


def test_model_inventory_distinguishes_downloaded_and_loaded(monkeypatch) -> None:
    def fake_run(command: list[str], **kwargs: Any) -> object:
        assert kwargs["check"] is True
        assert kwargs["capture_output"] is True
        if command[1:] == ["ls", "--json"]:
            stdout = json.dumps(
                [
                    {"modelKey": "google/gemma-4-12b-qat"},
                    {"modelKey": "fastcontext-1.0-4b-rl"},
                ]
            )
        elif command[1:] == ["ps", "--json"]:
            stdout = json.dumps(
                [
                    {
                        "modelKey": "fastcontext-1.0-4b-rl",
                        "identifier": "fastcontext-1.0-4b-rl",
                        "contextLength": 65536,
                        "status": "idle",
                        "parallel": 1,
                    }
                ]
            )
        else:
            raise AssertionError(command)
        return type("Completed", (), {"stdout": stdout, "stderr": ""})()

    monkeypatch.setattr(lmstudio.subprocess, "run", fake_run)
    inventory = lmstudio.model_inventory()

    configured = inventory["configured_models"]
    assert configured["gemma"]["downloaded"] is True
    assert configured["gemma"]["loaded"] is False
    assert configured["fastcontext"]["downloaded"] is True
    assert configured["fastcontext"]["loaded"] is True
    assert configured["fastcontext"]["loaded_detail"]["contextLength"] == 65536


def test_load_fastcontext_model_uses_expected_lms_flags(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> object:
        calls.append(command)
        assert kwargs["check"] is True
        return type("Completed", (), {"stdout": "loaded", "stderr": ""})()

    monkeypatch.setattr(lmstudio.subprocess, "run", fake_run)

    result = lmstudio.load_fastcontext_model()

    command = calls[0]
    assert command[1:] == [
        "load",
        lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        "--context-length",
        "65536",
        "--gpu",
        "max",
        "--identifier",
        lmstudio.DEFAULT_FASTCONTEXT_MODEL,
    ]
    assert result["model_id"] == lmstudio.DEFAULT_FASTCONTEXT_MODEL
    assert result["context_length"] == 65536
    assert result["gpu"] == "max"


@pytest.mark.asyncio
async def test_chat_json_posts_chat_completion_and_parses_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "gemma"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '```json\n{"ok": true}\n```'}},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    result = await lmstudio.chat_json(
        "gemma",
        [{"role": "user", "content": "return json"}],
        transport=transport,
    )
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_chat_completion_posts_tools_and_parses_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["tools"][0]["function"]["name"] == "Read"
        assert payload["tool_choice"] == "auto"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "Read",
                                        "arguments": '{"path":"src/app.py","offset":3,"limit":20}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    result = await lmstudio.chat_completion(
        "fastcontext",
        [{"role": "user", "content": "find code"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "Read", "parameters": {"type": "object"}},
            }
        ],
        tool_choice="auto",
        transport=transport,
    )

    assert result.content == ""
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "Read"
    assert result.tool_calls[0].arguments == {"path": "src/app.py", "offset": 3, "limit": 20}


@pytest.mark.asyncio
async def test_chat_completion_keeps_malformed_tool_arguments_nonfatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "Grep", "arguments": "{bad"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    result = await lmstudio.chat_completion(
        "fastcontext",
        [{"role": "user", "content": "find code"}],
        transport=httpx.MockTransport(handler),
    )

    assert result.tool_calls[0].arguments == {}
    assert "Invalid tool arguments JSON" in str(result.tool_calls[0].arguments_error)


@pytest.mark.asyncio
async def test_chat_text_still_requires_text_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "", "tool_calls": []}}]},
        )

    with pytest.raises(lmstudio.LMStudioError, match="empty chat completion"):
        await lmstudio.chat_text(
            "fastcontext",
            [{"role": "user", "content": "find code"}],
            transport=httpx.MockTransport(handler),
        )


def test_parse_json_content_handles_embedded_json() -> None:
    assert lmstudio.parse_json_content('Here is JSON: {"ok": true}') == {"ok": True}


def _write_card_fixture(root: Path) -> None:
    (root / "app").mkdir()
    (root / "app" / "page.tsx").write_text("export default function Page() { return <main /> }")
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15", "react": "19"}}),
        encoding="utf-8",
    )


def _create_repository_card(tmp_path: Path) -> str:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    _write_card_fixture(snapshot_root)
    repo_id = catalog.upsert_repository(
        {
            "owner": {"login": "owner"},
            "name": "repo",
            "full_name": "owner/repo",
            "html_url": "https://github.com/owner/repo",
            "private": False,
            "archived": False,
            "language": "TypeScript",
            "topics": ["nextjs"],
        },
        "test",
    )
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    return catalog.upsert_repository_card(snapshot_id, pipeline.build_repository_card(snapshot_root))


@pytest.mark.asyncio
async def test_profile_repository_cards_stores_gemma_profile(tmp_path, monkeypatch) -> None:
    card_id = _create_repository_card(tmp_path)

    async def fake_validate_models(config: lmstudio.LMStudioConfig) -> dict[str, Any]:
        return {
            "models": [config.gemma_model],
            "gemma_available": True,
            "fastcontext_available": False,
        }

    async def fake_chat_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "repository_type": "reference_application",
            "capabilities": [{"name": "dashboard", "confidence": 0.8, "evidence": ["app/page.tsx"]}],
            "likely_usefulness": 0.7,
            "extractability": 0.6,
            "maintenance_quality": 0.5,
            "needs_fastcontext": True,
            "concerns": [],
        }

    monkeypatch.setattr(profiler.lmstudio, "validate_models", fake_validate_models)
    monkeypatch.setattr(profiler.lmstudio, "chat_json", fake_chat_json)

    result = await profiler.profile_repository_cards(limit=5)
    assert result == {"profiled_cards": 1, "failed_cards": 0, "available_cards": 1}

    row = catalog.get_connection().execute(
        "SELECT gemma_profile FROM repository_cards WHERE card_id = ?",
        [card_id],
    ).fetchone()
    assert row is not None
    stored = json.loads(row[0])
    assert stored["schema_version"] == profiler.PROFILE_SCHEMA_VERSION
    assert stored["repository_type"] == "reference_application"

    runs = catalog.get_connection().execute(
        "SELECT stage_name, status, model_id FROM analysis_runs WHERE stage_name = 'profile'"
    ).fetchall()
    assert runs == [("profile", "completed", lmstudio.DEFAULT_GEMMA_MODEL)]


def test_lmstudio_status_cli_prints_json(monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    async def fake_status(start_server: bool, smoke_test: bool) -> dict[str, object]:
        assert start_server is True
        assert smoke_test is True
        return {"reachable": True}

    monkeypatch.setattr(main_module, "_lmstudio_status", fake_status)
    monkeypatch.setattr(sys, "argv", ["source-scout", "lmstudio-status", "--start-server", "--smoke-test"])
    main_module.main()
    captured = capsys.readouterr()
    assert '"reachable": true' in captured.out


@pytest.mark.asyncio
async def test_lmstudio_status_reports_api_downloaded_and_loaded(monkeypatch) -> None:
    import source_scout.__main__ as main_module

    async def fake_validate_models(config: lmstudio.LMStudioConfig) -> dict[str, object]:
        return {
            "base_url": config.base_url,
            "models": [config.gemma_model],
            "gemma_model": config.gemma_model,
            "fastcontext_model": config.fastcontext_model,
            "gemma_available": True,
            "fastcontext_available": False,
        }

    def fake_model_inventory(config: lmstudio.LMStudioConfig) -> dict[str, object]:
        return {
            "downloaded_models": [config.gemma_model, config.fastcontext_model],
            "loaded_models": [config.fastcontext_model],
            "configured_models": {
                "gemma": {
                    "model_id": config.gemma_model,
                    "downloaded": True,
                    "loaded": False,
                    "loaded_detail": None,
                },
                "fastcontext": {
                    "model_id": config.fastcontext_model,
                    "downloaded": True,
                    "loaded": True,
                    "loaded_detail": {"contextLength": 65536},
                },
            },
        }

    monkeypatch.setattr(lmstudio, "validate_models", fake_validate_models)
    monkeypatch.setattr(lmstudio, "model_inventory", fake_model_inventory)

    result = await main_module._lmstudio_status(start_server=False, smoke_test=False)

    configured = result["configured_models"]
    assert configured["gemma"]["downloaded"] is True
    assert configured["gemma"]["loaded"] is False
    assert configured["gemma"]["api_listed"] is True
    assert configured["fastcontext"]["downloaded"] is True
    assert configured["fastcontext"]["loaded"] is True
    assert configured["fastcontext"]["api_listed"] is False


def test_profile_cli_invokes_profiler(monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    async def fake_profile(limit: int, force: bool = False) -> dict[str, int]:
        assert limit == 2
        assert force is True
        return {"profiled_cards": 2}

    monkeypatch.setattr(profiler, "profile_repository_cards", fake_profile)
    monkeypatch.setattr(sys, "argv", ["source-scout", "profile", "--limit", "2", "--force"])
    main_module.main()
    captured = capsys.readouterr()
    assert "{'profiled_cards': 2}" in captured.out
