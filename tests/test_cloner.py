from unittest.mock import MagicMock, patch

import pytest

from source_scout.cloner import (
    _clone_path,
    cleanup_clone,
    clone_repo,
    format_tree,
    get_directory_tree,
)
from source_scout.urls import normalize_url


def test_sanitize_url_removes_git_suffix():
    assert normalize_url("https://github.com/owner/repo.git") == "github.com/owner/repo"


def test_sanitize_url_removes_https_prefix():
    assert normalize_url("https://github.com/owner/repo") == "github.com/owner/repo"


def test_sanitize_url_trailing_slash():
    assert normalize_url("https://github.com/owner/repo/") == "github.com/owner/repo"


def test_sanitize_url_no_change():
    assert normalize_url("github.com/owner/repo") == "github.com/owner/repo"


def test_clone_path_deterministic():
    url = "https://github.com/owner/repo"
    path1 = _clone_path(url)
    path2 = _clone_path(url)
    assert path1 == path2
    assert "source_scout_" in path1


def test_clone_path_different_urls():
    p1 = _clone_path("https://github.com/a/b")
    p2 = _clone_path("https://github.com/c/d")
    assert p1 != p2


def test_cleanup_clone(tmp_path):
    d = tmp_path / "test_repo"
    d.mkdir()
    (d / "file.txt").write_text("test")
    cleanup_clone(str(d))
    assert not d.exists()


def test_get_directory_tree(tmp_path):
    d = tmp_path / "test_tree"
    d.mkdir()
    (d / "README.md").write_text("readme")
    (d / "src").mkdir()
    (d / "src" / "main.py").write_text("print('hello')")
    (d / "tests").mkdir()
    (d / "tests" / "test_main.py").write_text("def test(): pass")

    tree = get_directory_tree(str(d))
    assert "README.md" in tree
    assert "src" in tree
    assert "main.py" in tree
    assert "tests" in tree
    assert "test_main.py" in tree


def test_get_directory_tree_skips_git(tmp_path):
    d = tmp_path / "test_skip"
    d.mkdir()
    (d / ".git").mkdir()
    (d / "readme.md").write_text("hello")

    tree = get_directory_tree(str(d))
    assert ".git" not in tree
    assert "readme.md" in tree


def test_format_tree_within_limit():
    tree = "├── file1\n├── file2\n└── file3"
    result = format_tree(tree, max_lines=200)
    assert result == tree


def test_format_tree_truncated():
    lines = [f"line_{i}" for i in range(300)]
    tree = "\n".join(lines)
    result = format_tree(tree, max_lines=200)
    assert "[...truncated]" in result
    assert result.count("\n") < 250


@patch("source_scout.cloner.git.Repo.clone_from")
def test_clone_repo_new(mock_clone, tmp_path):
    mock_clone.return_value = None
    with patch("source_scout.cloner._clone_path", return_value=str(tmp_path / "clone")):
        with patch("source_scout.cloner.os.walk", return_value=[]):
            result = clone_repo("https://github.com/owner/repo")
    assert result == str(tmp_path / "clone")
    mock_clone.assert_called_once()


@patch("source_scout.cloner.git.Repo.clone_from")
def test_clone_repo_already_exists(mock_clone, tmp_path):
    clone_dir = tmp_path / "existing"
    clone_dir.mkdir()
    with patch("source_scout.cloner._clone_path", return_value=str(clone_dir)):
        mock_repo = MagicMock()
        mock_remote = MagicMock()
        mock_remote().urls = ["https://github.com/owner/repo"]
        mock_repo.return_value.remote.return_value = mock_remote
        with patch("source_scout.cloner.git.Repo", mock_repo):
            result = clone_repo("https://github.com/owner/repo")
    assert result == str(clone_dir)
    mock_clone.assert_not_called()


@patch("source_scout.cloner.git.Repo.clone_from")
def test_clone_repo_too_large(mock_clone, tmp_path):
    mock_clone.return_value = None
    clone_dir = tmp_path / "large_clone"

    def fake_walk(path, topdown=True, onerror=None, followlinks=False):
        yield (path, [], ["big_file.bin"])

    def fake_getsize(filepath):
        return 250 * 1024 * 1024

    with patch("source_scout.cloner._clone_path", return_value=str(clone_dir)):
        with patch("source_scout.cloner.os.walk", fake_walk):
            with patch("source_scout.cloner.os.path.getsize", fake_getsize):
                from fastmcp.exceptions import ToolError

                with pytest.raises(ToolError, match="size limit"):
                    clone_repo("https://github.com/owner/repo")
