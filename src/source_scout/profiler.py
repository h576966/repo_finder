import json
from typing import Any

from . import catalog, deepseek

PROMPT_VERSION = "repository-profiler-v4"
PROFILE_SCHEMA_VERSION = "repository-profile-v3"
ALLOWED_REPOSITORY_TYPES = {
    "library",
    "design_system",
    "reference_application",
    "starter",
    "tooling",
    "examples",
}
PROFILE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "repository_profile",
        "schema": {
            "type": "object",
            "properties": {
                "repository_type": {
                    "type": "string",
                    "enum": sorted(ALLOWED_REPOSITORY_TYPES),
                },
                "capabilities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "confidence": {"type": "number"},
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["name", "confidence", "evidence"],
                        "additionalProperties": True,
                    },
                },
                "likely_usefulness": {"type": "number"},
                "extractability": {"type": "number"},
                "maintenance_quality": {"type": "number"},
                "needs_fastcontext": {"type": "boolean"},
                "concerns": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "repository_type",
                "capabilities",
                "likely_usefulness",
                "extractability",
                "maintenance_quality",
                "needs_fastcontext",
                "concerns",
            ],
            "additionalProperties": True,
        },
    },
}


def _profile_messages(card: dict[str, Any]) -> list[dict[str, str]]:
    compact_card = {
        "repo_id": card["repo_id"],
        "commit_sha": card["commit_sha"],
        "html_url": card["html_url"],
        "card_version": card["card_version"],
        "package_manifests": card["package_manifests"],
        "tree_summary": card["tree_summary"],
        "readme_excerpt": card["readme_excerpt"],
        "stack_signals": card["stack_signals"],
        "deterministic_features": card["deterministic_features"],
    }
    return [
        {
            "role": "system",
            "content": (
                "Profile public GitHub repositories for reusable TypeScript, JavaScript, "
                "Python, AI, data, and web application source. Outcome: a compact reuse "
                "profile with conservative capability labels, 0-1 quality scores, and "
                "short evidence strings from paths, manifests, stack signals, or deterministic "
                "features. Use only the RepositoryCard JSON. Prefer structural evidence over "
                "README claims. Return only valid JSON matching the requested schema."
            ),
        },
        {
            "role": "user",
            "content": (
                "Profile this repository card for reuse. Keep concerns and evidence brief; "
                "set needs_fastcontext=true when structural signals are too thin to trust.\n\n"
                f"RepositoryCard JSON:\n{json.dumps(compact_card, sort_keys=True)}\n\n"
                "Return exactly this JSON object shape:\n"
                "{\n"
                '  "repository_type": '
                '"library|design_system|reference_application|starter|tooling|examples",\n'
                '  "capabilities": [\n'
                '    {"name": "string", "confidence": 0.0, "evidence": ["path-or-signal"]}\n'
                "  ],\n"
                '  "likely_usefulness": 0.0,\n'
                '  "extractability": 0.0,\n'
                '  "maintenance_quality": 0.0,\n'
                '  "needs_fastcontext": true,\n'
                '  "concerns": ["string"]\n'
                "}"
            ),
        },
    ]


def validate_repository_profile(profile: dict[str, Any]) -> dict[str, Any]:
    repository_type = str(profile.get("repository_type", "examples"))
    if repository_type not in ALLOWED_REPOSITORY_TYPES:
        repository_type = "examples"

    capabilities = profile.get("capabilities", [])
    if not isinstance(capabilities, list):
        capabilities = []
    normalized_capabilities: list[dict[str, Any]] = []
    for item in capabilities:
        if not isinstance(item, dict):
            continue
        evidence = item.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []
        normalized_capabilities.append(
            {
                "name": str(item.get("name", "")).strip(),
                "confidence": _clamp_float(item.get("confidence")),
                "evidence": [str(value) for value in evidence],
            }
        )

    concerns = profile.get("concerns", [])
    if not isinstance(concerns, list):
        concerns = []

    normalized = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "repository_type": repository_type,
        "capabilities": normalized_capabilities,
        "likely_usefulness": _clamp_float(profile.get("likely_usefulness")),
        "extractability": _clamp_float(profile.get("extractability")),
        "maintenance_quality": _clamp_float(profile.get("maintenance_quality")),
        "needs_fastcontext": bool(profile.get("needs_fastcontext", True)),
        "concerns": [str(value) for value in concerns],
    }
    if _is_uninformative_profile(normalized):
        raise ValueError(
            "Model profile was uninformative: all quality scores are zero with no capabilities or concerns."
        )
    return normalized


def _clamp_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return round(max(0.0, min(parsed, 1.0)), 4)


async def profile_repository_cards(
    limit: int,
    force: bool = False,
    *,
    priority: str = "created-at",
    scope: str = "downloaded",
) -> dict[str, int]:
    config = deepseek.get_config()
    await _ensure_model_available(config)
    if priority == "created-at":
        cards = catalog.list_repository_cards_for_profile(
            limit,
            force=force,
            profile_schema_version=PROFILE_SCHEMA_VERSION,
        )
    elif priority == "audit":
        from .catalog_audit import list_profile_cards_by_audit_priority

        cards = list_profile_cards_by_audit_priority(limit, scope=scope, force=force)
    else:
        raise ValueError("priority must be 'created-at' or 'audit'.")
    profiled = 0
    failed = 0

    for card in cards:
        try:
            raw_profile = await deepseek.response_json(
                messages=_profile_messages(card),
                config=config,
                max_tokens=3000,
                attempts=1,
                response_format=PROFILE_RESPONSE_FORMAT,
            )
            try:
                profile = validate_repository_profile(raw_profile)
            except Exception as exc:
                repair_response = await deepseek.response_json(
                    messages=_repair_messages(card, raw_profile, [f"{type(exc).__name__}: {exc}"]),
                    config=config,
                    max_tokens=3000,
                    attempts=1,
                    response_format=PROFILE_RESPONSE_FORMAT,
                )
                profile = validate_repository_profile(repair_response)
            catalog.update_repository_card_profile(str(card["card_id"]), profile)
            catalog.record_analysis_run(
                "profile",
                "completed",
                {
                    "card_id": card["card_id"],
                    "profile_schema_version": PROFILE_SCHEMA_VERSION,
                    "repository_type": profile["repository_type"],
                },
                repo_id=str(card["repo_id"]),
                snapshot_id=str(card["snapshot_id"]),
                model_id=config.model_id,
                prompt_version=PROMPT_VERSION,
            )
            profiled += 1
        except Exception as exc:
            catalog.record_analysis_run(
                "profile",
                "failed",
                {"card_id": card.get("card_id"), "error": str(exc)},
                repo_id=str(card.get("repo_id", "")),
                snapshot_id=str(card.get("snapshot_id", "")),
                model_id=config.model_id,
                prompt_version=PROMPT_VERSION,
            )
            failed += 1

    return {"profiled_cards": profiled, "failed_cards": failed, "available_cards": len(cards)}


async def _ensure_model_available(config: deepseek.ModelConfig) -> None:
    status = await deepseek.validate_model(config)
    if not status["model_available"]:
        raise deepseek.ModelError(f"Configured DeepSeek model '{config.model_id}' is not available.")


def _repair_messages(
    card: dict[str, Any],
    raw_response: dict[str, Any] | None,
    validation_errors: list[str],
) -> list[dict[str, str]]:
    return [
        *_profile_messages(card),
        {"role": "assistant", "content": json.dumps(raw_response or {}, sort_keys=True)},
        {
            "role": "user",
            "content": (
                "Repair the JSON once. Return only the same schema. Validation errors:\n"
                f"{json.dumps(validation_errors, sort_keys=True)}"
            ),
        },
    ]


def _is_uninformative_profile(profile: dict[str, Any]) -> bool:
    return (
        profile.get("likely_usefulness") == 0
        and profile.get("extractability") == 0
        and profile.get("maintenance_quality") == 0
        and not profile.get("capabilities")
        and not profile.get("concerns")
    )
