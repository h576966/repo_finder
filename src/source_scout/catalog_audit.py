import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import catalog_core
from .catalog_scoring import (
    MIN_LABEL_SIGNAL,
    STRONG_LABEL_SIGNAL,
    _capability_label_signal_score,
)
from .constants import MAX_REPOSITORY_SIZE_KB, MAX_STALE_DAYS
from .profiler import PROFILE_SCHEMA_VERSION

BUCKETS = (
    "high_quality",
    "stale",
    "noisy",
    "unprofiled",
    "failed_profile",
    "outdated_profile",
    "low_signal",
    "discard_candidates",
)
SCOPES = ("downloaded", "cataloged", "all")
RECOMMENDATIONS = ("keep", "reprofile", "discard", "review")
PROFILE_TARGET_STATUSES = {"unprofiled", "failed_profile", "outdated_profile"}

HIGH_QUALITY_SCORE = 0.72
HIGH_ASSET_SCORE = 0.75
HIGH_PROFILE_QUALITY = 0.65
LOW_SIGNAL_SCORE = 0.35
LOW_ASSET_SCORE = 0.35
STRONG_CAPABILITY_SCORE = 0.75
POSSIBLE_CAPABILITY_SCORE = 0.45
STRONG_CAPABILITY_MAX_NOISE = 0.45
POSSIBLE_CAPABILITY_MAX_NOISE = 0.70
EXPLOSION_TOTAL_CAPABILITY_COUNT = 20
EXPLOSION_STRONG_CAPABILITY_COUNT = 12
NOISY_CONCERN_TERMS = {
    "boilerplate",
    "coupled",
    "demo",
    "example",
    "generated",
    "low quality",
    "toy",
    "unclear",
    "unmaintained",
}


def audit_catalog(
    *,
    limit_per_bucket: int = 10,
    bucket: str | None = None,
    scope: str = "downloaded",
    now: datetime | None = None,
) -> dict[str, Any]:
    selected_bucket = _normalize_bucket(bucket)
    selected_scope = _normalize_scope(scope)
    audit_now = now or datetime.now(UTC)
    dataset = _load_audit_dataset(audit_now)
    all_items = dataset["items"]
    items = _filter_items_for_scope(all_items, selected_scope)

    bucket_counts = {name: sum(1 for item in items if name in item["buckets"]) for name in BUCKETS}
    requested_buckets = [selected_bucket] if selected_bucket else list(BUCKETS)
    buckets = {
        name: [_summarize_item(item) for item in _sort_bucket(items, name)[: max(0, limit_per_bucket)]]
        for name in requested_buckets
    }
    recommendations = {
        name: [
            _summarize_item(item) for item in _sort_recommendation(items, name)[: max(0, limit_per_bucket)]
        ]
        for name in RECOMMENDATIONS
    }

    return {
        "summary": {
            "scope": selected_scope,
            "repositories": len(items),
            "total_repositories": len(all_items),
            "downloaded_repositories": sum(1 for item in items if item["downloaded"]),
            "snapshots": _scoped_count(dataset["snapshots"], items),
            "repository_cards": _scoped_count(dataset["cards"], items),
            "assets": _scoped_count(dataset["assets"], items),
            **bucket_counts,
        },
        "thresholds": {
            "profile_schema_version": PROFILE_SCHEMA_VERSION,
            "stale_after_days": MAX_STALE_DAYS,
            "high_quality_score": HIGH_QUALITY_SCORE,
            "high_asset_score": HIGH_ASSET_SCORE,
            "high_profile_quality": HIGH_PROFILE_QUALITY,
            "low_signal_score": LOW_SIGNAL_SCORE,
            "low_asset_score": LOW_ASSET_SCORE,
            "strong_capability_score": STRONG_CAPABILITY_SCORE,
            "possible_capability_score": POSSIBLE_CAPABILITY_SCORE,
            "capability_explosion_total_count": EXPLOSION_TOTAL_CAPABILITY_COUNT,
            "capability_explosion_strong_count": EXPLOSION_STRONG_CAPABILITY_COUNT,
        },
        "buckets": buckets,
        "recommendations": recommendations,
    }


def list_profile_cards_by_audit_priority(
    limit: int,
    *,
    scope: str = "downloaded",
    force: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    selected_scope = _normalize_scope(scope)
    dataset = _load_audit_dataset(now or datetime.now(UTC))
    items = _filter_items_for_scope(dataset["items"], selected_scope)
    targets = [
        item
        for item in _sort_profile_targets(items)
        if item["card_id"] and (force or item["profile_status"] in PROFILE_TARGET_STATUSES)
    ]
    card_ids = [str(item["card_id"]) for item in targets[: max(0, limit)]]
    cards_by_id = _repository_cards_by_id(card_ids)
    return [cards_by_id[card_id] for card_id in card_ids if card_id in cards_by_id]


def _load_audit_dataset(now: datetime) -> dict[str, Any]:
    repos = _query_dicts("SELECT * FROM repositories ORDER BY COALESCE(stars, 0) DESC, repo_id")
    snapshots = _query_dicts("SELECT * FROM snapshots")
    cards = _query_dicts(
        """
        SELECT c.*, s.repo_id
        FROM repository_cards c
        JOIN snapshots s ON s.snapshot_id = c.snapshot_id
        """
    )
    assets = _query_dicts("SELECT * FROM assets")
    profile_runs = _latest_profile_runs_by_card()

    snapshots_by_repo = _group_by(snapshots, "repo_id")
    cards_by_snapshot = _group_by(cards, "snapshot_id")
    cards_by_repo = _group_by(cards, "repo_id")
    assets_by_repo = _group_by(assets, "repo_id")

    items: list[dict[str, Any]] = []
    for repo in repos:
        repo_id = str(repo["repo_id"])
        latest_snapshot = _latest_by_timestamp(snapshots_by_repo.get(repo_id, []), "indexed_at")
        latest_card = None
        if latest_snapshot is not None:
            latest_card = _latest_by_timestamp(
                cards_by_snapshot.get(str(latest_snapshot["snapshot_id"]), []),
                "created_at",
            )
        if latest_card is None:
            latest_card = _latest_by_timestamp(cards_by_repo.get(repo_id, []), "created_at")
        items.append(
            _audit_repo(
                repo=repo,
                latest_snapshot=latest_snapshot,
                latest_card=latest_card,
                assets=assets_by_repo.get(repo_id, []),
                profile_runs=profile_runs,
                now=now,
            )
        )

    return {
        "items": items,
        "snapshots": snapshots,
        "cards": cards,
        "assets": assets,
    }


def _audit_repo(
    *,
    repo: dict[str, Any],
    latest_snapshot: dict[str, Any] | None,
    latest_card: dict[str, Any] | None,
    assets: list[dict[str, Any]],
    profile_runs: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    profile = _load_profile(latest_card)
    profile_quality = _profile_quality(profile)
    profile_status = _profile_status(latest_card, profile, profile_runs)
    best_asset_score = max((_float_value(asset.get("reuse_score")) for asset in assets), default=0.0)
    avg_asset_score = (
        sum(_float_value(asset.get("reuse_score")) for asset in assets) / len(assets) if assets else 0.0
    )
    capability_tiers = _capability_tiers(assets)
    capability_counts = {tier: len(values) for tier, values in capability_tiers.items()}
    capability_counts["total"] = sum(capability_counts.values())
    capability_counts["actionable"] = capability_counts.get("strong", 0) + capability_counts.get(
        "possible", 0
    )
    quality_score = _quality_score(profile_quality, best_asset_score, len(assets))
    pushed_at = _parse_datetime(repo.get("pushed_at"))
    created_at = _parse_datetime(repo.get("repo_created_at"))
    days_since_push = (now - pushed_at).days if pushed_at else None
    repo_age_days = (now - created_at).days if created_at else None
    downloaded = _snapshot_downloaded(latest_snapshot, repo)

    reasons: list[str] = []
    buckets: list[str] = []

    stale_reasons = _stale_reasons(repo, days_since_push, repo_age_days)
    noisy_reasons = _noisy_reasons(repo, profile, capability_counts)
    low_signal_reasons = _low_signal_reasons(
        asset_count=len(assets),
        best_asset_score=best_asset_score,
        profile_quality=profile_quality,
        profile_status=profile_status,
        quality_score=quality_score,
    )

    if stale_reasons:
        buckets.append("stale")
        reasons.extend(stale_reasons)
    if noisy_reasons:
        buckets.append("noisy")
        reasons.extend(noisy_reasons)
    if profile_status == "unprofiled":
        buckets.append("unprofiled")
        reasons.append("no assessment-model profile is stored")
    elif profile_status == "failed_profile":
        buckets.append("failed_profile")
        reasons.append("latest assessment-model profile attempt failed")
    elif profile_status == "outdated_profile":
        buckets.append("outdated_profile")
        reasons.append("assessment-model profile schema is older than current")
    if low_signal_reasons:
        buckets.append("low_signal")
        reasons.extend(low_signal_reasons)

    high_quality = (
        len(assets) > 0
        and profile_status == "profiled"
        and profile_quality is not None
        and quality_score >= HIGH_QUALITY_SCORE
        and best_asset_score >= HIGH_ASSET_SCORE
        and profile_quality >= HIGH_PROFILE_QUALITY
        and "stale" not in buckets
        and "low_signal" not in buckets
        and not _has_hard_noise(noisy_reasons)
    )
    if high_quality:
        buckets.insert(0, "high_quality")
        reasons.insert(0, "strong repo with deterministic assets and a current model profile")

    discard_candidate = _is_discard_candidate(buckets, reasons)
    if discard_candidate:
        buckets.append("discard_candidates")

    return {
        "repo_id": str(repo["repo_id"]),
        "html_url": str(repo.get("html_url") or ""),
        "stars": _int_value(repo.get("stars")),
        "source_channel": str(repo.get("source_channel") or ""),
        "snapshot_id": str(latest_snapshot["snapshot_id"]) if latest_snapshot else None,
        "commit_sha": str(latest_snapshot["commit_sha"]) if latest_snapshot else None,
        "downloaded": downloaded,
        "card_id": str(latest_card["card_id"]) if latest_card else None,
        "profile_status": profile_status,
        "repository_type": str(profile.get("repository_type")) if profile else None,
        "profile_quality": profile_quality,
        "quality_score": quality_score,
        "asset_count": len(assets),
        "best_asset_score": round(best_asset_score, 4),
        "average_asset_score": round(avg_asset_score, 4),
        "capabilities": capability_tiers["strong"],
        "capability_tiers": capability_tiers,
        "capability_counts": capability_counts,
        "days_since_push": days_since_push,
        "repo_age_days": repo_age_days,
        "buckets": buckets,
        "recommended_action": _recommended_action(
            buckets,
            profile_status,
            _has_capability_explosion(capability_counts),
        ),
        "reasons": _dedupe(reasons),
    }


def _normalize_bucket(bucket: str | None) -> str | None:
    if bucket is None:
        return None
    normalized = bucket.strip().replace("-", "_")
    if normalized not in BUCKETS:
        allowed = ", ".join(BUCKETS)
        raise ValueError(f"Unknown audit bucket '{bucket}'. Allowed: {allowed}")
    return normalized


def _normalize_scope(scope: str) -> str:
    normalized = scope.strip().replace("-", "_")
    if normalized not in SCOPES:
        allowed = ", ".join(SCOPES)
        raise ValueError(f"Unknown audit scope '{scope}'. Allowed: {allowed}")
    return normalized


def _filter_items_for_scope(items: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
    if scope == "all":
        return items
    if scope == "downloaded":
        return [item for item in items if item["downloaded"]]
    return [item for item in items if _is_cataloged(item)]


def _is_cataloged(item: dict[str, Any]) -> bool:
    return bool(item.get("snapshot_id") or item.get("card_id") or item.get("asset_count", 0) > 0)


def _scoped_count(rows: list[dict[str, Any]], items: list[dict[str, Any]]) -> int:
    repo_ids = {str(item["repo_id"]) for item in items}
    return sum(1 for row in rows if str(row.get("repo_id")) in repo_ids)


def _query_dicts(query: str) -> list[dict[str, Any]]:
    conn = catalog_core.get_connection()
    rows = conn.execute(query).fetchall()
    columns = [str(column[0]) for column in conn.description]
    return [dict(zip(columns, row, strict=False)) for row in rows]


def _repository_cards_by_id(card_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not card_ids:
        return {}
    placeholders = ", ".join("?" for _ in card_ids)
    conn = catalog_core.get_connection()
    rows = conn.execute(
        f"""
        SELECT
            c.card_id,
            c.snapshot_id,
            c.card_version,
            c.package_manifests,
            c.tree_summary,
            c.readme_excerpt,
            c.stack_signals,
            c.deterministic_features,
            c.repository_profile,
            s.repo_id,
            s.commit_sha,
            r.html_url
        FROM repository_cards c
        JOIN snapshots s ON s.snapshot_id = c.snapshot_id
        JOIN repositories r ON r.repo_id = s.repo_id
        WHERE c.card_id IN ({placeholders})
        """,
        card_ids,
    ).fetchall()
    columns = [str(column[0]) for column in conn.description]
    cards: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = dict(zip(columns, row, strict=False))
        for key, default in (
            ("package_manifests", {}),
            ("tree_summary", {}),
            ("stack_signals", {}),
            ("deterministic_features", {}),
            ("repository_profile", None),
        ):
            data[key] = _json_load(data.get(key), default)
        cards[str(data["card_id"])] = data
    return cards


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        raw_value = row.get(key)
        if raw_value is None:
            continue
        grouped.setdefault(str(raw_value), []).append(row)
    return grouped


def _latest_by_timestamp(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda row: str(row.get(key) or ""))


def _latest_profile_runs_by_card() -> dict[str, dict[str, Any]]:
    runs = _query_dicts(
        """
        SELECT *
        FROM analysis_runs
        WHERE stage_name = 'profile'
        ORDER BY created_at
        """
    )
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        details = _json_load(run.get("details"), {})
        if not isinstance(details, dict):
            continue
        card_id = details.get("card_id")
        if card_id:
            latest[str(card_id)] = run
    return latest


def _load_profile(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if card is None:
        return None
    profile = _json_load(card.get("repository_profile"), None)
    return profile if isinstance(profile, dict) else None


def _profile_status(
    card: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    profile_runs: dict[str, dict[str, Any]],
) -> str:
    if card is None:
        return "missing_card"
    if profile:
        if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
            return "outdated_profile"
        if _profile_quality(profile) is None:
            return "invalid_profile"
        return "profiled"
    latest_run = profile_runs.get(str(card["card_id"]))
    if latest_run and latest_run.get("status") == "failed":
        return "failed_profile"
    return "unprofiled"


def _profile_quality(profile: dict[str, Any] | None) -> float | None:
    if not profile:
        return None
    scores = [
        _float_value(profile.get("likely_usefulness")),
        _float_value(profile.get("extractability")),
        _float_value(profile.get("maintenance_quality")),
    ]
    if all(score <= 0 for score in scores) and not profile.get("concerns"):
        return None
    return round(sum(scores) / len(scores), 4)


def _quality_score(profile_quality: float | None, best_asset_score: float, asset_count: int) -> float:
    signals: list[float] = []
    if asset_count > 0:
        signals.append(best_asset_score)
    if profile_quality is not None:
        signals.append(profile_quality)
    if not signals:
        return 0.0
    return round(sum(signals) / len(signals), 4)


def _stale_reasons(
    repo: dict[str, Any],
    days_since_push: int | None,
    repo_age_days: int | None,
) -> list[str]:
    reasons: list[str] = []
    if not repo.get("pushed_at"):
        reasons.append("missing pushed_at freshness metadata")
    elif days_since_push is not None and days_since_push > MAX_STALE_DAYS:
        reasons.append(f"last push is {days_since_push} days old")
    if not repo.get("repo_created_at"):
        reasons.append("missing repo_created_at metadata")
    elif repo_age_days is not None and repo_age_days < 0:
        reasons.append("repo_created_at is in the future")
    return reasons


def _noisy_reasons(
    repo: dict[str, Any],
    profile: dict[str, Any] | None,
    capability_counts: dict[str, int],
) -> list[str]:
    reasons: list[str] = []
    if bool(repo.get("is_archived")):
        reasons.append("repository is archived")
    if bool(repo.get("is_mirror")):
        reasons.append("repository is a mirror")
    if bool(repo.get("is_fork")):
        reasons.append("repository is a fork")
    if bool(repo.get("is_template")):
        reasons.append("repository is a template")
    repo_size_kb = _int_value(repo.get("repo_size_kb"))
    if repo_size_kb is not None and repo_size_kb > MAX_REPOSITORY_SIZE_KB:
        reasons.append(f"repository size {repo_size_kb} KB exceeds limit {MAX_REPOSITORY_SIZE_KB} KB")
    if profile:
        concerns = [str(concern).lower() for concern in profile.get("concerns", [])]
        if any(term in concern for concern in concerns for term in NOISY_CONCERN_TERMS):
            reasons.append("model profile concerns suggest noise or tight coupling")
    if _has_capability_explosion(capability_counts):
        reasons.append(
            "capability explosion: "
            f"{capability_counts['actionable']} actionable labels, "
            f"{capability_counts['strong']} strong"
        )
    return reasons


def _low_signal_reasons(
    *,
    asset_count: int,
    best_asset_score: float,
    profile_quality: float | None,
    profile_status: str,
    quality_score: float,
) -> list[str]:
    reasons: list[str] = []
    if asset_count <= 0:
        reasons.append("no deterministic reuse assets")
    elif best_asset_score < LOW_ASSET_SCORE:
        reasons.append(f"best asset score {best_asset_score:.2f} is low")
    if profile_status in {"missing_card", "invalid_profile"}:
        reasons.append(f"profile status is {profile_status}")
    if profile_quality is not None and profile_quality < LOW_SIGNAL_SCORE:
        reasons.append(f"model profile quality {profile_quality:.2f} is low")
    if quality_score < LOW_SIGNAL_SCORE and (asset_count <= 0 or profile_quality is not None):
        reasons.append(f"combined quality score {quality_score:.2f} is low")
    return reasons


def _capability_tiers(assets: list[dict[str, Any]]) -> dict[str, list[str]]:
    tiers: dict[str, list[str]] = {"strong": [], "possible": [], "weak_noisy": []}
    for asset in assets:
        capability = str(asset.get("capability") or "")
        if not capability:
            continue
        tiers[_capability_tier(asset)].append(capability)
    return {tier: sorted(set(values)) for tier, values in tiers.items()}


def _capability_tier(asset: dict[str, Any]) -> str:
    capability = str(asset.get("capability") or "")
    reuse_score = _float_value(asset.get("reuse_score"))
    entry_paths = _asset_entry_paths(asset)
    evidence_paths = _asset_evidence_paths(asset)
    external_dependencies = _asset_external_dependencies(asset)
    synthesis = _asset_synthesis(asset)
    noise_penalty = _float_value(synthesis.get("noise_penalty"))
    label_signal = _capability_label_signal_score(
        capability,
        entry_paths + evidence_paths,
        external_dependencies,
        synthesis,
    )
    if (
        reuse_score >= STRONG_CAPABILITY_SCORE
        and evidence_paths
        and noise_penalty < STRONG_CAPABILITY_MAX_NOISE
        and label_signal >= STRONG_LABEL_SIGNAL
    ):
        return "strong"
    if (
        reuse_score >= POSSIBLE_CAPABILITY_SCORE
        and evidence_paths
        and noise_penalty < POSSIBLE_CAPABILITY_MAX_NOISE
        and label_signal >= MIN_LABEL_SIGNAL
    ):
        return "possible"
    return "weak_noisy"


def _asset_entry_paths(asset: dict[str, Any]) -> list[str]:
    loaded = _json_load(asset.get("entry_paths"), [])
    if not isinstance(loaded, list):
        return []
    return [str(path) for path in loaded if str(path).strip()]


def _asset_evidence_paths(asset: dict[str, Any]) -> list[str]:
    loaded = _json_load(asset.get("evidence_paths"), [])
    if not isinstance(loaded, list):
        return []
    return [str(path) for path in loaded if str(path).strip()]


def _asset_external_dependencies(asset: dict[str, Any]) -> list[str]:
    loaded = _json_load(asset.get("external_dependencies"), [])
    if not isinstance(loaded, list):
        return []
    return [str(dependency) for dependency in loaded if str(dependency).strip()]


def _asset_synthesis(asset: dict[str, Any]) -> dict[str, Any]:
    loaded = _json_load(asset.get("synthesis"), {})
    return loaded if isinstance(loaded, dict) else {}


def _has_capability_explosion(capability_counts: dict[str, int]) -> bool:
    return (
        capability_counts.get("actionable", 0) >= EXPLOSION_TOTAL_CAPABILITY_COUNT
        or capability_counts.get("strong", 0) >= EXPLOSION_STRONG_CAPABILITY_COUNT
    )


def _has_hard_noise(noisy_reasons: list[str]) -> bool:
    return any(not reason.startswith("capability explosion:") for reason in noisy_reasons)


def _is_discard_candidate(buckets: list[str], reasons: list[str]) -> bool:
    hard_noise = any(
        reason
        in {
            "repository is archived",
            "repository is a mirror",
            "repository is a fork",
            "repository is a template",
        }
        for reason in reasons
    )
    if "low_signal" in buckets and ("no deterministic reuse assets" in reasons or hard_noise):
        return True
    if "noisy" in buckets and hard_noise:
        return True
    return "stale" in buckets and "low_signal" in buckets


def _recommended_action(
    buckets: list[str],
    profile_status: str,
    capability_explosion: bool,
) -> str:
    if "high_quality" in buckets:
        return "keep_review_labels" if capability_explosion else "keep"
    if "discard_candidates" in buckets:
        return "review_discard"
    if profile_status == "failed_profile":
        return "retry_profile"
    if profile_status in {"unprofiled", "outdated_profile"}:
        return "profile"
    return "review"


def _sort_bucket(items: list[dict[str, Any]], bucket: str) -> list[dict[str, Any]]:
    bucket_items = [item for item in items if bucket in item["buckets"]]
    if bucket == "high_quality":
        return sorted(
            bucket_items,
            key=lambda item: (float(item["quality_score"]), int(item["stars"] or 0)),
            reverse=True,
        )
    if bucket == "stale":
        return sorted(
            bucket_items,
            key=lambda item: (int(item["days_since_push"] or 999999), -float(item["quality_score"])),
            reverse=True,
        )
    if bucket == "discard_candidates":
        return sorted(bucket_items, key=lambda item: (float(item["quality_score"]), str(item["repo_id"])))
    return sorted(
        bucket_items,
        key=lambda item: (int(item["stars"] or 0), float(item["quality_score"])),
        reverse=True,
    )


def _sort_recommendation(items: list[dict[str, Any]], recommendation: str) -> list[dict[str, Any]]:
    if recommendation == "keep":
        keep_items = [item for item in items if item["recommended_action"] in {"keep", "keep_review_labels"}]
        return sorted(
            keep_items,
            key=lambda item: (float(item["quality_score"]), int(item["stars"] or 0)),
            reverse=True,
        )
    if recommendation == "reprofile":
        return _sort_profile_targets(
            [item for item in items if item["card_id"] and item["profile_status"] in PROFILE_TARGET_STATUSES]
        )
    if recommendation == "discard":
        return _sort_bucket(items, "discard_candidates")
    if recommendation == "review":
        assigned = {"keep", "keep_review_labels", "profile", "retry_profile", "review_discard"}
        review_items = [item for item in items if item["recommended_action"] not in assigned]
        return sorted(
            review_items,
            key=lambda item: (float(item["quality_score"]), int(item["stars"] or 0)),
            reverse=True,
        )
    return []


def _sort_profile_targets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            float(item["quality_score"]),
            int(item["stars"] or 0),
            int(item["asset_count"]),
        ),
        reverse=True,
    )


def _summarize_item(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "repo_id",
        "html_url",
        "stars",
        "downloaded",
        "snapshot_id",
        "card_id",
        "profile_status",
        "repository_type",
        "quality_score",
        "profile_quality",
        "asset_count",
        "best_asset_score",
        "capabilities",
        "capability_tiers",
        "capability_counts",
        "days_since_push",
        "recommended_action",
        "reasons",
    )
    return {key: item[key] for key in keys}


def _json_load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _snapshot_downloaded(snapshot: dict[str, Any] | None, repo: dict[str, Any]) -> bool:
    if snapshot is None:
        return False
    raw_path = snapshot.get("snapshot_path")
    if raw_path and Path(str(raw_path)).exists():
        return True
    owner = repo.get("owner")
    name = repo.get("name")
    commit_sha = snapshot.get("commit_sha")
    if not owner or not name or not commit_sha:
        return False
    return catalog_core.snapshot_path(str(owner), str(name), str(commit_sha)).exists()


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
