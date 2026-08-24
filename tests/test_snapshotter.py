import shutil
from unittest.mock import MagicMock, patch

from source_scout import snapshotter


def test_clone_snapshot_reuses_matching_cached_commit(tmp_path) -> None:
    target = tmp_path / "snapshot"
    target.mkdir()
    existing = MagicMock()
    existing.head.commit.hexsha = "abc123"

    with (
        patch("source_scout.snapshotter.catalog_core.snapshot_path", return_value=target),
        patch("source_scout.snapshotter.git.Repo", return_value=existing) as repo_class,
    ):
        result = snapshotter.clone_snapshot(
            "https://github.com/owner/repo",
            "owner",
            "repo",
            "abc123",
            "main",
        )

    assert result == (target, "abc123")
    existing.close.assert_called_once_with()
    repo_class.init.assert_not_called()


def test_clone_snapshot_replaces_cached_commit_mismatch(tmp_path) -> None:
    target = tmp_path / "snapshot"
    target.mkdir()
    existing = MagicMock()
    existing.head.commit.hexsha = "oldsha"
    cloned = MagicMock()
    cloned.head.commit.hexsha = "newsha"

    def remove_cached(path) -> None:
        shutil.rmtree(path)

    with (
        patch("source_scout.snapshotter.catalog_core.snapshot_path", return_value=target),
        patch("source_scout.snapshotter.git.Repo", return_value=existing) as repo_class,
        patch("source_scout.snapshotter._remove_generated_path", side_effect=remove_cached) as remove,
    ):
        repo_class.init.return_value = cloned
        result = snapshotter.clone_snapshot(
            "https://github.com/owner/repo",
            "owner",
            "repo",
            "newsha",
            "main",
        )

    assert result == (target, "newsha")
    existing.close.assert_called_once_with()
    remove.assert_called_once_with(target)
    cloned.git.fetch.assert_called_once_with("--depth", "1", "origin", "newsha")
    cloned.git.checkout.assert_called_once_with("--detach", "FETCH_HEAD")
