from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import httpx

from . import assessment_rules, catalog, evidence_ledger, lmstudio
from .models import (
    AdaptationStep,
    AssessmentDimensions,
    CouplingRisk,
    EvidenceBackedReason,
    MissingEvidenceRequest,
    RequirementAssessment,
    ReuseAssessmentResult,
)

PROMPT_VERSION = "gemma-reuse-assessor-v1"
SCHEMA_VERSION = "reuse-assessment-v1"
ANALYZER_VERSION = "gemma-reuse-assessor-v1"

ALLOWED_MODEL_VERDICTS = {
    assessment_rules.VERDICT_SELECT,
    assessment_rules.VERDICT_INSPECT,
    assessment_rules.VERDICT_REJECT,
    assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE,
}
ALLOWED_REQUIREMENT_STATUSES = {"satisfied", "partial", "unsatisfied", "unknown"}
ALLOWED_RISK_SEVERITIES = {"low", "medium", "high"}
ALLOWED_BLOCKER_TYPES = {
    "license",
    "missing_functionality",
    "unsupported_stack",
    "excessive_coupling",
    "other",
}
ALLOWED_RETRIEVERS = {"deterministic", "fastcontext"}
ALLOWED_PRIORITIES = {"low", "medium", "high"}
PERMISSIVE_LICENSES = {
    "0BSD",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "CC0-1.0",
    "ISC",
    "MIT",
    "Unlicense",
}


class AssessorError(RuntimeError):
    pass


async def assess_candidate(
    candidate_id: str,
    task: str,
    *,
    fastcontext_policy: Literal["auto", "always", "never"] = "auto",
    max_evidence_rounds: int = 1,
    force: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ReuseAssessmentResult:
    if not task.strip():
        raise AssessorError("task is required.")
    if fastcontext_policy not in {"auto", "always", "never"}:
        raise AssessorError("fastcontext_policy must be one of: auto, always, never.")
    if max_evidence_rounds < 0 or max_evidence_rounds > 2:
        raise AssessorError("max_evidence_rounds must be between 0 and 2.")

    if fastcontext_policy == "never":
        context = _load_context(
            candidate_id,
            task,
            fastcontext_policy,
            fastcontext_status="not_requested",
        )
        return await _assess_context(context, force=force, transport=transport)

    if fastcontext_policy == "always":
        evidence_paths, events, status = await _fastcontext_evidence_for_always(
            candidate_id,
            task,
            max_evidence_rounds=max_evidence_rounds,
            transport=transport,
        )
        context = _load_context(
            candidate_id,
            task,
            fastcontext_policy,
            fastcontext_evidence_paths=evidence_paths,
            fastcontext_events=events,
            fastcontext_status=status,
        )
        return await _assess_context(context, force=force, transport=transport)

    initial_context = _load_context(
        candidate_id,
        task,
        fastcontext_policy,
        fastcontext_status="not_requested",
    )
    initial = await _assess_context(initial_context, force=force, transport=transport)
    if max_evidence_rounds == 0 or not _has_eligible_fastcontext_request(initial):
        return initial

    parent_task_signature = catalog.task_signature(task)
    existing_paths, existing_events = _existing_fastcontext_evidence(
        candidate_id,
        parent_task_signature,
    )
    if existing_paths:
        context = _load_context(
            candidate_id,
            task,
            fastcontext_policy,
            fastcontext_evidence_paths=existing_paths,
            fastcontext_events=existing_events,
            fastcontext_status="reused_existing",
        )
        return await _assess_context(context, force=force, transport=transport)

    evidence_paths, events, status = await _run_fastcontext_rounds(
        candidate_id,
        task,
        assessment=initial,
        max_evidence_rounds=max_evidence_rounds,
        transport=transport,
    )
    context = _load_context(
        candidate_id,
        task,
        fastcontext_policy,
        fastcontext_evidence_paths=evidence_paths,
        fastcontext_events=events,
        fastcontext_status=status,
    )
    return await _assess_context(context, force=True, transport=transport)


async def _assess_context(
    context: Mapping[str, Any],
    *,
    force: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> ReuseAssessmentResult:
    if not force:
        cached = catalog.get_latest_reuse_assessment(
            str(context["asset"]["asset_id"]),
            str(context["task_signature"]),
            str(context["input_fingerprint"]),
        )
        if cached is not None:
            catalog.record_analysis_run(
                "reuse-assess",
                "cached",
                {
                    "candidate_id": context["asset"]["asset_id"],
                    "task_signature": context["task_signature"],
                    "input_fingerprint": context["input_fingerprint"],
                    "assessment_id": cached.assessment_id,
                    "fastcontext_events": context["fastcontext_events"],
                },
                repo_id=str(context["asset"]["repo_id"]),
                snapshot_id=str(context["asset"]["snapshot_id"]),
                model_id=str(context["config"].gemma_model),
                prompt_version=PROMPT_VERSION,
                analyzer_version=ANALYZER_VERSION,
            )
            return cached

    if not context["evidence_ledger"].items:
        return _persist_safe_assessment(
            context,
            status="completed_fallback",
            validation_notes=[
                "No validated deterministic evidence was available; skipped Gemma assessment.",
                *context["evidence_ledger"].validation_notes,
            ],
        )

    raw_response: dict[str, Any] | None = None
    validation_errors: list[str] = []
    try:
        raw_response = await lmstudio.chat_json(
            model_id=str(context["config"].gemma_model),
            messages=_assessment_messages(context),
            config=context["config"],
            transport=transport,
            max_tokens=4000,
            temperature=0.0,
            attempts=1,
        )
        normalized = _normalize_response(raw_response, context["evidence_id_map"])
    except lmstudio.LMStudioError:
        raise
    except Exception as exc:
        validation_errors = _validation_errors(exc)
        try:
            repair_response = await lmstudio.chat_json(
                model_id=str(context["config"].gemma_model),
                messages=_repair_messages(context, raw_response, validation_errors),
                config=context["config"],
                transport=transport,
                max_tokens=4000,
                temperature=0.0,
                attempts=1,
            )
            normalized = _normalize_response(repair_response, context["evidence_id_map"])
            return _persist_assessment(
                context,
                normalized,
                status="completed_repaired",
                validation_notes=[
                    "Initial Gemma reuse assessment response required repair.",
                    *validation_errors,
                ],
            )
        except lmstudio.LMStudioError:
            raise
        except Exception as repair_exc:
            return _persist_safe_assessment(
                context,
                status="completed_fallback",
                validation_notes=[
                    "Gemma reuse assessment response failed validation after one repair attempt.",
                    *validation_errors,
                    *_validation_errors(repair_exc),
                ],
            )

    return _persist_assessment(context, normalized, status="completed", validation_notes=[])


async def _fastcontext_evidence_for_always(
    candidate_id: str,
    task: str,
    *,
    max_evidence_rounds: int,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[list[str], list[dict[str, Any]], str]:
    if max_evidence_rounds == 0:
        return [], [], "not_requested"
    parent_task_signature = catalog.task_signature(task)
    existing_paths, existing_events = _existing_fastcontext_evidence(
        candidate_id,
        parent_task_signature,
    )
    context = _load_context(
        candidate_id,
        task,
        "always",
        fastcontext_evidence_paths=existing_paths,
        fastcontext_events=existing_events,
        fastcontext_status="attempting",
    )
    attempt_paths, attempt_event = await _attempt_fastcontext_refinement(
        candidate_id,
        task,
        context=context,
        assessment=None,
        round_index=1,
        transport=transport,
    )
    all_paths = _unique_evidence_paths([*existing_paths, *attempt_paths])
    events = [*existing_events, attempt_event]
    if attempt_event["status"] == "completed":
        return all_paths, events, "completed"
    if existing_paths:
        return all_paths, events, "failed_with_existing"
    return all_paths, events, "failed"


async def _run_fastcontext_rounds(
    candidate_id: str,
    task: str,
    *,
    assessment: ReuseAssessmentResult,
    max_evidence_rounds: int,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[list[str], list[dict[str, Any]], str]:
    evidence_paths: list[str] = []
    events: list[dict[str, Any]] = []
    status = "not_requested"
    for round_index in range(1, max_evidence_rounds + 1):
        context = _load_context(
            candidate_id,
            task,
            "auto",
            fastcontext_evidence_paths=evidence_paths,
            fastcontext_events=events,
            fastcontext_status="attempting",
        )
        attempt_paths, event = await _attempt_fastcontext_refinement(
            candidate_id,
            task,
            context=context,
            assessment=assessment,
            round_index=round_index,
            transport=transport,
        )
        events.append(event)
        if event["status"] == "completed":
            evidence_paths = _unique_evidence_paths([*evidence_paths, *attempt_paths])
            status = "completed"
            break
        status = "failed"
        break
    return evidence_paths, events, status


async def _attempt_fastcontext_refinement(
    candidate_id: str,
    task: str,
    *,
    context: Mapping[str, Any],
    assessment: ReuseAssessmentResult | None,
    round_index: int,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[list[str], dict[str, Any]]:
    from . import fastcontext

    query = _focused_fastcontext_query(context, assessment)
    event: dict[str, Any] = {
        "round": round_index,
        "query": query,
        "status": "attempted",
        "refinement_id": None,
        "analysis_run_id": None,
        "evidence_count": 0,
        "error": None,
    }
    try:
        result = await fastcontext.refine_candidate(
            candidate_id=candidate_id,
            task=query,
            transport=transport,
            task_signature_override=str(context["task_signature"]),
        )
    except Exception as exc:
        event["status"] = "failed"
        event["error"] = str(exc)
        return [], event

    evidence_paths = [str(path) for path in result.get("evidence_paths", [])]
    event.update(
        {
            "status": "completed",
            "refinement_id": result.get("refinement_id"),
            "analysis_run_id": result.get("analysis_run_id"),
            "evidence_count": len(evidence_paths),
            "notes": result.get("notes", []),
        }
    )
    return evidence_paths, event


def _existing_fastcontext_evidence(
    candidate_id: str,
    task_signature: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    evidence_paths: list[str] = []
    events: list[dict[str, Any]] = []
    for refinement in catalog.list_evidence_refinements(
        candidate_id,
        task_signature=task_signature,
    ):
        paths = [str(path) for path in refinement.get("evidence_paths", [])]
        evidence_paths.extend(paths)
        events.append(
            {
                "round": 0,
                "query": refinement.get("query"),
                "status": "reused_existing",
                "refinement_id": refinement.get("refinement_id"),
                "analysis_run_id": None,
                "evidence_count": len(paths),
                "error": None,
            }
        )
    return _unique_evidence_paths(evidence_paths), events


def _focused_fastcontext_query(
    context: Mapping[str, Any],
    assessment: ReuseAssessmentResult | None,
) -> str:
    asset = context["asset"]
    lines = [
        f"Task: {context['task']}",
        f"Capability: {asset['capability']}",
        "Goal: find focused source file and line evidence for implementation inspection only.",
        "Do not decide, score, prove, or reject reusability.",
    ]
    existing_paths = [
        f"{item['path']}:{item['start_line']}-{item['end_line']}"
        for item in context["evidence_ledger"].items
    ]
    if existing_paths:
        lines.append("Existing validated evidence paths:")
        lines.extend(f"- {path}" for path in existing_paths[:8])
    if assessment is not None:
        questions = _eligible_fastcontext_questions(assessment)
        if questions:
            lines.append("Unresolved evidence questions:")
            lines.extend(f"- {question}" for question in questions)
    return "\n".join(lines)


def _has_eligible_fastcontext_request(assessment: ReuseAssessmentResult) -> bool:
    return bool(_eligible_fastcontext_questions(assessment))


def _eligible_fastcontext_questions(assessment: ReuseAssessmentResult) -> list[str]:
    questions: list[str] = []
    for item in assessment.missing_evidence:
        reason = item.reason.lower()
        if "preferred_retriever=fastcontext" not in reason:
            continue
        if "priority=medium" not in reason and "priority=high" not in reason:
            continue
        if item.question:
            questions.append(item.question)
    return questions


def _unique_evidence_paths(evidence_paths: Sequence[str]) -> list[str]:
    return sorted({str(path) for path in evidence_paths})


def _fastcontext_event_notes(events: Sequence[Mapping[str, Any]]) -> list[str]:
    notes: list[str] = []
    for event in events:
        status = str(event.get("status", ""))
        refinement_id = event.get("refinement_id")
        evidence_count = event.get("evidence_count")
        if status == "completed":
            notes.append(
                f"FastContext refinement completed: refinement_id={refinement_id}; "
                f"evidence_count={evidence_count}."
            )
        elif status == "reused_existing":
            notes.append(
                f"FastContext refinement reused: refinement_id={refinement_id}; "
                f"evidence_count={evidence_count}."
            )
        elif status == "failed":
            notes.append(f"FastContext refinement failed: {event.get('error')}")
    return notes


def _load_context(
    candidate_id: str,
    task: str,
    fastcontext_policy: str,
    *,
    fastcontext_evidence_paths: Sequence[str] = (),
    fastcontext_events: Sequence[Mapping[str, Any]] = (),
    fastcontext_status: str,
) -> dict[str, Any]:
    asset = catalog.get_asset_detail(candidate_id)
    if asset is None:
        raise AssessorError(f"Unknown candidate_id: {candidate_id}")
    repo = catalog.get_repository(str(asset["repo_id"]))
    if repo is None:
        raise AssessorError(f"Repository metadata is missing for {asset['repo_id']}")
    snapshot = catalog.get_snapshot(str(asset["snapshot_id"]))
    if snapshot is None:
        raise AssessorError(f"Snapshot metadata is missing for {asset['snapshot_id']}")
    card = catalog.get_repository_card_for_snapshot(str(asset["snapshot_id"]))
    if card is None:
        raise AssessorError(f"Repository card is missing for snapshot {asset['snapshot_id']}")

    task_sig = catalog.task_signature(task)
    ledger = evidence_ledger.build_candidate_evidence_ledger(
        candidate_id,
        task_signature=task_sig,
        fastcontext_evidence_paths=fastcontext_evidence_paths,
    )
    license_status = _license_status(repo)
    config = lmstudio.get_config()
    bundle_manifest = _matching_bundle_manifest(
        candidate_id=candidate_id,
        task_signature=task_sig,
        commit_sha=str(asset["commit_sha"]),
        snapshot_path=Path(str(asset["snapshot_path"])),
    )
    prompt_payload = _prompt_payload(
        task=task.strip(),
        task_signature=task_sig,
        asset=asset,
        repo=repo,
        snapshot=snapshot,
        card=card,
        evidence_items=ledger.items,
        bundle_manifest=bundle_manifest,
        license_status=license_status,
    )
    fingerprint_payload = {
        "model_id": config.gemma_model,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "analyzer_version": ANALYZER_VERSION,
        "fastcontext_policy": fastcontext_policy,
        "fastcontext_status": fastcontext_status,
        "fastcontext_events": [dict(event) for event in fastcontext_events],
        "prompt_payload": prompt_payload,
        "evidence_validation_notes": ledger.validation_notes,
    }
    return {
        "asset": asset,
        "repo": repo,
        "snapshot": snapshot,
        "card": card,
        "task": task.strip(),
        "task_signature": task_sig,
        "fastcontext_policy": fastcontext_policy,
        "fastcontext_status": fastcontext_status,
        "fastcontext_events": [dict(event) for event in fastcontext_events],
        "evidence_ledger": ledger,
        "evidence_id_map": _evidence_id_map(ledger.items),
        "license_status": license_status,
        "config": config,
        "prompt_payload": prompt_payload,
        "input_fingerprint": _fingerprint(fingerprint_payload),
    }


def _assessment_messages(context: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You assess whether a repository candidate contains reusable source for a task. "
                "Return only valid JSON. You do not have tools. Use only the provided evidence "
                "ledger IDs for source-specific claims. Do not output reuse_score or final score."
            ),
        },
        {
            "role": "user",
            "content": (
                "Assess this candidate using only the JSON context below.\n\n"
                f"Context JSON:\n{json.dumps(context['prompt_payload'], sort_keys=True)}\n\n"
                "Return exactly this JSON object shape:\n"
                "{\n"
                '  "recommended_verdict": "select|inspect|reject|insufficient_evidence",\n'
                '  "model_confidence": 0.0,\n'
                '  "dimension_scores": {\n'
                '    "functional_fit": 0.0,\n'
                '    "extractability": 0.0,\n'
                '    "dependency_fit": 0.0,\n'
                '    "coupling_risk": 0.0,\n'
                '    "maintenance_risk": 0.0\n'
                "  },\n"
                '  "requirement_assessments": [\n'
                '    {"requirement": "string", "status": "satisfied|partial|unsatisfied|unknown", '
                '"evidence_ids": ["E_<hash>"]}\n'
                "  ],\n"
                '  "fit_reasons": [{"text": "string", "evidence_ids": ["E_<hash>"]}],\n'
                '  "adaptation_plan": [{"step": "string", "evidence_ids": ["E_<hash>"]}],\n'
                '  "coupling_risks": [{"risk": "string", "severity": "low|medium|high", '
                '"evidence_ids": ["E_<hash>"]}],\n'
                '  "blockers": [{"type": "license|missing_functionality|unsupported_stack|'
                'excessive_coupling|other", "severity": "low|medium|high", '
                '"text": "string", "evidence_ids": []}],\n'
                '  "missing_evidence": [{"question": "string", "preferred_retriever": '
                '"deterministic|fastcontext", "priority": "low|medium|high"}],\n'
                '  "needs_fastcontext": false\n'
                "}"
            ),
        },
    ]


def _repair_messages(
    context: Mapping[str, Any],
    raw_response: Mapping[str, Any] | None,
    validation_errors: Sequence[str],
) -> list[dict[str, str]]:
    allowed_ids = sorted(context["evidence_id_map"])
    return [
        *_assessment_messages(context),
        {
            "role": "assistant",
            "content": json.dumps(raw_response or {}, sort_keys=True),
        },
        {
            "role": "user",
            "content": (
                "Repair the JSON once. Return only the same schema. Validation errors:\n"
                f"{json.dumps(list(validation_errors), sort_keys=True)}\n\n"
                "Allowed evidence IDs:\n"
                f"{json.dumps(allowed_ids, sort_keys=True)}"
            ),
        },
    ]


def _prompt_payload(
    *,
    task: str,
    task_signature: str,
    asset: Mapping[str, Any],
    repo: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    card: Mapping[str, Any],
    evidence_items: Sequence[Mapping[str, Any]],
    bundle_manifest: Mapping[str, Any] | None,
    license_status: str,
) -> dict[str, Any]:
    return {
        "task": task,
        "task_signature": task_signature,
        "candidate": {
            "candidate_id": asset["asset_id"],
            "capability": asset["capability"],
            "entry_paths": asset["entry_paths"],
            "dependency_paths": asset["dependency_paths"],
            "external_dependencies": asset["external_dependencies"],
            "deterministic_synthesis": asset["synthesis"],
        },
        "repository": {
            "repo_id": repo["repo_id"],
            "html_url": repo["html_url"],
            "description": repo.get("description"),
            "topics": _json_field(repo.get("topics"), []),
            "detected_languages": _json_field(repo.get("detected_languages"), {}),
            "license_spdx": repo.get("license_spdx"),
            "license_status": license_status,
            "stars": repo.get("stars"),
            "pushed_at": repo.get("pushed_at"),
        },
        "snapshot": {
            "snapshot_id": snapshot["snapshot_id"],
            "commit_sha": snapshot["commit_sha"],
            "default_branch": snapshot.get("default_branch"),
            "analyzer_version": snapshot["analyzer_version"],
        },
        "repository_card": {
            "card_id": card["card_id"],
            "card_version": card["card_version"],
            "package_manifests": card["package_manifests"],
            "tree_summary": card["tree_summary"],
            "readme_excerpt": card.get("readme_excerpt"),
            "stack_signals": card["stack_signals"],
            "deterministic_features": card["deterministic_features"],
            "gemma_profile": card.get("gemma_profile"),
        },
        "source_bundle_manifest": bundle_manifest,
        "evidence_ledger": list(evidence_items),
        "allowed_evidence_ids": [str(item["evidence_id"]) for item in evidence_items],
        "instructions": {
            "no_tools": True,
            "do_not_output_score": True,
            "source_claims_require_evidence_ids": True,
            "cite_only_allowed_evidence_ids": True,
        },
    }


def _normalize_response(
    response: Mapping[str, Any],
    evidence_id_map: Mapping[str, str],
) -> dict[str, Any]:
    errors: list[str] = []
    verdict = _string(response.get("recommended_verdict"))
    if verdict not in ALLOWED_MODEL_VERDICTS:
        errors.append(f"Invalid recommended_verdict: {verdict}")

    dimensions_raw = response.get("dimension_scores")
    if not isinstance(dimensions_raw, Mapping):
        dimensions_raw = {}
    dimensions = AssessmentDimensions(
        functional_fit=assessment_rules.clamp_dimension_score(
            _float_value(dimensions_raw.get("functional_fit"))
        ),
        extractability=assessment_rules.clamp_dimension_score(
            _float_value(dimensions_raw.get("extractability"))
        ),
        dependency_fit=assessment_rules.clamp_dimension_score(
            _float_value(dimensions_raw.get("dependency_fit"))
        ),
        coupling_risk=assessment_rules.clamp_dimension_score(
            _float_value(dimensions_raw.get("coupling_risk"))
        ),
        maintenance_risk=assessment_rules.clamp_dimension_score(
            _float_value(dimensions_raw.get("maintenance_risk"))
        ),
    )

    requirements = _normalize_requirements(response.get("requirement_assessments"), evidence_id_map, errors)
    reasons = _normalize_reasons(response.get("fit_reasons"), evidence_id_map, errors)
    adaptation_steps = _normalize_adaptation_steps(response.get("adaptation_plan"), evidence_id_map, errors)
    coupling_risks = _normalize_coupling_risks(response.get("coupling_risks"), evidence_id_map, errors)
    coupling_risks.extend(_normalize_blockers(response.get("blockers"), evidence_id_map, errors))
    missing_evidence = _normalize_missing_evidence(response.get("missing_evidence"), errors)
    if errors:
        raise AssessorError("; ".join(errors))
    return {
        "model_recommended_verdict": verdict,
        "model_confidence": assessment_rules.clamp_dimension_score(
            _float_value(response.get("model_confidence"))
        ),
        "dimensions": dimensions,
        "requirements": requirements,
        "reasons": reasons,
        "adaptation_steps": adaptation_steps,
        "coupling_risks": coupling_risks,
        "missing_evidence": missing_evidence,
        "needs_fastcontext": bool(response.get("needs_fastcontext", False)),
    }


def _normalize_requirements(
    raw: Any,
    evidence_id_map: Mapping[str, str],
    errors: list[str],
) -> list[RequirementAssessment]:
    items = _list_of_mappings(raw, "requirement_assessments", errors)
    requirements: list[RequirementAssessment] = []
    for item in items:
        requirement = _string(item.get("requirement")).strip()
        status = _string(item.get("status"))
        if status not in ALLOWED_REQUIREMENT_STATUSES:
            errors.append(f"Invalid requirement status: {status}")
        evidence_paths = _evidence_paths(item.get("evidence_ids"), evidence_id_map, errors)
        if status in {"satisfied", "partial"} and not evidence_paths:
            errors.append(f"Requirement '{requirement}' has status '{status}' without evidence_ids.")
        requirements.append(
            RequirementAssessment(
                requirement=requirement,
                satisfied=status == "satisfied",
                status=status,
                evidence_paths=evidence_paths,
                notes=[f"status: {status}"],
            )
        )
    return requirements


def _normalize_reasons(
    raw: Any,
    evidence_id_map: Mapping[str, str],
    errors: list[str],
) -> list[EvidenceBackedReason]:
    reasons: list[EvidenceBackedReason] = []
    for item in _list_of_mappings(raw, "fit_reasons", errors):
        text = _string(item.get("text")).strip()
        evidence_paths = _evidence_paths(item.get("evidence_ids"), evidence_id_map, errors)
        if text and not evidence_paths:
            errors.append(f"Fit reason '{text}' is missing evidence_ids.")
        reasons.append(EvidenceBackedReason(reason=text, evidence_paths=evidence_paths))
    return reasons


def _normalize_adaptation_steps(
    raw: Any,
    evidence_id_map: Mapping[str, str],
    errors: list[str],
) -> list[AdaptationStep]:
    steps: list[AdaptationStep] = []
    for item in _list_of_mappings(raw, "adaptation_plan", errors):
        step = _string(item.get("step")).strip()
        evidence_paths = _evidence_paths(item.get("evidence_ids"), evidence_id_map, errors)
        if step and not evidence_paths:
            errors.append(f"Adaptation step '{step}' is missing evidence_ids.")
        steps.append(
            AdaptationStep(
                summary=step,
                source_paths=sorted({_citation_path(path) for path in evidence_paths}),
            )
        )
    return steps


def _normalize_coupling_risks(
    raw: Any,
    evidence_id_map: Mapping[str, str],
    errors: list[str],
) -> list[CouplingRisk]:
    risks: list[CouplingRisk] = []
    for item in _list_of_mappings(raw, "coupling_risks", errors):
        risk = _string(item.get("risk")).strip()
        severity = _string(item.get("severity"))
        if severity not in ALLOWED_RISK_SEVERITIES:
            errors.append(f"Invalid coupling risk severity: {severity}")
            severity = "medium"
        evidence_paths = _evidence_paths(item.get("evidence_ids"), evidence_id_map, errors)
        if risk and not evidence_paths:
            errors.append(f"Coupling risk '{risk}' is missing evidence_ids.")
        risks.append(
            CouplingRisk(
                risk=risk,
                severity=severity,
                evidence_paths=evidence_paths,
                hard_blocker=False,
            )
        )
    return risks


def _normalize_blockers(
    raw: Any,
    evidence_id_map: Mapping[str, str],
    errors: list[str],
) -> list[CouplingRisk]:
    blockers: list[CouplingRisk] = []
    for item in _list_of_mappings(raw, "blockers", errors):
        blocker_type = _string(item.get("type"))
        if blocker_type not in ALLOWED_BLOCKER_TYPES:
            errors.append(f"Invalid blocker type: {blocker_type}")
            blocker_type = "other"
        text = _string(item.get("text")).strip()
        severity = _blocker_severity(blocker_type, item.get("severity"))
        evidence_paths = _evidence_paths(item.get("evidence_ids"), evidence_id_map, errors)
        hard_blocker = _is_hard_blocker(blocker_type, severity, evidence_paths)
        blockers.append(
            CouplingRisk(
                risk=f"Blocker ({blocker_type}): {text}",
                severity=severity,
                evidence_paths=evidence_paths,
                hard_blocker=hard_blocker,
            )
        )
    return blockers


def _blocker_severity(blocker_type: str, raw_severity: Any) -> str:
    severity = _string(raw_severity)
    if severity in ALLOWED_RISK_SEVERITIES:
        return severity
    if blocker_type in {"missing_functionality", "unsupported_stack", "excessive_coupling"}:
        return "high"
    return "medium"


def _is_hard_blocker(blocker_type: str, severity: str, evidence_paths: Sequence[str]) -> bool:
    if blocker_type == "license":
        return False
    if blocker_type in {"missing_functionality", "unsupported_stack", "excessive_coupling"}:
        return severity == "high" and bool(evidence_paths)
    if blocker_type == "other":
        return severity == "high" and bool(evidence_paths)
    return False


def _normalize_missing_evidence(raw: Any, errors: list[str]) -> list[MissingEvidenceRequest]:
    requests: list[MissingEvidenceRequest] = []
    for item in _list_of_mappings(raw, "missing_evidence", errors):
        question = _string(item.get("question")).strip()
        retriever = _string(item.get("preferred_retriever"))
        priority = _string(item.get("priority"))
        if retriever not in ALLOWED_RETRIEVERS:
            errors.append(f"Invalid missing_evidence preferred_retriever: {retriever}")
        if priority not in ALLOWED_PRIORITIES:
            errors.append(f"Invalid missing_evidence priority: {priority}")
        requests.append(
            MissingEvidenceRequest(
                question=question,
                reason=f"preferred_retriever={retriever}; priority={priority}",
            )
        )
    return requests


def _persist_assessment(
    context: Mapping[str, Any],
    normalized: Mapping[str, Any],
    *,
    status: str,
    validation_notes: list[str],
) -> ReuseAssessmentResult:
    score = assessment_rules.score_assessment(
        normalized["dimensions"],
        normalized["requirements"],
        float(normalized["model_confidence"]),
        str(context["license_status"]),
        normalized["coupling_risks"],
    )
    notes = [
        *context["evidence_ledger"].validation_notes,
        *validation_notes,
        *_fastcontext_event_notes(context["fastcontext_events"]),
        f"model_recommended_verdict: {normalized['model_recommended_verdict']}",
    ]
    if normalized["needs_fastcontext"]:
        notes.append(
            f"Gemma requested FastContext while fastcontext_policy={context['fastcontext_policy']}."
        )
    assessment = ReuseAssessmentResult(
        candidate_id=str(context["asset"]["asset_id"]),
        repo_id=str(context["asset"]["repo_id"]),
        snapshot_id=str(context["asset"]["snapshot_id"]),
        commit_sha=str(context["asset"]["commit_sha"]),
        task=str(context["task"]),
        task_signature=str(context["task_signature"]),
        model_id=str(context["config"].gemma_model),
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        analyzer_version=ANALYZER_VERSION,
        input_fingerprint=str(context["input_fingerprint"]),
        fastcontext_policy=str(context["fastcontext_policy"]),
        fastcontext_status=str(context["fastcontext_status"]),
        license_status=str(context["license_status"]),
        recommended_verdict=str(normalized["model_recommended_verdict"]),
        final_verdict=score.final_verdict,
        reuse_score=round(score.reuse_score, 4),
        model_confidence=round(score.model_confidence, 4),
        confidence=round(score.confidence, 4),
        evidence_coverage=round(score.evidence_coverage, 4),
        requirement_count=score.requirement_count,
        satisfied_requirement_count=score.satisfied_requirement_count,
        evidence_requirement_count=score.evidence_requirement_count,
        dimensions=assessment_rules.normalize_dimensions(normalized["dimensions"]),
        requirements=list(normalized["requirements"]),
        reasons=list(normalized["reasons"]),
        adaptation_steps=list(normalized["adaptation_steps"]),
        coupling_risks=list(normalized["coupling_risks"]),
        missing_evidence=list(normalized["missing_evidence"]),
        evidence_ledger=list(context["evidence_ledger"].items),
        validation_notes=notes,
    )
    return _store_and_record(context, assessment, status=status)


def _persist_safe_assessment(
    context: Mapping[str, Any],
    *,
    status: str,
    validation_notes: list[str],
) -> ReuseAssessmentResult:
    dimensions = AssessmentDimensions(0.0, 0.0, 0.0, 1.0, 1.0)
    assessment = ReuseAssessmentResult(
        candidate_id=str(context["asset"]["asset_id"]),
        repo_id=str(context["asset"]["repo_id"]),
        snapshot_id=str(context["asset"]["snapshot_id"]),
        commit_sha=str(context["asset"]["commit_sha"]),
        task=str(context["task"]),
        task_signature=str(context["task_signature"]),
        model_id=str(context["config"].gemma_model),
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        analyzer_version=ANALYZER_VERSION,
        input_fingerprint=str(context["input_fingerprint"]),
        fastcontext_policy=str(context["fastcontext_policy"]),
        fastcontext_status=str(context["fastcontext_status"]),
        license_status=str(context["license_status"]),
        recommended_verdict=assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE,
        final_verdict=assessment_rules.VERDICT_INSUFFICIENT_EVIDENCE,
        reuse_score=0.0,
        model_confidence=0.0,
        confidence=0.0,
        evidence_coverage=0.0,
        requirement_count=0,
        satisfied_requirement_count=0,
        evidence_requirement_count=0,
        dimensions=dimensions,
        requirements=[],
        reasons=[],
        adaptation_steps=[],
        coupling_risks=[],
        missing_evidence=[
            MissingEvidenceRequest(
                question="Need validated source evidence before reuse can be assessed.",
                reason="preferred_retriever=deterministic; priority=high",
            )
        ],
        evidence_ledger=list(context["evidence_ledger"].items),
        validation_notes=[*validation_notes, *_fastcontext_event_notes(context["fastcontext_events"])],
    )
    return _store_and_record(context, assessment, status=status)


def _store_and_record(
    context: Mapping[str, Any],
    assessment: ReuseAssessmentResult,
    *,
    status: str,
) -> ReuseAssessmentResult:
    assessment_id = catalog.store_reuse_assessment(assessment)
    stored = catalog.get_reuse_assessment(assessment_id)
    if stored is None:
        raise AssessorError(f"Stored assessment could not be reloaded: {assessment_id}")
    catalog.record_analysis_run(
        "reuse-assess",
        status,
        {
            "candidate_id": assessment.candidate_id,
            "task_signature": assessment.task_signature,
            "input_fingerprint": assessment.input_fingerprint,
            "assessment_id": assessment_id,
            "final_verdict": assessment.final_verdict,
            "reuse_score": assessment.reuse_score,
            "fastcontext_policy": assessment.fastcontext_policy,
            "fastcontext_status": assessment.fastcontext_status,
            "fastcontext_events": context["fastcontext_events"],
        },
        repo_id=assessment.repo_id,
        snapshot_id=assessment.snapshot_id,
        model_id=assessment.model_id,
        prompt_version=PROMPT_VERSION,
        analyzer_version=ANALYZER_VERSION,
    )
    return stored


def _license_status(repo: Mapping[str, Any]) -> str:
    spdx = repo.get("license_spdx")
    if spdx is None or str(spdx).strip() == "":
        return assessment_rules.LICENSE_MISSING
    normalized = str(spdx).strip()
    if normalized in PERMISSIVE_LICENSES:
        return assessment_rules.LICENSE_PERMISSIVE_DETECTED
    if normalized in {"NOASSERTION", "UNKNOWN"} or normalized.startswith("LicenseRef-"):
        return assessment_rules.LICENSE_UNKNOWN
    return assessment_rules.LICENSE_REVIEW_REQUIRED


def _matching_bundle_manifest(
    *,
    candidate_id: str,
    task_signature: str,
    commit_sha: str,
    snapshot_path: Path,
) -> dict[str, Any] | None:
    manifest_path = catalog.bundle_path(candidate_id) / "bundle.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("candidate_id") != candidate_id:
        return None
    if manifest.get("task_signature") != task_signature:
        return None
    if manifest.get("commit_sha") != commit_sha:
        return None
    if manifest.get("source_snapshot"):
        try:
            if Path(str(manifest["source_snapshot"])).resolve() != snapshot_path.resolve():
                return None
        except OSError:
            return None
    return {
        "candidate_id": manifest.get("candidate_id"),
        "task_signature": manifest.get("task_signature"),
        "commit_sha": manifest.get("commit_sha"),
        "files": manifest.get("files", []),
        "evidence_paths": manifest.get("evidence_paths", []),
        "external_dependencies": manifest.get("external_dependencies", []),
    }


def _evidence_id_map(items: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    return {
        str(item["evidence_id"]): (
            f"{item['path']}:{int(item['start_line'])}-{int(item['end_line'])}"
        )
        for item in items
    }


def _evidence_paths(raw_ids: Any, evidence_id_map: Mapping[str, str], errors: list[str]) -> list[str]:
    if raw_ids is None:
        return []
    if not isinstance(raw_ids, list):
        errors.append("evidence_ids must be a list.")
        return []
    evidence_paths: list[str] = []
    for raw_id in raw_ids:
        evidence_id = _string(raw_id)
        path = evidence_id_map.get(evidence_id)
        if path is None:
            errors.append(f"Unknown evidence_id: {evidence_id}")
            continue
        if path not in evidence_paths:
            evidence_paths.append(path)
    return evidence_paths


def _list_of_mappings(raw: Any, field_name: str, errors: list[str]) -> list[Mapping[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{field_name} must be a list.")
        return []
    items: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            errors.append(f"{field_name} entries must be objects.")
            continue
        items.append(item)
    return items


def _json_field(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return raw


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _string(value: Any) -> str:
    return str(value or "").strip()


def _validation_errors(exc: Exception) -> list[str]:
    if isinstance(exc, AssessorError):
        return [str(exc)]
    return [f"{type(exc).__name__}: {exc}"]


def _citation_path(evidence_path: str) -> str:
    if ":" not in evidence_path:
        return evidence_path
    path, _range = evidence_path.rsplit(":", 1)
    return path


def assessment_to_jsonable(assessment: ReuseAssessmentResult) -> dict[str, Any]:
    return asdict(assessment)
