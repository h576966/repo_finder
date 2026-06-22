import shutil
from pathlib import Path

import git
from fastmcp.exceptions import ToolError

from . import catalog


def clone_snapshot(
    repo_url: str,
    owner: str,
    repo: str,
    commit_sha: str,
    default_branch: str | None,
) -> tuple[Path, str]:
    _ = default_branch
    target = catalog.snapshot_path(owner, repo, commit_sha)
    if target.exists():
        try:
            existing = git.Repo(str(target))
            return target, str(existing.head.commit.hexsha)
        except git.InvalidGitRepositoryError:
            _remove_generated_path(target)

    target.mkdir(parents=True, exist_ok=True)
    try:
        cloned = git.Repo.init(str(target))
        cloned.create_remote("origin", repo_url)
        cloned.git.fetch("--depth", "1", "origin", commit_sha)
        cloned.git.checkout("--detach", "FETCH_HEAD")
    except git.GitCommandError as exc:
        _remove_generated_path(target)
        raise ToolError(f"Failed to clone repository snapshot: {exc.stderr.strip()}")

    actual_sha = str(cloned.head.commit.hexsha)
    if actual_sha != commit_sha:
        _remove_generated_path(target)
        raise ToolError(
            f"Snapshot checkout mismatch for {owner}/{repo}: expected {commit_sha}, got {actual_sha}"
        )

    return target, actual_sha


def _remove_generated_path(path: Path) -> None:
    home = catalog.ensure_home().resolve()
    resolved = path.resolve()
    if home not in resolved.parents:
        raise ToolError(f"Refusing to remove path outside source_scout home: {resolved}")
    shutil.rmtree(resolved, ignore_errors=True)
