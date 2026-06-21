from typing import Any

from . import catalog, lmstudio

PROMPT_VERSION = "gemma-repo-card-v1"
PROFILE_SCHEMA_VERSION = "gemma-profile-v1"
ALLOWED_REPOSITORY_TYPES = {
    "library",
    "design_system",
    "reference_application",
    "starter",
    "tooling",
    "examples",
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
                "You profile public GitHub repositories for reusable Next.js, React, "
                "TypeScript UI code. Return only valid JSON. Be conservative and do "
                "not claim capabilities without structural evidence."
            ),
        },
        {
            "role": "user",
            "content": (
                "Profile this repository card for reuse.\n\n"
                f"RepositoryCard:\n{compact_card}\n\n"
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


def validate_gemma_profile(profile: dict[str, Any]) -> dict[str, Any]:
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

    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "repository_type": repository_type,
        "capabilities": normalized_capabilities,
        "likely_usefulness": _clamp_float(profile.get("likely_usefulness")),
        "extractability": _clamp_float(profile.get("extractability")),
        "maintenance_quality": _clamp_float(profile.get("maintenance_quality")),
        "needs_fastcontext": bool(profile.get("needs_fastcontext", True)),
        "concerns": [str(value) for value in concerns],
    }


def _clamp_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return round(max(0.0, min(parsed, 1.0)), 4)


async def profile_repository_cards(limit: int, force: bool = False) -> dict[str, int]:
    config = lmstudio.get_config()
    await _ensure_gemma_available(config)
    cards = catalog.list_repository_cards_for_profile(limit, force=force)
    profiled = 0
    failed = 0

    for card in cards:
        try:
            raw_profile = await lmstudio.chat_json(
                model_id=config.gemma_model,
                messages=_profile_messages(card),
                config=config,
            )
            profile = validate_gemma_profile(raw_profile)
            catalog.update_repository_card_gemma_profile(str(card["card_id"]), profile)
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
                model_id=config.gemma_model,
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
                model_id=config.gemma_model,
                prompt_version=PROMPT_VERSION,
            )
            failed += 1

    return {"profiled_cards": profiled, "failed_cards": failed, "available_cards": len(cards)}


async def _ensure_gemma_available(config: lmstudio.LMStudioConfig) -> None:
    status = await lmstudio.validate_models(config)
    if not status["gemma_available"]:
        raise lmstudio.LMStudioError(
            f"Configured Gemma model '{config.gemma_model}' is not available in LM Studio."
        )
