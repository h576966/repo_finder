
import pytest

from source_scout.github_client import GitHubClient


@pytest.fixture
def mock_client():
    client = GitHubClient()
    return client


@pytest.fixture
def sample_metadata():
    return {
        "full_name": "testowner/testrepo",
        "html_url": "https://github.com/testowner/testrepo",
        "description": "A test repository",
        "language": "Python",
        "stargazers_count": 150,
        "forks_count": 30,
        "open_issues_count": 5,
        "pushed_at": "2026-04-20T12:00:00Z",
        "created_at": "2024-01-01T00:00:00Z",
        "archived": False,
        "license": {"spdx_id": "MIT", "name": "MIT License"},
        "topics": ["testing", "python"],
        "default_branch": "main",
    }


@pytest.fixture
def sample_search_response():
    return {
        "total_count": 2,
        "incomplete_results": False,
        "items": [
            {
                "full_name": "testowner/repo1",
                "html_url": "https://github.com/testowner/repo1",
                "description": "Python testing library",
                "language": "Python",
                "stargazers_count": 500,
                "forks_count": 50,
                "open_issues_count": 10,
                "pushed_at": "2026-04-25T12:00:00Z",
                "created_at": "2025-01-01T00:00:00Z",
                "archived": False,
                "license": {"spdx_id": "MIT"},
                "topics": ["testing", "python"],
            },
            {
                "full_name": "testowner/repo2",
                "html_url": "https://github.com/testowner/repo2",
                "description": "Old abandoned project",
                "language": "Python",
                "stargazers_count": 10,
                "forks_count": 2,
                "open_issues_count": 0,
                "pushed_at": "2023-01-01T12:00:00Z",
                "created_at": "2022-01-01T00:00:00Z",
                "archived": True,
                "license": None,
                "topics": [],
            },
        ],
    }
