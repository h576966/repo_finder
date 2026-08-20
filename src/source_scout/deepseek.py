from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from openai import APIConnectionError, APIError, APIStatusError, AsyncOpenAI

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT_SECONDS = 120.0


class ModelError(RuntimeError):
    pass


class ModelConfigurationError(ModelError):
    pass


class ModelConnectionError(ModelError):
    pass


class ModelStatusError(ModelError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ModelResponseError(ModelError):
    pass


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = DEEPSEEK_BASE_URL
    model_id: str = DEEPSEEK_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any]
    arguments_error: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    content: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    raw: dict[str, Any]
    output_items: list[dict[str, Any]] = field(default_factory=list)
    response_id: str | None = None
    response_model: str | None = None
    usage: dict[str, Any] | None = None
    latency_ms: int | None = None


def get_config() -> ModelConfig:
    raw_timeout = os.environ.get("SOURCE_SCOUT_MODEL_TIMEOUT")
    if raw_timeout:
        try:
            timeout = max(1.0, float(raw_timeout))
        except ValueError:
            timeout = DEFAULT_TIMEOUT_SECONDS
    else:
        timeout = DEFAULT_TIMEOUT_SECONDS
    return ModelConfig(timeout_seconds=timeout)


def _api_key() -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ModelConfigurationError("DEEPSEEK_API_KEY is required.")
    return api_key


class DeepSeekClient:
    def __init__(
        self,
        config: ModelConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config or get_config()
        self._transport = transport
        self._http_client: httpx.AsyncClient | None = None
        self._client: AsyncOpenAI | None = None

    async def __aenter__(self) -> DeepSeekClient:
        api_key = _api_key()
        self._http_client = httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            transport=self._transport,
        )
        self._client = AsyncOpenAI(
            base_url=self.config.base_url,
            api_key=api_key,
            timeout=self.config.timeout_seconds,
            max_retries=2,
            http_client=self._http_client,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None
        self._http_client = None

    def _active_client(self) -> AsyncOpenAI:
        if self._client is None:
            raise RuntimeError("DeepSeekClient must be used as an async context manager.")
        return self._client

    async def list_models(self) -> list[str]:
        try:
            response = await self._active_client().models.list()
        except APIStatusError as exc:
            raise _status_error("model listing", exc) from exc
        except APIConnectionError as exc:
            raise ModelConnectionError("Could not connect to the DeepSeek API.") from exc
        except APIError as exc:
            raise ModelError("DeepSeek model listing failed.") from exc
        return [str(model.id) for model in response.data if model.id]

    async def create_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        max_output_tokens: int,
        temperature: float,
        text_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.config.model_id,
            "input": input_items,
            "max_output_tokens": max_output_tokens,
            "reasoning": {"effort": "none"},
            "temperature": temperature,
        }
        if text_format is not None:
            payload["text"] = {"format": text_format}
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

        started = time.perf_counter()
        try:
            response = await self._active_client().responses.create(**payload)
        except APIStatusError as exc:
            raise _status_error("response", exc) from exc
        except APIConnectionError as exc:
            raise ModelConnectionError("Could not connect to the DeepSeek API.") from exc
        except APIError as exc:
            raise ModelError("DeepSeek response failed.") from exc
        latency_ms = round((time.perf_counter() - started) * 1000)
        return _extract_response(response.model_dump(mode="json"), latency_ms=latency_ms)


async def list_models(
    config: ModelConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    async with DeepSeekClient(config, transport) as client:
        return await client.list_models()


async def validate_model(
    config: ModelConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    active = config or get_config()
    models = await list_models(active, transport=transport)
    return {
        "base_url": active.base_url,
        "models": models,
        "model_id": active.model_id,
        "model_available": active.model_id in models,
    }


async def response_json(
    *,
    messages: list[dict[str, Any]],
    config: ModelConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_tokens: int = 1600,
    temperature: float = 0.0,
    attempts: int = 2,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text_format = _responses_text_format(response_format or {"type": "json_object"})
    last_error: ModelResponseError | None = None
    async with DeepSeekClient(config, transport) as client:
        for _attempt in range(max(1, attempts)):
            try:
                response = await client.create_response(
                    input_items=messages,
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                    text_format=text_format,
                    tool_choice="none",
                )
                if not response.content.strip():
                    raise ModelResponseError("DeepSeek returned an empty response.")
                return parse_json_content(response.content)
            except ModelResponseError as exc:
                last_error = exc
    if last_error is not None:
        raise last_error
    raise ModelError("DeepSeek response failed.")


async def response_text(
    *,
    messages: list[dict[str, Any]],
    config: ModelConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_tokens: int = 1600,
    temperature: float = 0.0,
    response_format: dict[str, Any] | None = None,
) -> str:
    async with DeepSeekClient(config, transport) as client:
        response = await client.create_response(
            input_items=messages,
            max_output_tokens=max_tokens,
            temperature=temperature,
            text_format=(_responses_text_format(response_format) if response_format is not None else None),
            tool_choice="none",
        )
    if not response.content.strip():
        raise ModelResponseError("DeepSeek returned an empty response.")
    return response.content


async def response_completion(
    *,
    messages: list[dict[str, Any]],
    config: ModelConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    client: DeepSeekClient | None = None,
    max_tokens: int = 1600,
    temperature: float = 0.0,
    response_format: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> ModelResponse:
    text_format = _responses_text_format(response_format) if response_format is not None else None
    if client is not None:
        return await client.create_response(
            input_items=messages,
            max_output_tokens=max_tokens,
            temperature=temperature,
            text_format=text_format,
            tools=tools,
            tool_choice=tool_choice,
        )
    async with DeepSeekClient(config, transport) as active_client:
        return await active_client.create_response(
            input_items=messages,
            max_output_tokens=max_tokens,
            temperature=temperature,
            text_format=text_format,
            tools=tools,
            tool_choice=tool_choice,
        )


def _status_error(operation: str, exc: APIStatusError) -> ModelStatusError:
    detail = exc.response.text.strip()
    suffix = f" Response: {detail[:500]}" if detail else ""
    return ModelStatusError(
        f"DeepSeek {operation} failed with HTTP {exc.status_code}.{suffix}",
        status_code=exc.status_code,
    )


def _responses_text_format(response_format: dict[str, Any]) -> dict[str, Any]:
    if response_format.get("type") != "json_schema":
        return dict(response_format)
    json_schema = response_format.get("json_schema")
    if not isinstance(json_schema, dict):
        raise ModelResponseError("DeepSeek JSON Schema output requires a json_schema object.")
    return {"type": "json_schema", **json_schema}


def _extract_response(data: dict[str, Any], *, latency_ms: int | None = None) -> ModelResponse:
    status = data.get("status")
    if status == "failed":
        error = data.get("error")
        raise ModelResponseError(f"DeepSeek response failed: {error}")
    output_items = _response_output_items(data)
    content_text = _response_output_text(output_items)
    tool_calls = _extract_response_tool_calls(output_items)
    raw_usage = data.get("usage")
    usage = (
        {str(key): value for key, value in raw_usage.items() if value is not None}
        if isinstance(raw_usage, dict)
        else None
    )
    return ModelResponse(
        content=content_text,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else _response_finish_reason(data),
        raw=data,
        output_items=output_items,
        response_id=str(data["id"]) if data.get("id") else None,
        response_model=str(data["model"]) if data.get("model") else None,
        usage=usage,
        latency_ms=latency_ms,
    )


def _response_output_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    output = data.get("output")
    if not isinstance(output, list):
        raise ModelResponseError("DeepSeek returned no response output items.")
    return [item for item in output if isinstance(item, dict)]


def _response_output_text(output_items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in output_items:
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if isinstance(content_item, dict) and content_item.get("type") == "output_text":
                text = content_item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def _extract_response_tool_calls(output_items: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, item in enumerate(output_items):
        if item.get("type") != "function_call":
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments, arguments_error = _parse_tool_arguments(item.get("arguments"))
        call_id = item.get("call_id") or item.get("id")
        calls.append(
            ToolCall(
                id=str(call_id or f"tool-call-{index + 1}"),
                name=name,
                arguments=arguments,
                raw=item,
                arguments_error=arguments_error,
            )
        )
    return calls


def _response_finish_reason(data: dict[str, Any]) -> str | None:
    incomplete_details = data.get("incomplete_details")
    if isinstance(incomplete_details, dict):
        reason = incomplete_details.get("reason")
        if isinstance(reason, str) and reason:
            return reason
    status = data.get("status")
    return status if isinstance(status, str) and status else None


def _parse_tool_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None]:
    if raw_arguments is None:
        return {}, None
    if isinstance(raw_arguments, dict):
        return raw_arguments, None
    if not isinstance(raw_arguments, str):
        return {}, f"Unexpected tool arguments type: {type(raw_arguments).__name__}"
    if not raw_arguments.strip():
        return {}, None
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        return {}, f"Invalid tool arguments JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return {}, "Tool arguments JSON must be an object."
    return parsed, None


def parse_json_content(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _parse_embedded_json(text)
    if not isinstance(parsed, dict):
        raise ModelResponseError("Expected DeepSeek to return a JSON object.")
    return parsed


def _parse_embedded_json(text: str) -> Any:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
            return parsed
        except json.JSONDecodeError:
            continue
    raise ModelResponseError("Could not parse a JSON object from the DeepSeek response.")
