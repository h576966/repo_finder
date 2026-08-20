from __future__ import annotations


async def _api_status(smoke_test: bool) -> dict[str, object]:
    from . import deepseek, fastcontext

    config = deepseek.get_config()
    try:
        status = await deepseek.validate_model(config)
    except deepseek.ModelError as exc:
        return {
            "reachable": False,
            "base_url": config.base_url,
            "model_id": config.model_id,
            "error": str(exc),
        }

    result: dict[str, object] = {"reachable": True, **status}
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
