import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_GEMMA_MODEL = "google/gemma-4-12b-qat"
DEFAULT_FASTCONTEXT_MODEL = "fastcontext-1.0-4b-rl"
DEFAULT_TIMEOUT_SECONDS = 30.0
LMS_EXE = r"C:\Users\Nikla\.lmstudio\bin\lms.exe"


class LMStudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class LMStudioConfig:
    base_url: str = DEFAULT_BASE_URL
    gemma_model: str = DEFAULT_GEMMA_MODEL
    fastcontext_model: str = DEFAULT_FASTCONTEXT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def get_config() -> LMStudioConfig:
    return LMStudioConfig(
        base_url=os.environ.get("LM_STUDIO_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        gemma_model=os.environ.get("REPO_FINDER_GEMMA_MODEL", DEFAULT_GEMMA_MODEL),
        fastcontext_model=os.environ.get("REPO_FINDER_FASTCONTEXT_MODEL", DEFAULT_FASTCONTEXT_MODEL),
        timeout_seconds=_get_timeout(),
    )


def _get_timeout() -> float:
    raw = os.environ.get("REPO_FINDER_LMSTUDIO_TIMEOUT")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


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
    messages: list[dict[str, str]],
    config: LMStudioConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_tokens: int = 1600,
    temperature: float = 0.1,
    attempts: int = 2,
) -> dict[str, Any]:
    active = config or get_config()
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    last_error: LMStudioError | None = None
    for _attempt in range(max(1, attempts)):
        try:
            async with httpx.AsyncClient(timeout=active.timeout_seconds, transport=transport) as client:
                response = await client.post(f"{active.base_url}/chat/completions", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LMStudioError(f"LM Studio chat completion failed for model '{model_id}'.") from exc

        data = response.json()
        try:
            content = _extract_message_content(data)
            return parse_json_content(content)
        except LMStudioError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise LMStudioError("LM Studio chat completion failed.")


def _extract_message_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LMStudioError("LM Studio returned no chat completion choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise LMStudioError("LM Studio returned an invalid chat completion choice.")
    message = first.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise LMStudioError("LM Studio returned an empty chat completion.")
    return content


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


def start_server() -> bool:
    try:
        subprocess.run(
            [LMS_EXE, "server", "start"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        raise LMStudioError(f"Failed to start LM Studio server via {LMS_EXE}.") from exc
