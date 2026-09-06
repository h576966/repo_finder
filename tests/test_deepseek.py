import json
from typing import Any

import httpx
import pytest

from source_scout import deepseek, failures


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

    result = await deepseek.response_completion(
        messages=[{"role": "user", "content": "Return JSON."}],
        response_format=schema,
        tool_choice="none",
        transport=httpx.MockTransport(handler),
    )
    assert json.loads(result.content) == {"ok": True}


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
        await deepseek.response_completion(
            messages=[{"role": "user", "content": "Return JSON."}],
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
        await deepseek.response_completion(
            messages=[{"role": "user", "content": "Return JSON."}],
            transport=httpx.MockTransport(handler),
        )
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_transient_response_error_has_zero_sdk_retries(status_code: int) -> None:
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
        await deepseek.response_completion(
            messages=[{"role": "user", "content": "Return JSON."}],
            transport=httpx.MockTransport(handler),
        )
    assert calls == 1


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
async def test_timeout_failure_has_a_distinct_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider was slow", request=request)

    with pytest.raises(deepseek.ModelTimeoutError, match="timed out"):
        await deepseek.list_models(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_http_failure_does_not_expose_provider_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "secret-provider-detail"}})

    with pytest.raises(deepseek.ModelStatusError) as exc_info:
        await deepseek.list_models(transport=httpx.MockTransport(handler))

    assert "secret-provider-detail" not in str(exc_info.value)
    failure = failures.failure_from_exception(exc_info.value, stage="model_status")
    assert failure.to_dict() == {
        "schema_version": failures.ERROR_SCHEMA_VERSION,
        "error_type": "authentication",
        "stage": "model_status",
        "message": "DeepSeek model listing failed with HTTP 401.",
        "retryable": False,
        "status_code": 401,
    }
