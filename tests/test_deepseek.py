import json
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from source_scout import catalog, cli_status, deepseek, fastcontext, pipeline, profiler


def _message_response(content: str, *, status: str = "completed") -> dict[str, Any]:
    return {
        "id": "resp-1",
        "object": "response",
        "created_at": 0,
        "model": deepseek.DEEPSEEK_MODEL,
        "output": [
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "status": status,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _tool_response(
    *,
    name: str = "Read",
    arguments: str = '{"path":"README.md","offset":1,"limit":1}',
) -> dict[str, Any]:
    return {
        "id": "resp-tool",
        "object": "response",
        "created_at": 0,
        "model": deepseek.DEEPSEEK_MODEL,
        "output": [
            {
                "id": "fc-1",
                "type": "function_call",
                "call_id": "call-1",
                "name": name,
                "arguments": arguments,
                "status": "completed",
            }
        ],
        "parallel_tool_calls": True,
        "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def test_config_is_fixed_to_deepseek_v4_flash(monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_SCOUT_MODEL_TIMEOUT", "9")
    config = deepseek.get_config()
    assert config.base_url == "https://api.deepseek.com"
    assert config.model_id == "deepseek-v4-flash"
    assert config.timeout_seconds == 9.0


def test_catalog_migrates_existing_profile_column() -> None:
    catalog.reset_connection()
    connection = duckdb.connect(str(catalog.catalog_db_path()))
    connection.execute("CREATE TABLE repository_cards (card_id TEXT PRIMARY KEY, gemma_profile TEXT)")
    connection.execute(
        "INSERT INTO repository_cards VALUES (?, ?)",
        ["card-1", '{"schema_version":"repository-profile-v2"}'],
    )
    connection.close()

    migrated = catalog.get_connection()
    columns = {
        str(row[0])
        for row in migrated.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'repository_cards'"
        ).fetchall()
    }
    assert "repository_profile" in columns
    assert "gemma_profile" not in columns
    assert migrated.execute(
        "SELECT repository_profile FROM repository_cards WHERE card_id = 'card-1'"
    ).fetchone() == ('{"schema_version":"repository-profile-v2"}',)


@pytest.mark.asyncio
async def test_model_listing_uses_deepseek_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/models"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": deepseek.DEEPSEEK_MODEL, "object": "model", "owned_by": "deepseek"}],
            },
        )

    assert await deepseek.list_models(transport=httpx.MockTransport(handler)) == [deepseek.DEEPSEEK_MODEL]


@pytest.mark.asyncio
async def test_response_json_uses_native_responses_json_schema() -> None:
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "result",
            "schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/responses"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["input"] == [{"role": "user", "content": "Return JSON."}]
        assert payload["reasoning"] == {"effort": "none"}
        assert payload["tool_choice"] == "none"
        assert payload["text"]["format"] == {
            "type": "json_schema",
            "name": "result",
            "schema": schema["json_schema"]["schema"],
        }
        return httpx.Response(200, json=_message_response('{"ok":true}'))

    result = await deepseek.response_json(
        messages=[{"role": "user", "content": "Return JSON."}],
        response_format=schema,
        attempts=1,
        transport=httpx.MockTransport(handler),
    )
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_response_json_retries_one_empty_response() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_message_response("" if calls == 1 else '{"ok":true}'))

    result = await deepseek.response_json(
        messages=[{"role": "user", "content": "Return JSON."}],
        attempts=2,
        transport=httpx.MockTransport(handler),
    )
    assert result == {"ok": True}
    assert calls == 2


@pytest.mark.asyncio
async def test_failed_response_reports_api_error_before_output_validation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-failed",
                "object": "response",
                "created_at": 0,
                "model": deepseek.DEEPSEEK_MODEL,
                "output": None,
                "status": "failed",
                "error": {"code": "server_error", "message": "generation failed"},
            },
        )

    with pytest.raises(deepseek.ModelResponseError, match="generation failed"):
        await deepseek.response_json(
            messages=[{"role": "user", "content": "Return JSON."}],
            attempts=1,
            transport=httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_native_function_call_is_parsed_with_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/responses"
        assert payload["tools"][0]["name"] == "Read"
        assert payload["tool_choice"] == "required"
        return httpx.Response(200, json=_tool_response())

    completion = await deepseek.response_completion(
        messages=[{"role": "user", "content": "Read the file."}],
        tools=[
            {
                "type": "function",
                "name": "Read",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        tool_choice="required",
        transport=httpx.MockTransport(handler),
    )
    assert completion.tool_calls[0].name == "Read"
    assert completion.tool_calls[0].arguments == {
        "path": "README.md",
        "offset": 1,
        "limit": 1,
    }
    assert completion.response_id == "resp-tool"
    assert completion.response_model == deepseek.DEEPSEEK_MODEL
    assert completion.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert completion.latency_ms is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 402])
async def test_non_retryable_response_error_is_not_retried(status_code: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, json={"error": {"message": "request rejected"}})

    with pytest.raises(deepseek.ModelError, match=rf"HTTP {status_code}"):
        await deepseek.response_json(
            messages=[{"role": "user", "content": "Return JSON."}],
            transport=httpx.MockTransport(handler),
        )
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_transient_response_error_uses_bounded_sdk_retries(status_code: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            headers={"retry-after-ms": "0"},
            json={"error": {"message": "temporary failure"}},
        )

    with pytest.raises(deepseek.ModelError, match=rf"HTTP {status_code}"):
        await deepseek.response_json(
            messages=[{"role": "user", "content": "Return JSON."}],
            transport=httpx.MockTransport(handler),
        )
    assert calls == 3


@pytest.mark.asyncio
async def test_missing_api_key_fails_before_request(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(deepseek.ModelConfigurationError, match="DEEPSEEK_API_KEY is required"):
        await deepseek.list_models(transport=httpx.MockTransport(lambda request: httpx.Response(500)))


@pytest.mark.asyncio
async def test_connection_failure_has_a_distinct_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sandbox blocked the connection", request=request)

    with pytest.raises(deepseek.ModelConnectionError, match="Could not connect"):
        await deepseek.list_models(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_fastcontext_smoke_validates_exact_read_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["tools"][0]["name"] == "Read"
        assert payload["text"]["format"]["type"] == "json_schema"
        return httpx.Response(200, json=_tool_response())

    transport = httpx.MockTransport(handler)
    result = await fastcontext.smoke_test(transport=transport)
    assert result["ok"] is True
    assert result["tool_call"]["args"] == {"path": "README.md", "offset": 1, "limit": 1}


@pytest.mark.asyncio
async def test_model_status_reports_assessment_and_exploration_smokes(monkeypatch) -> None:
    async def fake_validate(config: deepseek.ModelConfig) -> dict[str, object]:
        return {
            "base_url": config.base_url,
            "models": [config.model_id],
            "model_id": config.model_id,
            "model_available": True,
        }

    async def fake_json(**kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def fake_smoke(config: deepseek.ModelConfig) -> dict[str, bool]:
        return {"ok": True}

    monkeypatch.setattr(deepseek, "validate_model", fake_validate)
    monkeypatch.setattr(deepseek, "response_json", fake_json)
    monkeypatch.setattr(fastcontext, "smoke_test", fake_smoke)
    result = await cli_status._api_status(smoke_test=True)
    assert result["reachable"] is True
    assert result["configured"] is True
    assert result["authorized"] is True
    assert result["smoke_tests"] == {
        "assessment": {"completed": True, "response": {"ok": True}},
        "exploration": {"completed": True, "response": {"ok": True}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            deepseek.ModelConfigurationError("DEEPSEEK_API_KEY is required."),
            {"reachable": None, "configured": False, "error_type": "configuration"},
        ),
        (
            deepseek.ModelConnectionError("Could not connect to the DeepSeek API."),
            {"reachable": False, "configured": True, "error_type": "connection"},
        ),
        (
            deepseek.ModelStatusError("HTTP 401", status_code=401),
            {
                "reachable": True,
                "configured": True,
                "authorized": False,
                "error_type": "authentication",
                "status_code": 401,
            },
        ),
        (
            deepseek.ModelStatusError("HTTP 402", status_code=402),
            {
                "reachable": True,
                "configured": True,
                "error_type": "billing",
                "status_code": 402,
            },
        ),
        (
            deepseek.ModelStatusError("HTTP 429", status_code=429),
            {
                "reachable": True,
                "configured": True,
                "error_type": "rate_limit",
                "status_code": 429,
            },
        ),
        (
            deepseek.ModelStatusError("HTTP 503", status_code=503),
            {
                "reachable": True,
                "configured": True,
                "error_type": "service",
                "status_code": 503,
            },
        ),
    ],
)
async def test_model_status_classifies_failures(
    monkeypatch, error: deepseek.ModelError, expected: dict[str, object]
) -> None:
    async def fail_validate(config: deepseek.ModelConfig) -> dict[str, object]:
        raise error

    monkeypatch.setattr(deepseek, "validate_model", fail_validate)
    result = await cli_status._api_status(smoke_test=False)
    for key, value in expected.items():
        assert result[key] == value


def _create_repository_card(tmp_path: Path) -> str:
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "app").mkdir()
    (root / "app" / "page.tsx").write_text(
        "export default function Page() { return <main /> }",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15", "react": "19"}}),
        encoding="utf-8",
    )
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
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", root)
    return catalog.upsert_repository_card(snapshot_id, pipeline.build_repository_card(root))


@pytest.mark.asyncio
async def test_profiler_stores_repository_profile(tmp_path: Path, monkeypatch) -> None:
    card_id = _create_repository_card(tmp_path)

    async def fake_validate(config: deepseek.ModelConfig) -> dict[str, object]:
        return {"model_available": True}

    async def fake_response_json(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["response_format"] == profiler.PROFILE_RESPONSE_FORMAT
        return {
            "repository_type": "reference_application",
            "capabilities": [{"name": "dashboard", "confidence": 0.8, "evidence": ["app/page.tsx"]}],
            "likely_usefulness": 0.7,
            "extractability": 0.6,
            "maintenance_quality": 0.5,
            "needs_fastcontext": False,
            "concerns": [],
        }

    monkeypatch.setattr(deepseek, "validate_model", fake_validate)
    monkeypatch.setattr(deepseek, "response_json", fake_response_json)
    assert await profiler.profile_repository_cards(limit=5) == {
        "profiled_cards": 1,
        "failed_cards": 0,
        "available_cards": 1,
    }
    row = (
        catalog.get_connection()
        .execute(
            "SELECT repository_profile FROM repository_cards WHERE card_id = ?",
            [card_id],
        )
        .fetchone()
    )
    assert row is not None
    stored = json.loads(row[0])
    assert stored["schema_version"] == "repository-profile-v3"
    assert stored["repository_type"] == "reference_application"
