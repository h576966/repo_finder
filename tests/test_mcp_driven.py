import json
import os

import pytest

from repo_finder import pattern_extractor, repo_inspector
from repo_finder.server import _build_search_query

_CORPUS_PATH = os.path.join(os.path.dirname(__file__), "corpus", "ground_truth.json")


def _load_corpus() -> list[dict]:
    with open(_CORPUS_PATH) as f:
        data = json.load(f)
    return data["corpus"]


@pytest.fixture(scope="module")
def corpus() -> list[dict]:
    return _load_corpus()


@pytest.mark.integration
class TestMetadataAccuracy:
    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_stars_meet_minimum(self, entry: dict, requires_github_token: str) -> None:
        result = await repo_inspector.inspect_repo(entry["owner"], entry["repo"])
        assert result.stars >= entry["expected"]["min_stars"], (
            f"{entry['owner']}/{entry['repo']}: expected >= {entry['expected']['min_stars']} stars, got {result.stars}"  # noqa: E501
        )



    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_license_detected(self, entry: dict, requires_github_token: str) -> None:
        result = await repo_inspector.inspect_repo(entry["owner"], entry["repo"])
        expected_license = entry["expected"]["license_spdx"]
        assert result.license_name == expected_license, (
            f"{entry['owner']}/{entry['repo']}: expected license {expected_license}, got {result.license_name}"  # noqa: E501
        )



    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_archived_status(self, entry: dict, requires_github_token: str) -> None:
        result = await repo_inspector.inspect_repo(entry["owner"], entry["repo"])
        assert result.archived == entry["expected"]["archived"]

    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_language_matches(self, entry: dict, requires_github_token: str) -> None:
        result = await repo_inspector.inspect_repo(entry["owner"], entry["repo"])
        assert result.language and result.language.lower() == entry["expected"]["language"].lower(), (  # noqa: E501
            f"{entry['owner']}/{entry['repo']}: expected language {entry['expected']['language']}, got {result.language}"  # noqa: E501
        )

    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_verdict_valid(self, entry: dict, requires_github_token: str) -> None:
        result = await repo_inspector.inspect_repo(entry["owner"], entry["repo"])
        assert result.verdict in entry["expected"]["verdict_possible"], (  # noqa: E501
            f"{entry['owner']}/{entry['repo']}: verdict {result.verdict} not in {entry['expected']['verdict_possible']}"  # noqa: E501
        )

    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_verdict_not_skip(self, entry: dict, requires_github_token: str) -> None:
        result = await repo_inspector.inspect_repo(entry["owner"], entry["repo"])
        skipped = entry["expected"].get("verdict_not", [])
        assert result.verdict not in skipped, (
            f"{entry['owner']}/{entry['repo']}: verdict {result.verdict} should not be in {skipped}"
        )


@pytest.mark.integration
class TestActivityScoring:
    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_activity_known(self, entry: dict, requires_github_token: str) -> None:
        result = await repo_inspector.inspect_repo(entry["owner"], entry["repo"])
        quality = result.quality
        assert quality.signals.get("activity") in ("active", "moderate", "stale", "unknown")

    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_activity_not_unknown(self, entry: dict, requires_github_token: str) -> None:
        result = await repo_inspector.inspect_repo(entry["owner"], entry["repo"])
        quality = result.quality
        if not entry["expected"]["archived"]:
            assert quality.signals.get("activity") != "unknown", (
                f"{entry['owner']}/{entry['repo']}: active repo has unknown activity"
            )


@pytest.mark.integration
class TestPatternExtraction:
    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_extract_patterns_returns(self, entry: dict, requires_github_token: str) -> None:
        result = await pattern_extractor.extract_patterns(entry["owner"], entry["repo"])
        assert result.owner == entry["owner"]
        assert result.repo == entry["repo"]
        assert isinstance(result.patterns, list)
        assert len(result.file_tree) > 0

    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_extract_patterns_readme_sections(self, entry: dict, requires_github_token: str) -> None:
        result = await pattern_extractor.extract_patterns(entry["owner"], entry["repo"])
        if entry["expected"]["has_readme"]:
            assert len(result.readme_sections) > 0, (
                f"{entry['owner']}/{entry['repo']}: expected README sections but got none"
            )

    @pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['owner']}/{e['repo']}")
    async def test_extract_patterns_with_focus(self, entry: dict, requires_github_token: str) -> None:
        result = await pattern_extractor.extract_patterns(entry["owner"], entry["repo"], focus="api")
        assert result.focus == "api"
        assert isinstance(result.patterns, list)


@pytest.mark.integration
class TestSearchRanking:
    @pytest.mark.parametrize("query,language,min_stars,expected_repo", [
        ("Python HTTP client", "Python", None, "psf/requests"),
        ("Python web framework", "Python", None, "pallets/flask"),
        ("HTTP client Python", "Python", None, "encode/httpx"),
    ])
    async def test_search_returns_results(  # noqa: E501
        self, query: str, language: str, min_stars: int | None, expected_repo: str, requires_github_token: str
    ) -> None:
        from repo_finder.github_client import get_client

        client = get_client()
        search_query = _build_search_query(query, language, min_stars, None, None)
        repos = await client.search_repos(search_query, per_page=10)

        assert len(repos) > 0, f"Search for '{query}' returned 0 results"
        full_names = [r.get("full_name", "") for r in repos]
        assert expected_repo in full_names, (
            f"Expected {expected_repo} in search results for '{query}', got: {full_names[:5]}"
        )


@pytest.mark.integration
@pytest.mark.xfail(
    reason="deep_inspect clones large repos (FastAPI 100MB+) — need smaller corpus repos. "
           "Unit tests in test_deep_inspect.py cover the logic."
)
class TestDeepInspect:
    @pytest.mark.parametrize("owner,repo", [
        ("fastapi", "fastapi"),
        ("pallets", "flask"),
    ])
    async def test_deep_inspect_returns_tree(self, owner: str, repo: str, requires_github_token: str) -> None:
        result = await pattern_extractor.extract_patterns_deep(owner, repo)
        assert result.owner == owner
        assert result.repo == repo
        assert len(result.tree_visual) > 0

    @pytest.mark.parametrize("owner,repo,expected_framework", [
        ("fastapi", "fastapi", "fastapi"),
        pytest.param(
            "pallets", "flask", "flask",
            marks=pytest.mark.xfail(
                reason="detect_framework doesn't check src/ subdirs for app.py/wsgi.py"
            ),
        ),
    ])
    async def test_deep_inspect_framework_detected(  # noqa: E501
        self, owner: str, repo: str, expected_framework: str, requires_github_token: str
    ) -> None:
        result = await pattern_extractor.extract_patterns_deep(owner, repo)
        assert result.framework == expected_framework

    @pytest.mark.parametrize("owner,repo", [
        ("fastapi", "fastapi"),
        ("pallets", "flask"),
    ])
    async def test_deep_inspect_has_file_snippets(  # noqa: E501
        self, owner: str, repo: str, requires_github_token: str
    ) -> None:
        result = await pattern_extractor.extract_patterns_deep(owner, repo)
        assert len(result.full_file_snippets) >= 1
