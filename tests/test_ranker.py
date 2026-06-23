import pytest

from source_scout.ranker import (
    build_repo_summaries,
    rank_repos,
    score_activity,
    score_license,
    score_popularity,
    score_relevance,
    score_repo,
    score_structure,
)

pytestmark = pytest.mark.legacy


def test_score_relevance_exact_match():
    repo = {"description": "FastAPI web framework for building APIs", "topics": ["api", "web", "fastapi"], "full_name": "tiangolo/fastapi", "language": "Python"}  # noqa: E501
    score = score_relevance(repo, "fastapi web framework python")
    assert score > 0.3


def test_score_relevance_no_match():
    repo = {"description": "Machine learning toolkit", "topics": ["ml", "ai"], "full_name": "ml/mltool", "language": "Python"}  # noqa: E501
    score = score_relevance(repo, "fastapi web framework")
    assert score < 0.3


def test_score_activity_archived():
    repo = {"pushed_at": "2026-04-25T12:00:00Z", "archived": True}
    assert score_activity(repo) == 0.0


def test_score_activity_recent():
    repo = {"pushed_at": "2026-04-25T12:00:00Z", "archived": False}
    assert score_activity(repo) > 0.5


def test_score_activity_old():
    repo = {"pushed_at": "2023-01-01T12:00:00Z", "archived": False}
    assert score_activity(repo) < 0.3


def test_score_popularity_high():
    repo = {"stargazers_count": 5000, "forks_count": 1000}
    score = score_popularity(repo, max_stars=5000)
    assert score > 0.5


def test_score_popularity_low():
    repo = {"stargazers_count": 0, "forks_count": 0}
    score = score_popularity(repo, max_stars=5000)
    assert score == 0.0


def test_score_structure_good():
    repo = {"description": "A well-documented project", "topics": ["python", "api"], "license": {"spdx_id": "MIT"}, "has_wiki": True, "homepage": "https://example.com"}  # noqa: E501
    assert score_structure(repo) > 0.5


def test_score_structure_bare():
    repo = {"description": "", "topics": [], "license": None, "has_wiki": False, "homepage": None}
    assert score_structure(repo) < 0.2


def test_score_license_mit():
    repo = {"license": {"spdx_id": "MIT"}}
    assert score_license(repo) == 1.0


def test_score_license_gpl():
    repo = {"license": {"spdx_id": "GPL-3.0"}}
    assert score_license(repo) == 0.8


def test_score_license_none():
    repo = {"license": None}
    assert score_license(repo) == 0.2


def test_score_repo_verdict_useful():
    repo = {
        "description": "FastAPI backend with auth and SQLite",
        "language": "Python",
        "stargazers_count": 500,
        "forks_count": 100,
        "pushed_at": "2026-04-20T12:00:00Z",
        "archived": False,
        "license": {"spdx_id": "MIT"},
        "topics": ["fastapi", "auth", "api"],
        "full_name": "test/awesome-api",
        "has_wiki": True,
        "homepage": "https://example.com",
    }
    score = score_repo(repo, "fastapi backend auth sqlite", max_stars=500)
    assert score.total > 0.7
    assert score.verdict == "useful"


def test_score_repo_verdict_skip():
    repo = {
        "description": "",
        "language": "Python",
        "stargazers_count": 2,
        "forks_count": 0,
        "pushed_at": "2023-01-01T12:00:00Z",
        "archived": True,
        "license": None,
        "topics": [],
        "full_name": "dead/repo",
        "has_wiki": False,
        "homepage": None,
    }
    score = score_repo(repo, "fastapi backend auth sqlite", max_stars=5000)
    assert score.total < 0.4
    assert score.verdict == "skip"


def test_rank_repos_sorting():
    repos = [
        {"full_name": "repo/a", "stargazers_count": 100, "forks_count": 10, "pushed_at": "2026-04-20T12:00:00Z", "archived": False, "license": {"spdx_id": "MIT"}, "description": "FastAPI backend with auth", "language": "Python", "topics": ["fastapi"], "has_wiki": True, "homepage": ""},  # noqa: E501
        {"full_name": "repo/b", "stargazers_count": 5, "forks_count": 0, "pushed_at": "2023-01-01T12:00:00Z", "archived": True, "license": None, "description": "", "language": "Python", "topics": [], "has_wiki": False, "homepage": None},  # noqa: E501
    ]
    ranked = rank_repos(repos, "fastapi backend auth", top_n=10)
    assert ranked[0]["full_name"] == "repo/a"
    assert len(ranked) == 2


def test_build_repo_summaries():
    repos = [
        {"full_name": "test/repo1", "html_url": "https://github.com/test/repo1", "description": "FastAPI backend", "language": "Python", "stargazers_count": 500, "forks_count": 100, "pushed_at": "2026-04-20T12:00:00Z", "archived": False, "license": {"spdx_id": "MIT"}, "topics": ["api"], "has_wiki": True, "homepage": ""},  # noqa: E501
    ]
    summaries = build_repo_summaries(repos, "fastapi backend")
    assert len(summaries) == 1
    assert summaries[0].full_name == "test/repo1"
    assert summaries[0].verdict in ("useful", "maybe", "skip")
