from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .deepseek import ModelConfig


async def _api_status(smoke_test: bool) -> dict[str, object]:
    from . import deepseek, fastcontext

    config = deepseek.get_config()
    try:
        status = await deepseek.validate_model(config)
    except deepseek.ModelConfigurationError as exc:
        return _error_status(config, exc, reachable=None, configured=False, error_type="configuration")
    except deepseek.ModelConnectionError as exc:
        return _error_status(config, exc, reachable=False, configured=True, error_type="connection")
    except deepseek.ModelStatusError as exc:
        error_type = _http_error_type(exc.status_code)
        error_result = _error_status(config, exc, reachable=True, configured=True, error_type=error_type)
        error_result["status_code"] = exc.status_code
        if exc.status_code in {401, 403}:
            error_result["authorized"] = False
        return error_result
    except deepseek.ModelError as exc:
        return _error_status(config, exc, reachable=None, configured=True, error_type="api")

    result: dict[str, object] = {"reachable": True, "configured": True, "authorized": True, **status}
    if not smoke_test:
        return result

    smoke_results: dict[str, object] = {}
    try:
        response = await deepseek.response_json(
            messages=[
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": 'Return exactly {"ok": true} as JSON.'},
            ],
            config=config,
            max_tokens=100,
            temperature=0.0,
        )
        smoke_results["assessment"] = {"completed": True, "response": response}
    except deepseek.ModelError as exc:
        smoke_results["assessment"] = {"completed": False, "error": str(exc)}
    try:
        response = await fastcontext.smoke_test(config)
        smoke_results["exploration"] = {"completed": True, "response": response}
    except (fastcontext.FastContextError, deepseek.ModelError) as exc:
        smoke_results["exploration"] = {"completed": False, "error": str(exc)}
    result["smoke_tests"] = smoke_results
    return result


def _error_status(
    config: ModelConfig,
    exc: Exception,
    *,
    reachable: bool | None,
    configured: bool,
    error_type: str,
) -> dict[str, object]:
    return {
        "reachable": reachable,
        "configured": configured,
        "base_url": config.base_url,
        "model_id": config.model_id,
        "error_type": error_type,
        "error": str(exc),
    }


def _http_error_type(status_code: int) -> str:
    if status_code in {401, 403}:
        return "authentication"
    if status_code == 402:
        return "billing"
    if status_code == 429:
        return "rate_limit"
    if status_code >= 500:
        return "service"
    return "http"
