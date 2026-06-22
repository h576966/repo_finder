import hashlib
import math
import os
from datetime import UTC, datetime
from typing import Any

from .models import RepoScore, RepoSummary


def _hash_query(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()[:16]


def _term_overlap(text: str | None, query: str) -> float:
    if not text:
        return 0.0
    text_lower = text.lower()
    query_lower = query.lower()
    query_terms = [t for t in query_lower.split() if len(t) > 2]
    if not query_terms:
        return 0.5
    matches = sum(1 for t in query_terms if t in text_lower)
    return matches / len(query_terms)


def score_relevance(repo: dict[str, Any], query: str) -> float:
    desc = repo.get("description") or ""
    topics = repo.get("topics") or []
    name = repo.get("full_name") or ""
    language = repo.get("language") or ""

    score = 0.0

    desc_overlap = _term_overlap(desc, query)
    score += desc_overlap * 0.5

    name_overlap = _term_overlap(name, query)
    score += name_overlap * 0.2

    topics_text = " ".join(topics) if topics else ""
    topics_overlap = _term_overlap(topics_text, query)
    score += topics_overlap * 0.2

    query_lower = query.lower()
    if language and language.lower() in query_lower:
        score += 0.1

    return min(score, 1.0)


def score_activity(repo: dict[str, Any]) -> float:
    if repo.get("archived", False):
        return 0.0

    pushed_at = repo.get("pushed_at")
    if not pushed_at:
        return 0.1

    try:
        pushed_dt = datetime.fromisoformat(pushed_at)
    except (ValueError, AttributeError):
        return 0.1

    now = datetime.now(UTC)
    age_days = (now - pushed_dt).days

    if age_days <= 30:
        recency = 1.0
    elif age_days <= 180:
        recency = 0.7
    elif age_days <= 365:
        recency = 0.4
    else:
        recency = 0.1

    return recency


def score_popularity(repo: dict[str, Any], max_stars: int = 1) -> float:
    stars = repo.get("stargazers_count", 0) or 0
    forks = repo.get("forks_count", 0) or 0

    capped_stars = min(stars, 5000)
    effective_max = max(max_stars, 1)

    if effective_max <= 1 or capped_stars <= 0:
        stars_score = 0.0
    else:
        stars_score = math.log(capped_stars + 1) / math.log(effective_max + 1)

    if capped_stars <= 0:
        forks_score = 0.0
    else:
        forks_score = math.log(forks + 1) / math.log(max(capped_stars, 1) + 1)

    return stars_score * 0.7 + forks_score * 0.3


def score_structure(repo: dict[str, Any]) -> float:
    score = 0.0

    description = repo.get("description")
    if description and len(description) > 0:
        score += 0.25

    topics = repo.get("topics") or []
    if topics:
        score += 0.25

    has_license = repo.get("license") is not None
    if has_license:
        score += 0.25

    if repo.get("has_wiki", False):
        score += 0.1

    if repo.get("homepage"):
        score += 0.15

    return min(score, 1.0)


_PERMISSIVE_LICENSES = {
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause",
    "unlicense", "mpl-2.0", "isc",
}


def score_license(repo: dict[str, Any]) -> float:
    lic = repo.get("license")
    if not lic:
        return 0.2

    spdx = (lic.get("spdx_id") or "").lower()
    if not spdx:
        return 0.5

    if spdx in _PERMISSIVE_LICENSES:
        return 1.0
    if spdx.startswith("gpl") or spdx.startswith("agpl"):
        return 0.8
    return 0.6


def _get_weight(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


RELEVANCE_WEIGHT = _get_weight("RANKER_WEIGHT_RELEVANCE", 0.40)
ACTIVITY_WEIGHT = _get_weight("RANKER_WEIGHT_ACTIVITY", 0.20)
POPULARITY_WEIGHT = _get_weight("RANKER_WEIGHT_POPULARITY", 0.15)
STRUCTURE_WEIGHT = _get_weight("RANKER_WEIGHT_STRUCTURE", 0.15)
LICENSE_WEIGHT = _get_weight("RANKER_WEIGHT_LICENSE", 0.10)


def score_repo(repo: dict[str, Any], query: str, max_stars: int = 1) -> RepoScore:
    rel = score_relevance(repo, query)
    act = score_activity(repo)
    pop = score_popularity(repo, max_stars)
    struct = score_structure(repo)
    lic = score_license(repo)

    total = (
        rel * RELEVANCE_WEIGHT
        + act * ACTIVITY_WEIGHT
        + pop * POPULARITY_WEIGHT
        + struct * STRUCTURE_WEIGHT
        + lic * LICENSE_WEIGHT
    )

    # Verdict domain: search-space metadata ranking (stars, activity, license, etc.)
    if total >= 0.70:
        verdict = "useful"
    elif total >= 0.40:
        verdict = "maybe"
    else:
        verdict = "skip"

    return RepoScore(
        total=round(total, 4),
        relevance=round(rel, 4),
        activity=round(act, 4),
        popularity=round(pop, 4),
        structure=round(struct, 4),
        license=round(lic, 4),
        verdict=verdict,
    )


def _identify_risks(repo: dict[str, Any], score: RepoScore) -> list[str]:
    risks: list[str] = []
    if repo.get("archived", False):
        risks.append("archived")
    pushed_at = repo.get("pushed_at")
    if pushed_at:
        try:
            pushed_dt = datetime.fromisoformat(pushed_at)
            if (datetime.now(UTC) - pushed_dt).days > 365:
                risks.append("no recent activity (>1 year)")
        except (ValueError, AttributeError):
            pass
    if not repo.get("license"):
        risks.append("no license")
    if score.total < 0.40:
        risks.append("low quality signals")
    if (repo.get("open_issues_count") or 0) > 500:
        risks.append("high open issues")
    return risks


def _compute_max_stars(repos: list[dict[str, Any]]) -> int:
    ms = max((r.get("stargazers_count", 0) or 0) for r in repos)
    return max(ms, 1)


def rank_repos(
    repos: list[dict[str, Any]],
    query: str,
    top_n: int = 10,
) -> list[dict[str, Any]]:
    if not repos:
        return []

    max_stars = _compute_max_stars(repos)

    scored: list[tuple[dict[str, Any], RepoScore]] = []
    for repo in repos:
        sc = score_repo(repo, query, max_stars)
        scored.append((repo, sc))

    scored.sort(key=lambda x: x[1].total, reverse=True)
    return [r for r, _ in scored[:top_n]]


def build_repo_summaries(
    repos: list[dict[str, Any]],
    query: str,
    max_stars: int | None = None,
) -> list[RepoSummary]:
    if not repos:
        return []

    if max_stars is None:
        max_stars = _compute_max_stars(repos)

    summaries: list[RepoSummary] = []
    for repo in repos:
        sc = score_repo(repo, query, max_stars)
        risks = _identify_risks(repo, sc)
        summaries.append(
            RepoSummary(
                full_name=repo.get("full_name", ""),
                html_url=repo.get("html_url", ""),
                description=repo.get("description"),
                language=repo.get("language"),
                stars=repo.get("stargazers_count", 0) or 0,
                last_push=repo.get("pushed_at", ""),
                score=sc.total,
                verdict=sc.verdict,
                risks=risks,
            )
        )

    summaries.sort(key=lambda s: s.score, reverse=True)
    return summaries
