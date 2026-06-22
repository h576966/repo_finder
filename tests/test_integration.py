import pytest

from source_scout import pattern_extractor, repo_inspector
from source_scout.github_client import get_client


@pytest.mark.integration
async def test_search_repos_integration(requires_github_token: str) -> None:
    client = get_client()
    repos = await client.search_repos(
        "language:python topic:http stars:>=100",
        per_page=30,
    )
    assert len(repos) > 0
    for r in repos:
        assert "full_name" in r


@pytest.mark.integration
async def test_inspect_known_repo_integration(requires_github_token: str) -> None:
    result = await repo_inspector.inspect_repo("psf", "requests")
    assert result.owner == "psf"
    assert result.repo == "requests"
    assert result.stars > 0
    assert result.verdict in ("useful", "maybe", "skip")


@pytest.mark.integration
async def test_compare_manual_integration(requires_github_token: str) -> None:
    r1 = await repo_inspector.inspect_repo("psf", "requests")
    r2 = await repo_inspector.inspect_repo("encode", "httpx")
    assert r1.stars > 0
    assert r2.stars > 0
    assert r1.verdict in ("useful", "maybe", "skip")
    assert r2.verdict in ("useful", "maybe", "skip")


@pytest.mark.integration
async def test_extract_patterns_integration(requires_github_token: str) -> None:
    result = await pattern_extractor.extract_patterns("tiangolo", "fastapi")
    assert result.owner == "tiangolo"
    assert result.repo == "fastapi"
    assert result.verdict in ("useful", "maybe", "skip")
    assert isinstance(result.patterns, list)


@pytest.mark.integration
async def test_inspect_nonexistent_repo_integration(requires_github_token: str) -> None:
    import httpx

    with pytest.raises(httpx.HTTPStatusError):
        await repo_inspector.inspect_repo(
            "nonexistent-owner-12345", "nonexistent-repo-12345"
        )
