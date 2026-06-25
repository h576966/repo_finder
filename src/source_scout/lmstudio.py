import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_GEMMA_MODEL = "google/gemma-4-12b-qat"
DEFAULT_FASTCONTEXT_MODEL = "fastcontext-1.0-4b-rl"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_GEMMA_CONTEXT_LENGTH = 32_768
DEFAULT_GEMMA_GPU = "max"
DEFAULT_FASTCONTEXT_CONTEXT_LENGTH = 65_536
DEFAULT_FASTCONTEXT_GPU = "max"
LMS_EXE = r"C:\Users\Nikla\.lmstudio\bin\lms.exe"


class LMStudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class LMStudioConfig:
    base_url: str = DEFAULT_BASE_URL
    gemma_model: str = DEFAULT_GEMMA_MODEL
    fastcontext_model: str = DEFAULT_FASTCONTEXT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class LMStudioToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any]
    arguments_error: str | None = None


@dataclass(frozen=True)
class LMStudioChatCompletion:
    content: str
    tool_calls: list[LMStudioToolCall]
    finish_reason: str | None
    message: dict[str, Any]
    raw: dict[str, Any]


def get_config() -> LMStudioConfig:
    return LMStudioConfig(
        base_url=os.environ.get("LM_STUDIO_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        gemma_model=os.environ.get("SOURCE_SCOUT_GEMMA_MODEL", DEFAULT_GEMMA_MODEL),
        fastcontext_model=os.environ.get("SOURCE_SCOUT_FASTCONTEXT_MODEL", DEFAULT_FASTCONTEXT_MODEL),
        timeout_seconds=_get_timeout(),
    )


def _get_timeout() -> float:
    raw = os.environ.get("SOURCE_SCOUT_LMSTUDIO_TIMEOUT")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def list_downloaded_models() -> list[dict[str, Any]]:
    return _run_lms_json(["ls", "--json"])


def list_loaded_models() -> list[dict[str, Any]]:
    return _run_lms_json(["ps", "--json"])


def load_fastcontext_model(
    config: LMStudioConfig | None = None,
    context_length: int = DEFAULT_FASTCONTEXT_CONTEXT_LENGTH,
    gpu: str = DEFAULT_FASTCONTEXT_GPU,
) -> dict[str, Any]:
    active = config or get_config()
    state = model_inventory(active)["configured_models"].get("fastcontext", {})
    if state.get("loaded"):
        _run_lms(["unload", active.fastcontext_model], {"model_id": active.fastcontext_model})
    return _run_lms(
        [
            "load",
            active.fastcontext_model,
            "--context-length",
            str(context_length),
            "--gpu",
            gpu,
            "--identifier",
            active.fastcontext_model,
        ],
        {
            "model_id": active.fastcontext_model,
            "context_length": context_length,
            "gpu": gpu,
        },
    )


def load_gemma_model(
    config: LMStudioConfig | None = None,
    context_length: int = DEFAULT_GEMMA_CONTEXT_LENGTH,
    gpu: str = DEFAULT_GEMMA_GPU,
) -> dict[str, Any]:
    active = config or get_config()
    state = model_inventory(active)["configured_models"].get("gemma", {})
    if state.get("loaded"):
        _run_lms(["unload", active.gemma_model], {"model_id": active.gemma_model})
    return _run_lms(
        [
            "load",
            active.gemma_model,
            "--context-length",
            str(context_length),
            "--gpu",
            gpu,
            "--identifier",
            active.gemma_model,
        ],
        {
            "model_id": active.gemma_model,
            "context_length": context_length,
            "gpu": gpu,
        },
    )


def model_inventory(config: LMStudioConfig | None = None) -> dict[str, Any]:
    active = config or get_config()
    downloaded_models = list_downloaded_models()
    loaded_models = list_loaded_models()
    downloaded_ids = _model_id_set(downloaded_models)
    loaded_ids = _model_id_set(loaded_models)
    return {
        "downloaded_models": sorted(downloaded_ids),
        "loaded_models": sorted(loaded_ids),
        "configured_models": {
            "gemma": _model_state(active.gemma_model, downloaded_ids, loaded_ids, loaded_models),
            "fastcontext": _model_state(
                active.fastcontext_model,
                downloaded_ids,
                loaded_ids,
                loaded_models,
            ),
        },
    }


def _run_lms_json(args: list[str]) -> list[dict[str, Any]]:
    result = _run_lms(args, {})
    try:
        parsed = json.loads(str(result["stdout"] or "[]"))
    except json.JSONDecodeError as exc:
        raise LMStudioError(f"lms {' '.join(args)} returned invalid JSON.") from exc
    if not isinstance(parsed, list):
        raise LMStudioError(f"lms {' '.join(args)} returned an unexpected JSON shape.")
    return [item for item in parsed if isinstance(item, dict)]


def _run_lms(args: list[str], extra: dict[str, Any]) -> dict[str, Any]:
    command = [LMS_EXE, *args]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LMStudioError(f"Failed to run {' '.join(command)}.") from exc
    return {
        "command": " ".join(command),
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        **extra,
    }


def _model_id_set(models: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for model in models:
        for key in (
            "modelKey",
            "identifier",
            "indexedModelIdentifier",
            "path",
            "selectedVariant",
        ):
            value = model.get(key)
            if isinstance(value, str) and value:
                ids.add(value)
    return ids


def _model_state(
    model_id: str,
    downloaded_ids: set[str],
    loaded_ids: set[str],
    loaded_models: list[dict[str, Any]],
) -> dict[str, Any]:
    loaded_model = _find_model(model_id, loaded_models)
    detail = _loaded_model_detail(loaded_model) if loaded_model else None
    return {
        "model_id": model_id,
        "downloaded": model_id in downloaded_ids,
        "loaded": model_id in loaded_ids,
        "loaded_detail": detail,
    }


def _find_model(model_id: str, models: list[dict[str, Any]]) -> dict[str, Any] | None:
    for model in models:
        if model_id in _model_id_set([model]):
            return model
    return None


def _loaded_model_detail(model: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "modelKey",
        "identifier",
        "displayName",
        "path",
        "contextLength",
        "maxContextLength",
        "status",
        "parallel",
        "queued",
        "ttlMs",
        "architecture",
        "paramsString",
    ]
    return {key: model[key] for key in keys if key in model}


async def list_models(
    config: LMStudioConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    active = config or get_config()
    try:
        async with httpx.AsyncClient(timeout=active.timeout_seconds, transport=transport) as client:
            response = await client.get(f"{active.base_url}/models")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LMStudioError(
            f"LM Studio is unreachable at {active.base_url}. "
            "Start it with 'lms server start' or the LM Studio Local Server UI."
        ) from exc

    data = response.json()
    models = data.get("data", [])
    if not isinstance(models, list):
        raise LMStudioError("LM Studio returned an unexpected /v1/models response.")
    return [str(model["id"]) for model in models if isinstance(model, dict) and model.get("id")]


async def validate_models(
    config: LMStudioConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    active = config or get_config()
    models = await list_models(active, transport=transport)
    return {
        "base_url": active.base_url,
        "models": models,
        "gemma_model": active.gemma_model,
        "fastcontext_model": active.fastcontext_model,
        "gemma_available": active.gemma_model in models,
        "fastcontext_available": active.fastcontext_model in models,
    }


async def chat_json(
    model_id: str,
    messages: list[dict[str, Any]],
    config: LMStudioConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_tokens: int = 1600,
    temperature: float = 0.1,
    attempts: int = 2,
    response_format: dict[str, Any] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    last_error: LMStudioError | None = None
    for _attempt in range(max(1, attempts)):
        try:
            content = await chat_text(
                model_id=model_id,
                messages=messages,
                config=config,
                transport=transport,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                seed=seed,
            )
            return parse_json_content(content)
        except LMStudioError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise LMStudioError("LM Studio chat completion failed.")


async def chat_text(
    model_id: str,
    messages: list[dict[str, Any]],
    config: LMStudioConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_tokens: int = 1600,
    temperature: float = 0.1,
    response_format: dict[str, Any] | None = None,
    seed: int | None = None,
) -> str:
    completion = await chat_completion(
        model_id=model_id,
        messages=messages,
        config=config,
        transport=transport,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        seed=seed,
    )
    if not completion.content.strip():
        raise LMStudioError("LM Studio returned an empty chat completion.")
    return completion.content


async def chat_completion(
    model_id: str,
    messages: list[dict[str, Any]],
    config: LMStudioConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_tokens: int = 1600,
    temperature: float = 0.1,
    response_format: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    extra_body: dict[str, Any] | None = None,
    seed: int | None = None,
) -> LMStudioChatCompletion:
    active = config or get_config()
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if seed is not None:
        payload["seed"] = seed
    if extra_body is not None:
        payload.update(extra_body)

    try:
        async with httpx.AsyncClient(timeout=active.timeout_seconds, transport=transport) as client:
            response = await client.post(f"{active.base_url}/chat/completions", json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip()
        suffix = f" Response: {detail[:500]}" if detail else ""
        raise LMStudioError(
            f"LM Studio chat completion failed for model '{model_id}'.{suffix}"
        ) from exc
    except httpx.HTTPError as exc:
        raise LMStudioError(f"LM Studio chat completion failed for model '{model_id}'.") from exc

    data = response.json()
    return _extract_chat_completion(data)


def _extract_chat_completion(data: dict[str, Any]) -> LMStudioChatCompletion:
    choice = _first_choice(data)
    message = choice.get("message")
    if not isinstance(message, dict):
        raise LMStudioError("LM Studio returned an invalid chat completion message.")
    content = message.get("content")
    if content is None:
        content_text = ""
    elif isinstance(content, str):
        content_text = content
    else:
        raise LMStudioError("LM Studio returned non-text chat completion content.")
    return LMStudioChatCompletion(
        content=content_text,
        tool_calls=_extract_tool_calls(message),
        finish_reason=str(choice["finish_reason"]) if choice.get("finish_reason") is not None else None,
        message=message,
        raw=data,
    )


def _extract_message_content(data: dict[str, Any]) -> str:
    content = _extract_chat_completion(data).content
    if not content.strip():
        raise LMStudioError("LM Studio returned an empty chat completion.")
    return content


def _first_choice(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LMStudioError("LM Studio returned no chat completion choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise LMStudioError("LM Studio returned an invalid chat completion choice.")
    return first


def _extract_tool_calls(message: dict[str, Any]) -> list[LMStudioToolCall]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    calls: list[LMStudioToolCall] = []
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments, arguments_error = _parse_tool_arguments(function.get("arguments"))
        call_id = raw_call.get("id")
        calls.append(
            LMStudioToolCall(
                id=str(call_id or f"tool-call-{index + 1}"),
                name=name,
                arguments=arguments,
                raw=raw_call,
                arguments_error=arguments_error,
            )
        )
    return calls


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
        raise LMStudioError("Expected LM Studio to return a JSON object.")
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
    raise LMStudioError("Could not parse JSON object from LM Studio response.")


def start_server(
    config: LMStudioConfig | None = None,
    startup_timeout_seconds: float = 30.0,
) -> bool:
    active = config or get_config()
    command = _server_start_command(active)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise LMStudioError(f"Failed to start LM Studio server via {LMS_EXE}.") from exc

    deadline = time.monotonic() + startup_timeout_seconds
    while time.monotonic() < deadline:
        if _server_reachable(active):
            return True
        time.sleep(0.5)

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    raise LMStudioError(
        f"Failed to start LM Studio server via {LMS_EXE} within "
        f"{startup_timeout_seconds:.0f} seconds."
    )


def _server_start_command(config: LMStudioConfig) -> list[str]:
    parsed = urlparse(config.base_url)
    command = [LMS_EXE, "server", "start"]
    if parsed.port is not None:
        command.extend(["--port", str(parsed.port)])
    if parsed.hostname:
        command.extend(["--bind", parsed.hostname])
    return command


def _server_reachable(config: LMStudioConfig) -> bool:
    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.get(f"{config.base_url}/models")
            response.raise_for_status()
    except httpx.HTTPError:
        return False
    return True
