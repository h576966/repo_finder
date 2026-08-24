import math
import re
from collections import Counter
from typing import Any

from .capabilities import (
    AI_DATA_CAPABILITIES,
    BACKEND_CAPABILITIES,
    CAPABILITY_INTENT_HINTS,
    COMMAND_PALETTE_DEPENDENCIES,
    UI_CAPABILITIES,
)
from .catalog_assessments import task_signature
from .catalog_core import _cutoff_date, _json_load, get_connection
from .catalog_scoring import (
    MIN_LABEL_SIGNAL,
    POSSIBLE_LABEL_SCORE_CAP,
    STRONG_LABEL_SIGNAL,
    _backend_path_alignment_score,
    _capability_intent_scores,
    _capability_label_signal_score,
    _float_value,
    _has_backend_path,
    _has_profile_signal,
    _paths_contain_any,
    _profile_match_score,
    _synthesis_score,
    _task_terms,
)
from .constants import MAX_REPO_AGE_DAYS, MAX_REPOSITORY_SIZE_KB, MAX_STALE_DAYS
from .models import ReusableCandidate
from .target_profile import TargetProfileV1, npm_unambiguous_major

RETRIEVAL_THRESHOLD_VERSION = "retrieval-threshold-v1"
MIN_RETRIEVAL_SCORE_V1 = 0.35
MIN_TASK_RELEVANCE_SIGNAL_V1 = 0.50
BM25_ROLE_VERSION = "bm25-role-v1"
BM25_MAX_BOOST = 0.18
BM25_MIN_ROLE_OVERLAP = 2
_BM25_STOP_WORDS = {
    "and",
    "for",
    "from",
    "implementation",
    "implement",
    "into",
    "that",
    "the",
    "using",
    "with",
}
_CAPABILITY_ROLE_TERMS = {
    "command-palette": {
        "actions",
        "keyboard",
        "launcher",
        "navigation",
        "navigating",
        "overlay",
        "shortcuts",
    },
    "data-table": {
        "browser",
        "filters",
        "pagination",
        "records",
        "sorting",
        "tabular",
    },
}
_FRAMEWORK_DEPENDENCY_SIGNALS = {
    "@nestjs/core": "nestjs",
    "@sveltejs/kit": "sveltekit",
    "django": "django",
    "express": "express",
    "fastapi": "fastapi",
    "fastify": "fastify",
    "flask": "flask",
    "hono": "hono",
    "next": "nextjs",
    "nuxt": "nuxt",
    "react": "react",
    "starlette": "starlette",
    "svelte": "svelte",
    "vue": "vue",
}
_NPM_MAJOR_FRAMEWORK_DEPENDENCIES = {
    "@nestjs/core",
    "@sveltejs/kit",
    "express",
    "fastify",
    "hono",
    "next",
    "nuxt",
    "react",
    "svelte",
    "vue",
}


def _target_fit(
    target_profile: TargetProfileV1 | None,
    *,
    detected_languages: Any,
    package_manifests: Any,
    stack_signals: Any,
) -> tuple[float, list[str]]:
    if target_profile is None:
        return 0.0, []

    notes: list[str] = []
    score = 0.0
    candidate_languages = _candidate_languages(detected_languages, stack_signals)
    target_ecosystems = _language_ecosystems(set(target_profile.languages))
    candidate_ecosystems = _language_ecosystems(candidate_languages)
    if target_ecosystems and candidate_ecosystems:
        shared_ecosystems = sorted(target_ecosystems & candidate_ecosystems)
        if shared_ecosystems:
            score += 0.03
            notes.append(f"Shared language ecosystem: {', '.join(shared_ecosystems)}.")
        else:
            score -= 0.10
            notes.append("Candidate and target use different language ecosystems.")

    candidate_dependencies = _candidate_dependencies(package_manifests)
    target_dependencies = {
        item.name: item.constraint
        for item in (*target_profile.runtime_dependencies, *target_profile.dev_dependencies)
    }
    target_npm_dependencies = {
        item.name: item.constraint
        for item in (*target_profile.runtime_dependencies, *target_profile.dev_dependencies)
        if item.ecosystem == "npm"
    }
    dependency_overlap = sorted(set(candidate_dependencies) & set(target_dependencies))
    if dependency_overlap:
        score += min(0.03, len(dependency_overlap) * 0.01)
        notes.append(f"Shared dependencies: {', '.join(dependency_overlap[:6])}.")

    candidate_frameworks = {
        signal
        for dependency, signal in _FRAMEWORK_DEPENDENCY_SIGNALS.items()
        if dependency in candidate_dependencies
    }
    candidate_frameworks.update(_candidate_stack_frameworks(stack_signals))
    shared_frameworks = sorted(set(target_profile.framework_signals) & candidate_frameworks)
    if shared_frameworks:
        score += 0.04
        notes.append(f"Shared frameworks: {', '.join(shared_frameworks)}.")

    major_conflicts: list[str] = []
    unknown_major_constraints: list[str] = []
    for dependency in sorted(_NPM_MAJOR_FRAMEWORK_DEPENDENCIES & set(target_npm_dependencies)):
        candidate_constraint = candidate_dependencies.get(dependency, "")
        if dependency not in candidate_dependencies:
            continue
        target_constraint = target_npm_dependencies.get(dependency, "")
        candidate_major = npm_unambiguous_major(candidate_constraint)
        target_major = npm_unambiguous_major(target_constraint)
        if candidate_major is not None and target_major is not None and candidate_major != target_major:
            major_conflicts.append(f"{dependency} {candidate_major}→{target_major}")
        elif candidate_major is None or target_major is None:
            unknown_major_constraints.append(dependency)
    if major_conflicts:
        score -= 0.10
        notes.append(f"Known npm major-version conflict: {', '.join(major_conflicts)}.")
    if unknown_major_constraints:
        notes.append(
            f"npm major-version compatibility is unknown for: {', '.join(unknown_major_constraints)}."
        )

    if not notes:
        notes.append("Target compatibility is unknown from available deterministic metadata.")
    return round(max(-0.20, min(0.10, score)), 4), notes


def _candidate_languages(detected_languages: Any, stack_signals: Any) -> set[str]:
    languages: set[str] = set()
    if isinstance(detected_languages, dict):
        languages.update(str(value).strip().lower() for value in detected_languages.values() if value)
    elif isinstance(detected_languages, list):
        languages.update(str(value).strip().lower() for value in detected_languages if value)
    if isinstance(stack_signals, dict):
        if bool(stack_signals.get("has_typescript_files")):
            languages.add("typescript")
        if bool(stack_signals.get("has_javascript_or_typescript_files")):
            languages.add("javascript")
        if bool(stack_signals.get("has_python_files")):
            languages.add("python")
    return languages


def _language_ecosystems(languages: set[str]) -> set[str]:
    ecosystems: set[str] = set()
    if languages & {"javascript", "typescript", "tsx", "jsx"}:
        ecosystems.add("node")
    if "python" in languages:
        ecosystems.add("python")
    return ecosystems


def _candidate_dependencies(package_manifests: Any) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    if not isinstance(package_manifests, dict):
        return dependencies
    for manifest in package_manifests.values():
        if not isinstance(manifest, dict):
            continue
        for section in ("dependencies", "devDependencies"):
            values = manifest.get(section)
            if not isinstance(values, dict):
                continue
            for name, constraint in values.items():
                dependencies.setdefault(str(name).strip().lower(), str(constraint).strip())
    return dependencies


def _candidate_stack_frameworks(stack_signals: Any) -> set[str]:
    if not isinstance(stack_signals, dict):
        return set()
    signals: set[str] = set()
    if bool(stack_signals.get("has_next_dependency")):
        signals.add("nextjs")
    if bool(stack_signals.get("has_react_dependency")):
        signals.add("react")
    return signals


def _bm25_terms(value: str) -> list[str]:
    return [
        term
        for term in re.findall(r"[a-z0-9]+", value.lower())
        if len(term) > 2 and term not in _BM25_STOP_WORDS
    ]


def _role_card_terms(data: dict[str, Any]) -> list[str]:
    capability = str(data.get("capability", ""))
    entry_paths = _json_load(data.get("entry_paths"), [])
    dependency_paths = _json_load(data.get("dependency_paths"), [])
    external_dependencies = _json_load(data.get("external_dependencies"), [])
    evidence_paths = _json_load(data.get("evidence_paths"), [])
    role_terms = {
        capability,
        *CAPABILITY_INTENT_HINTS.get(capability, set()),
        *_CAPABILITY_ROLE_TERMS.get(capability, set()),
        *[str(path) for path in entry_paths],
        *[str(path) for path in dependency_paths],
        *[str(path) for path in evidence_paths],
        *[str(dependency) for dependency in external_dependencies],
    }
    return _bm25_terms(" ".join(sorted(role_terms)))


def _bm25_role_scores(task: str, rows: list[dict[str, Any]]) -> dict[str, float]:
    query_terms = set(_bm25_terms(task))
    if not query_terms or not rows:
        return {}
    documents = [_role_card_terms(data) for data in rows]
    average_length = sum(len(document) for document in documents) / len(documents)
    if average_length <= 0:
        return {}
    document_frequency = {term: sum(1 for document in documents if term in document) for term in query_terms}
    raw_scores: dict[str, float] = {}
    for data, document in zip(rows, documents, strict=True):
        frequencies = Counter(document)
        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if frequency <= 0:
                continue
            frequency_in_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                1 + (len(documents) - frequency_in_documents + 0.5) / (frequency_in_documents + 0.5)
            )
            denominator = frequency + 1.2 * (0.25 + 0.75 * len(document) / average_length)
            score += inverse_document_frequency * (frequency * 2.2 / denominator)
        raw_scores[str(data["asset_id"])] = score
    maximum = max(raw_scores.values(), default=0.0)
    if maximum <= 0:
        return {}
    return {asset_id: round(score / maximum, 4) for asset_id, score in raw_scores.items()}


def _role_overlap(task_terms: set[str], capability: str) -> int:
    return len(task_terms & _CAPABILITY_ROLE_TERMS.get(capability, set()))


def search_assets(
    task: str,
    max_repos: int,
    *,
    target_profile: TargetProfileV1 | None = None,
) -> list[ReusableCandidate]:
    conn = get_connection()
    created_cutoff = _cutoff_date(MAX_REPO_AGE_DAYS)
    pushed_cutoff = _cutoff_date(MAX_STALE_DAYS)
    rows = conn.execute(
        """
        WITH ranked_snapshots AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY repo_id
                    ORDER BY indexed_at DESC, snapshot_id DESC
                ) AS snapshot_rank
            FROM snapshots
        )
        SELECT
            a.asset_id, a.repo_id, a.capability, a.entry_paths,
            a.dependency_paths, a.external_dependencies, a.evidence_paths,
            a.synthesis, a.reuse_score, s.commit_sha, r.html_url,
            r.is_public, r.is_archived, r.repo_size_kb, r.repo_created_at,
            r.pushed_at, r.detected_languages, c.repository_profile,
            c.package_manifests, c.stack_signals
        FROM assets a
        JOIN ranked_snapshots s ON s.snapshot_id = a.snapshot_id
        JOIN repositories r ON r.repo_id = a.repo_id
        LEFT JOIN repository_cards c ON c.snapshot_id = a.snapshot_id
        WHERE s.snapshot_rank = 1
            AND r.is_public = true
            AND r.is_archived = false
            AND (r.is_mirror IS NULL OR r.is_mirror = false)
            AND (r.is_fork IS NULL OR r.is_fork = false)
            AND (r.is_template IS NULL OR r.is_template = false)
            AND (r.repo_size_kb IS NULL OR r.repo_size_kb <= ?)
            AND r.repo_created_at IS NOT NULL
            AND r.repo_created_at >= ?
            AND r.pushed_at IS NOT NULL
            AND r.pushed_at >= ?
        """,
        [MAX_REPOSITORY_SIZE_KB, created_cutoff, pushed_cutoff],
    ).fetchall()
    columns = [str(c[0]) for c in conn.description]
    row_data = [dict(zip(columns, row, strict=False)) for row in rows]
    task_terms = _task_terms(task)
    bm25_scores = _bm25_role_scores(task, row_data)
    signature = task_signature(
        task,
        target_profile.fingerprint if target_profile is not None else "",
    )
    intent_scores = _capability_intent_scores(task)
    best_intent_score = max(intent_scores.values(), default=0.0)
    primary_intent = max(intent_scores.items(), key=lambda item: item[1], default=("", 0.0))[0]
    has_bm25_role_intent = (
        max(
            (_role_overlap(task_terms, capability) for capability in _CAPABILITY_ROLE_TERMS),
            default=0,
        )
        >= BM25_MIN_ROLE_OVERLAP
    )

    scored: list[tuple[float, ReusableCandidate]] = []
    for data in row_data:
        entry_paths = _json_load(data.get("entry_paths"), [])
        dependency_paths = _json_load(data.get("dependency_paths"), [])
        external_dependencies = _json_load(data.get("external_dependencies"), [])
        evidence_paths = _json_load(data.get("evidence_paths"), [])
        synthesis = _json_load(data.get("synthesis"), {})
        repository_profile = _json_load(data.get("repository_profile"), None)
        detected_languages = _json_load(data.get("detected_languages"), {})
        package_manifests = _json_load(data.get("package_manifests"), {})
        stack_signals = _json_load(data.get("stack_signals"), {})
        searchable = " ".join(
            [
                str(data.get("capability", "")),
                str(data.get("repo_id", "")),
                " ".join(entry_paths),
                " ".join(external_dependencies),
                " ".join(synthesis.get("adaptation_notes", [])),
            ]
        ).lower()
        overlap = sum(1 for term in task_terms if term in searchable)
        capability = str(data["capability"])
        profile_has_signal = _has_profile_signal(repository_profile)
        profile_score = _profile_match_score(repository_profile) if profile_has_signal else 0.0
        ui_path_score = _synthesis_score(synthesis, "ui_path_score")
        noise_penalty = _synthesis_score(synthesis, "noise_penalty")
        capability_path_score = _synthesis_score(synthesis, "capability_path_score")
        label_signal_score = _capability_label_signal_score(
            capability,
            entry_paths + evidence_paths,
            external_dependencies,
            synthesis,
        )
        if label_signal_score < MIN_LABEL_SIGNAL:
            continue
        path_alignment_score = _backend_path_alignment_score(
            capability,
            entry_paths + evidence_paths,
        )
        base_score = _float_value(data.get("reuse_score"))
        if label_signal_score < STRONG_LABEL_SIGNAL:
            base_score = min(base_score, POSSIBLE_LABEL_SCORE_CAP)
        capability_intent_score = intent_scores.get(capability, 0.0)
        score = (
            (base_score * 0.55)
            + (ui_path_score * 0.2)
            + (profile_score * 0.25)
            + (capability_path_score * 0.12)
            + (label_signal_score * 0.08)
            + (capability_intent_score * 0.28)
            + min(0.12, overlap * 0.035)
            - (noise_penalty * 0.16)
            + path_alignment_score
        )
        if best_intent_score >= 0.35 and capability_intent_score < best_intent_score * 0.75:
            score -= 0.32
        if (
            primary_intent in BACKEND_CAPABILITIES
            and best_intent_score >= 0.7
            and capability != primary_intent
        ):
            score -= 0.22
        if (
            primary_intent in AI_DATA_CAPABILITIES
            and best_intent_score >= 0.7
            and capability != primary_intent
        ):
            score -= 0.18
        if best_intent_score >= 0.7 and capability in UI_CAPABILITIES and capability_intent_score < 0.35:
            score -= 0.18
        if capability == "data-table" and "@tanstack/react-table" not in external_dependencies:
            score -= (1 - capability_path_score) * 0.12
        if capability == "command-palette":
            dependency_set = set(external_dependencies)
            has_command_dependency = bool(COMMAND_PALETTE_DEPENDENCIES & dependency_set)
            if has_command_dependency:
                score += 0.12
            elif capability_path_score <= 0:
                score -= 0.42
            else:
                score += 0.04
        if capability == "server-actions" and not _paths_contain_any(
            entry_paths + evidence_paths,
            {"actions", "server-action"},
        ):
            score -= 0.3
        if capability == "trpc-router":
            has_trpc_dependency = "@trpc/server" in external_dependencies
            has_trpc_path = _paths_contain_any(entry_paths + evidence_paths, {"trpc"})
            if has_trpc_dependency and has_trpc_path:
                score += 0.18
            elif not has_trpc_dependency:
                score -= 0.6
        if capability == "data-access":
            db_dependencies = {"drizzle-orm", "prisma", "@prisma/client"} & set(
                external_dependencies
            )
            has_specific_db_path = _paths_contain_any(
                entry_paths + evidence_paths,
                {"drizzle", "prisma", "schema"},
            )
            if db_dependencies:
                score += 0.12
            elif not has_specific_db_path:
                score -= 0.36
        if capability == "file-storage":
            storage_dependencies = {
                "@aws-sdk/client-s3",
                "@vercel/blob",
                "@supabase/supabase-js",
                "firebase",
                "googleapis",
                "react-dropzone",
                "uploadthing",
            } & set(external_dependencies)
            has_storage_path = _paths_contain_any(
                entry_paths + evidence_paths,
                {"attachment", "blob", "document", "drive", "r2", "s3", "storage", "upload"},
            )
            if storage_dependencies:
                score += 0.14
            if not has_storage_path:
                score -= 0.32
        if capability == "model-server-integration":
            has_model_server_path = _paths_contain_any(
                entry_paths + evidence_paths,
                {"chat", "completion", "lmstudio", "model", "models", "ollama", "openai", "responses"},
            )
            if has_model_server_path:
                score += 0.12
            else:
                score -= 0.28
        if capability == "local-ai-integration":
            if "embedding" in task_terms:
                has_embedding_path = _paths_contain_any(
                    entry_paths + evidence_paths,
                    {"embed", "embedding", "embeddings"},
                )
                if has_embedding_path:
                    score += 0.18
                else:
                    score -= 0.35
            if "ollama" in task_terms and not _paths_contain_any(
                entry_paths + evidence_paths,
                {"ollama"},
            ):
                score -= 0.24
        if capability == "node-ai-sdk":
            ai_sdk_dependencies = {
                "@ai-sdk/anthropic",
                "@ai-sdk/openai",
                "@ai-sdk/react",
                "ai",
            } & set(external_dependencies)
            asks_for_ai_sdk = bool({"sdk", "streaming", "stream"} & task_terms)
            if ai_sdk_dependencies:
                score += 0.18
            elif asks_for_ai_sdk:
                score -= 0.35
        data_tool_terms = {"duckdb", "pandas", "polars"}
        if task_terms & data_tool_terms:
            data_tool_dependencies = data_tool_terms & set(external_dependencies)
            if data_tool_dependencies:
                score += 0.3
            elif capability == "data-pipeline":
                score -= 0.42
        if capability in BACKEND_CAPABILITIES and not _has_backend_path(entry_paths):
            score -= 0.28
        if capability in AI_DATA_CAPABILITIES and path_alignment_score < 0:
            score -= 0.08
        if profile_has_signal and profile_score < 0.12:
            score -= 0.08
        if not entry_paths:
            score -= 0.12
        sort_score = max(0.0, score)
        bm25_score = bm25_scores.get(str(data["asset_id"]), 0.0)
        if has_bm25_role_intent:
            if (
                _role_overlap(task_terms, capability) < BM25_MIN_ROLE_OVERLAP
                or bm25_score < MIN_TASK_RELEVANCE_SIGNAL_V1
            ):
                continue
            sort_score += bm25_score * BM25_MAX_BOOST
        else:
            task_relevance_signal = max(
                capability_intent_score,
                min(1.0, overlap / 3.0),
            )
            if best_intent_score < 0.18 or task_relevance_signal < MIN_TASK_RELEVANCE_SIGNAL_V1:
                continue
        if sort_score < MIN_RETRIEVAL_SCORE_V1:
            continue
        target_fit_score, target_fit_notes = _target_fit(
            target_profile,
            detected_languages=detected_languages,
            package_manifests=package_manifests,
            stack_signals=stack_signals,
        )
        sort_score = max(0.0, sort_score + target_fit_score)
        display_score = min(sort_score, 1.0)
        candidate = ReusableCandidate(
            candidate_id=str(data["asset_id"]),
            repo_id=str(data["repo_id"]),
            html_url=str(data["html_url"]),
            commit_sha=str(data["commit_sha"]),
            capability=capability,
            score=round(display_score, 4),
            task_signature=signature,
            entry_paths=[str(p) for p in entry_paths],
            dependency_paths=[str(p) for p in dependency_paths],
            external_dependencies=[str(p) for p in external_dependencies],
            evidence_paths=[str(p) for p in evidence_paths],
            adaptation_notes=[str(p) for p in synthesis.get("adaptation_notes", [])],
            target_fit_score=target_fit_score,
            target_fit_notes=target_fit_notes,
        )
        scored.append((sort_score, candidate))

    scored.sort(key=lambda item: item[0], reverse=True)
    unique_by_repo: dict[str, ReusableCandidate] = {}
    for _, candidate in scored:
        if candidate.repo_id not in unique_by_repo:
            unique_by_repo[candidate.repo_id] = candidate
        if len(unique_by_repo) >= max_repos:
            break
    return list(unique_by_repo.values())
