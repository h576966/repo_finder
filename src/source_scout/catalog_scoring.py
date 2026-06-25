from pathlib import Path
from typing import Any

from .capabilities import (
    AI_DATA_CAPABILITIES,
    BACKEND_CAPABILITIES,
    BACKEND_PATH_PARTS,
    BACKGROUND_JOB_FALSE_POSITIVE_TERMS,
    BACKGROUND_JOB_STRONG_TERMS,
    CAPABILITY_INTENT_HINTS,
    CAPABILITY_PATH_TERMS,
)


def _task_terms(task: str) -> set[str]:
    normalized = task.lower().replace("-", " ").replace("_", " ")
    return {term for term in normalized.split() if len(term) > 2}


def _capability_terms(capability: str) -> set[str]:
    terms = set(capability.lower().replace("-", " ").split())
    if capability == "data-table":
        terms.update({"datatable", "tanstack", "columns", "grid"})
    if capability == "command-palette":
        terms.update({"cmdk", "command", "palette"})
    if capability == "trpc-router":
        terms.update({"inittrpc", "procedure", "protectedprocedure", "publicprocedure", "trpc"})
    if capability == "data-access":
        terms.update({"drizzle", "prisma", "database", "schema"})
    if capability == "auth-middleware":
        terms.update({"auth", "session", "middleware"})
    if capability == "server-actions":
        terms.update({"actions", "revalidatepath", "server"})
    if capability == "file-storage":
        terms.update({"blob", "drive", "multipart", "r2", "s3", "storage", "upload"})
    for hint in CAPABILITY_INTENT_HINTS.get(capability, set()):
        terms.add(hint.lower())
        terms.update(hint.lower().replace("-", " ").split())
    return {term for term in terms if len(term) > 2}


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _profile_match_score(
    profile: dict[str, Any] | None,
    task_terms: set[str],
    capability: str,
) -> float:
    if not profile:
        return 0.0

    capability_terms = _capability_terms(capability)
    wanted_terms = task_terms | capability_terms
    best_capability = 0.0
    capabilities = profile.get("capabilities", [])
    if isinstance(capabilities, list):
        for item in capabilities:
            if not isinstance(item, dict):
                continue
            evidence = item.get("evidence", [])
            evidence_text = " ".join(str(value) for value in evidence) if isinstance(evidence, list) else ""
            searchable = f"{item.get('name', '')} {evidence_text}".lower().replace("-", " ")
            capability_overlap = sum(1 for term in capability_terms if term in searchable)
            task_overlap = sum(1 for term in wanted_terms if term in searchable)
            if task_overlap <= 0:
                continue
            confidence = _float_value(item.get("confidence"))
            if capability_overlap <= 0:
                candidate_score = confidence * min(0.18, task_overlap * 0.05)
            else:
                candidate_score = confidence * (0.45 + (capability_overlap * 0.18) + (task_overlap * 0.04))
            best_capability = max(best_capability, min(1.0, candidate_score))

    quality = (
        _float_value(profile.get("likely_usefulness"))
        + _float_value(profile.get("extractability"))
        + _float_value(profile.get("maintenance_quality"))
    ) / 3
    concerns = " ".join(str(value).lower() for value in profile.get("concerns", []))
    concern_penalty = 0.08 if any(term in concerns for term in ("coupled", "low quality", "unclear")) else 0.0
    quality_weight = 0.22 if best_capability >= 0.25 else 0.1
    combined = (best_capability * (1 - quality_weight)) + (quality * quality_weight) - concern_penalty
    return round(
        max(0.0, min(1.0, combined)),
        4,
    )


def _synthesis_score(synthesis: dict[str, Any], key: str) -> float:
    return max(0.0, min(1.0, _float_value(synthesis.get(key))))


def _has_backend_path(paths: list[Any]) -> bool:
    for raw_path in paths:
        path = str(raw_path).replace("\\", "/").lower()
        parts = set(path.split("/"))
        if parts & BACKEND_PATH_PARTS:
            return True
        if _path_tokens(path) & BACKEND_PATH_PARTS:
            return True
        if path.startswith(("lib/", "src/lib/", "app/api/", "src/app/api/", "worker/", "src/worker/")):
            return True
    return False


def _path_tokens(path: str) -> set[str]:
    tokens: set[str] = set()
    for part in path.replace("\\", "/").lower().split("/"):
        stem = Path(part).stem
        tokens.add(part)
        tokens.add(stem)
        tokens.update(token for token in stem.replace("_", "-").split("-") if token)
    return tokens


def _all_path_tokens(paths: list[Any]) -> set[str]:
    tokens: set[str] = set()
    for raw_path in paths:
        tokens.update(_path_tokens(str(raw_path)))
    return tokens


def _paths_contain_any(paths: list[Any], terms: set[str]) -> bool:
    joined = " ".join(str(path).replace("\\", "/").lower() for path in paths)
    return any(term in joined for term in terms)


def _backend_path_alignment_score(capability: str, paths: list[Any]) -> float:
    if capability not in BACKEND_CAPABILITIES and capability not in AI_DATA_CAPABILITIES:
        return 0.0

    if capability == "background-jobs":
        return _background_job_path_alignment_score(paths)

    wanted_terms = CAPABILITY_PATH_TERMS.get(capability, set())
    if not wanted_terms:
        return 0.0
    hits = len(_all_path_tokens(paths) & wanted_terms)
    if hits <= 0:
        return -0.18
    return min(0.16, hits * 0.04)


def _background_job_path_alignment_score(paths: list[Any]) -> float:
    strong_hits = 0
    false_positive_only = False
    for raw_path in paths:
        path = str(raw_path).replace("\\", "/").lower()
        is_false_positive = any(term in path for term in BACKGROUND_JOB_FALSE_POSITIVE_TERMS)
        tokens = _path_tokens(path)
        if tokens & BACKGROUND_JOB_STRONG_TERMS and not is_false_positive:
            strong_hits += 1
        elif is_false_positive:
            false_positive_only = True

    if strong_hits <= 0:
        return -0.36 if false_positive_only else -0.24
    return min(0.18, strong_hits * 0.04)


def _capability_intent_scores(task: str) -> dict[str, float]:
    lowered = task.lower().replace("-", " ").replace("_", " ")
    scores: dict[str, float] = {}
    for capability, hints in CAPABILITY_INTENT_HINTS.items():
        score = 0.0
        for hint in hints:
            normalized_hint = hint.lower().replace("-", " ").replace("_", " ")
            if normalized_hint in lowered:
                score += 0.35 if " " in hint else 0.18
        scores[capability] = min(1.0, score)
    return scores
