from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .failures import FailureDetails, failure_from_exception

if TYPE_CHECKING:
    from .deepseek import ModelConfig


async def _api_status(smoke_test: bool) -> dict[str, object]:
    from .exploration_policy import remote_exploration_mode

    mode = remote_exploration_mode()
    if mode == "off":
        return {"status": "disabled", "healthy": False, "reachable": None,
                "policy_mode": mode,
                "reason": "Remote exploration is disabled by policy; no model requests were made."}
    from . import deepseek, fastcontext

    config = deepseek.get_config()
    try:
        status = await deepseek.validate_model(config)
    except deepseek.ModelError as exc:
        failure = failure_from_exception(exc, stage="model_status")
        return _error_status(config, failure)

    result: dict[str, object] = {
        "policy_mode": mode,
        "healthy": bool(status.get("model_available")),
        "reachable": True,
        "configured": True,
        "authorized": True,
        **status,
    }
    if not result["healthy"]:
        failure = FailureDetails(
            "configuration",
            "model_status",
            f"Configured DeepSeek model '{config.model_id}' is not available.",
            retryable=False,
        )
        result.update(
            {
                "error_type": failure.error_type,
                "error": failure.message,
                "failure": failure.to_dict(),
            }
        )
        return result
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
        failure = failure_from_exception(exc, stage="assessment_smoke")
        smoke_results["assessment"] = {
            "completed": False,
            "error": str(exc),
            "failure": failure.to_dict(),
        }
    try:
        response = await fastcontext.smoke_test(config)
        smoke_results["exploration"] = {"completed": True, "response": response}
    except (fastcontext.FastContextError, deepseek.ModelError) as exc:
        failure = failure_from_exception(exc, stage="exploration_smoke")
        smoke_results["exploration"] = {
            "completed": False,
            "error": str(exc),
            "failure": failure.to_dict(),
        }
    result["smoke_tests"] = smoke_results
    result["healthy"] = bool(result["healthy"]) and all(
        bool(smoke.get("completed")) for smoke in smoke_results.values() if isinstance(smoke, dict)
    )
    if not result["healthy"]:
        for smoke in smoke_results.values():
            if not isinstance(smoke, dict) or smoke.get("completed"):
                continue
            raw_failure = smoke.get("failure")
            if isinstance(raw_failure, dict):
                result["error_type"] = raw_failure.get("error_type", "model_response")
                result["error"] = raw_failure.get("message", "Model smoke test failed.")
                result["failure"] = raw_failure
            break
    return result


def _error_status(
    config: ModelConfig,
    failure: FailureDetails,
) -> dict[str, object]:
    result: dict[str, object] = {
        "healthy": False,
        "reachable": False if failure.error_type in {"connection", "timeout"} else None,
        "configured": failure.error_type != "configuration",
        "base_url": config.base_url,
        "model_id": config.model_id,
        "error_type": failure.error_type,
        "error": failure.message,
        "failure": failure.to_dict(),
    }
    if failure.status_code is not None:
        result["reachable"] = True
        result["status_code"] = failure.status_code
    if failure.error_type == "authentication":
        result["authorized"] = False
    return result


def status_is_healthy(status: dict[str, Any]) -> bool:
    return bool(status.get("healthy"))
