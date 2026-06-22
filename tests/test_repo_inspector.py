
from source_scout.models import RepoStructure
from source_scout.repo_inspector import (
    _determine_verdict,
    _evaluate_activity,
    _extract_key_files,
)
from source_scout.urls import parse_owner_repo


def testparse_owner_repo_slug():
    assert parse_owner_repo("owner/repo") == ("owner", "repo")


def testparse_owner_repo_url():
    assert parse_owner_repo("https://github.com/owner/repo") == ("owner", "repo")


def testparse_owner_repo_url_with_trailing_slash():
    assert parse_owner_repo("https://github.com/owner/repo/") == ("owner", "repo")


def testparse_owner_repo_invalid():
    assert parse_owner_repo("not-a-repo") is None
    assert parse_owner_repo("") is None
    assert parse_owner_repo("owner/repo/extra") is None


def test_evaluate_activity_active():
    assert _evaluate_activity("2026-04-25T12:00:00Z") == "active"


def test_evaluate_activity_stale():
    assert _evaluate_activity("2023-01-01T12:00:00Z") == "stale"


def test_evaluate_activity_unknown():
    assert _evaluate_activity(None) == "unknown"
    assert _evaluate_activity("not-a-date") == "unknown"


def test_extract_key_files_python_project():
    structure = RepoStructure(
        dirs=[".github", "src", "tests"],
        files=["pyproject.toml", "README.md", "main.py", "Dockerfile", ".env.example"],
    )
    key_files = _extract_key_files(structure)
    assert "pyproject.toml" in key_files
    assert "Dockerfile" in key_files
    assert "main.py" in key_files


def test_determine_verdict_archived():
    metadata = {"archived": True}
    verdict, reasoning = _determine_verdict(metadata, None)
    assert verdict == "skip"
    assert "archived" in reasoning.lower()


def test_determine_verdict_high_quality():
    from source_scout.models import QualityReport
    quality = QualityReport(signals={"readme": "comprehensive", "license": "MIT", "ci": "present"}, score=0.8)
    metadata = {"archived": False}
    verdict, reasoning = _determine_verdict(metadata, quality)
    assert verdict == "useful"
