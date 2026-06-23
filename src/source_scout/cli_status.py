from __future__ import annotations

import asyncio


async def _lmstudio_status(
    start_server: bool,
    smoke_test: bool,
    load_gemma: bool = False,
    gemma_context_length: int = 32_768,
    gemma_gpu: str = "max",
) -> dict[str, object]:
    from . import lmstudio

    config = lmstudio.get_config()
    started = False
    try:
        status = await lmstudio.validate_models(config)
    except lmstudio.LMStudioError as exc:
        if not start_server:
            return {
                "reachable": False,
                "error": str(exc),
                "hint": "Run source-scout lmstudio-status --start-server",
                **_status_with_inventory(_offline_status(config), config),
            }
        try:
            lmstudio.start_server(config)
        except lmstudio.LMStudioError as start_exc:
            return {
                "reachable": False,
                "started_server": False,
                "error": str(exc),
                "start_error": str(start_exc),
                "hint": "Start LM Studio Local Server from the LM Studio UI, then rerun this command.",
                **_status_with_inventory(_offline_status(config), config),
            }
        started = True
        await asyncio.sleep(1)
        status = await lmstudio.validate_models(config)

    load_result: dict[str, object] | None = None
    inventory_status = _status_with_inventory(status, config)
    gemma_state = _configured_model_state(inventory_status, "gemma")
    if load_gemma and _should_load_model(gemma_state, gemma_context_length):
        try:
            load_result = lmstudio.load_gemma_model(
                config,
                context_length=gemma_context_length,
                gpu=gemma_gpu,
            )
            await asyncio.sleep(1)
            status = await lmstudio.validate_models(config)
            inventory_status = _status_with_inventory(status, config)
        except lmstudio.LMStudioError as exc:
            load_result = {
                "model_id": config.gemma_model,
                "context_length": gemma_context_length,
                "gpu": gemma_gpu,
                "loaded": False,
                "error": str(exc),
            }

    result: dict[str, object] = {
        "reachable": True,
        "started_server": started,
        "load_gemma_requested": load_gemma,
        **inventory_status,
    }
    if load_result is not None:
        result["load_gemma"] = load_result
    if smoke_test:
        try:
            smoke_result = await lmstudio.chat_json(
                model_id=config.gemma_model,
                messages=[
                    {"role": "system", "content": "Return only valid JSON."},
                    {"role": "user", "content": 'Return exactly {"ok": true}.'},
                ],
                config=config,
                max_tokens=100,
            )
            result["gemma_smoke_test"] = {"completed": True, "response": smoke_result}
        except lmstudio.LMStudioError as exc:
            result["gemma_smoke_test"] = {"completed": False, "error": str(exc)}
    return result


async def _fastcontext_status(
    start_server: bool,
    smoke_test: bool,
    load_model: bool = False,
    context_length: int = 65_536,
    gpu: str = "max",
) -> dict[str, object]:
    from . import fastcontext, lmstudio

    config = lmstudio.get_config()
    started = False
    try:
        status = await lmstudio.validate_models(config)
    except lmstudio.LMStudioError as exc:
        if not start_server:
            return {
                "reachable": False,
                "error": str(exc),
                "hint": "Run source-scout fastcontext-status --start-server",
                **_status_with_inventory(_offline_status(config), config),
            }
        try:
            lmstudio.start_server(config)
        except lmstudio.LMStudioError as start_exc:
            return {
                "reachable": False,
                "started_server": False,
                "error": str(exc),
                "start_error": str(start_exc),
                "hint": "Start LM Studio Local Server from the LM Studio UI, then rerun this command.",
                **_status_with_inventory(_offline_status(config), config),
            }
        started = True
        await asyncio.sleep(1)
        status = await lmstudio.validate_models(config)

    load_result: dict[str, object] | None = None
    inventory_status = _status_with_inventory(status, config)
    fastcontext_state = _configured_model_state(inventory_status, "fastcontext")
    if load_model and not bool(fastcontext_state.get("loaded")):
        try:
            load_result = lmstudio.load_fastcontext_model(
                config,
                context_length=context_length,
                gpu=gpu,
            )
            await asyncio.sleep(1)
            status = await lmstudio.validate_models(config)
            inventory_status = _status_with_inventory(status, config)
        except lmstudio.LMStudioError as exc:
            load_result = {
                "model_id": config.fastcontext_model,
                "context_length": context_length,
                "gpu": gpu,
                "loaded": False,
                "error": str(exc),
            }

    result: dict[str, object] = {
        "reachable": True,
        "started_server": started,
        "load_model_requested": load_model,
        **inventory_status,
    }
    if load_result is not None:
        result["load_model"] = load_result
    if smoke_test:
        try:
            smoke_result = await fastcontext.smoke_test(config)
            result["fastcontext_smoke_test"] = {"completed": True, "response": smoke_result}
        except (fastcontext.FastContextError, lmstudio.LMStudioError) as exc:
            result["fastcontext_smoke_test"] = {"completed": False, "error": str(exc)}
    return result


def _status_with_inventory(
    status: dict[str, object],
    config: object,
) -> dict[str, object]:
    from . import lmstudio

    result: dict[str, object] = dict(status)
    api_models = status.get("models")
    api_model_ids = set(api_models) if isinstance(api_models, list) else set()
    try:
        inventory = lmstudio.model_inventory(config if isinstance(config, lmstudio.LMStudioConfig) else None)
    except lmstudio.LMStudioError as exc:
        result["inventory_error"] = str(exc)
        inventory = {
            "downloaded_models": [],
            "loaded_models": [],
            "configured_models": {
                "gemma": {
                    "model_id": getattr(config, "gemma_model", lmstudio.DEFAULT_GEMMA_MODEL),
                    "downloaded": False,
                    "loaded": False,
                    "loaded_detail": None,
                },
                "fastcontext": {
                    "model_id": getattr(
                        config,
                        "fastcontext_model",
                        lmstudio.DEFAULT_FASTCONTEXT_MODEL,
                    ),
                    "downloaded": False,
                    "loaded": False,
                    "loaded_detail": None,
                },
            },
        }
    configured = inventory["configured_models"]
    if isinstance(configured, dict):
        for value in configured.values():
            if isinstance(value, dict):
                value["api_listed"] = value.get("model_id") in api_model_ids
    result.update(inventory)
    return result


def _offline_status(config: object) -> dict[str, object]:
    from . import lmstudio

    return {
        "base_url": getattr(config, "base_url", lmstudio.DEFAULT_BASE_URL),
        "models": [],
        "gemma_model": getattr(config, "gemma_model", lmstudio.DEFAULT_GEMMA_MODEL),
        "fastcontext_model": getattr(
            config,
            "fastcontext_model",
            lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        ),
        "gemma_available": False,
        "fastcontext_available": False,
    }


def _configured_model_state(status: dict[str, object], key: str) -> dict[str, object]:
    configured = status.get("configured_models")
    if not isinstance(configured, dict):
        return {}
    state = configured.get(key)
    return state if isinstance(state, dict) else {}


def _should_load_model(state: dict[str, object], desired_context_length: int) -> bool:
    if not bool(state.get("loaded")):
        return True
    detail = state.get("loaded_detail")
    if not isinstance(detail, dict):
        return True
    try:
        current_context = int(detail.get("contextLength", 0))
    except (TypeError, ValueError):
        return True
    return current_context < desired_context_length
