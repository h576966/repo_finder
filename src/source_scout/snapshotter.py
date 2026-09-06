import shutil
from pathlib import Path

import git
from fastmcp.exceptions import ToolError

from . import catalog_core


def clone_snapshot(
    repo_url: str,
    owner: str,
    repo: str,
    commit_sha: str,
    default_branch: str | None,
    *,
    preserve_bytes: bool = False,
) -> tuple[Path, str]:
    _ = default_branch
    target = catalog_core.snapshot_path(owner, repo, commit_sha)
    if target.exists():
        try:
            existing = git.Repo(str(target))
            try:
                existing_sha = str(existing.head.commit.hexsha)
                byte_exact = True
                if preserve_bytes:
                    byte_exact = not existing.is_dirty(untracked_files=True)
                    byte_exact = byte_exact and (
                        existing.config_reader("repository").get_value(
                            "core", "autocrlf", default=None
                        )
                        is False
                    )
            finally:
                existing.close()
            if existing_sha == commit_sha and byte_exact:
                return target, existing_sha
            _remove_generated_path(target)
        except git.InvalidGitRepositoryError:
            _remove_generated_path(target)

    target.mkdir(parents=True, exist_ok=True)
    cloned: git.Repo | None = None
    try:
        cloned = git.Repo.init(str(target))
        if preserve_bytes:
            with cloned.config_writer("repository") as config:
                config.set_value("core", "autocrlf", False)
        cloned.create_remote("origin", repo_url)
        cloned.git.fetch("--depth", "1", "origin", commit_sha)
        cloned.git.checkout("--detach", "FETCH_HEAD")
    except git.GitCommandError as exc:
        if cloned is not None:
            cloned.close()
        _remove_generated_path(target)
        raise ToolError(f"Failed to clone repository snapshot: {exc.stderr.strip()}")

    actual_sha = str(cloned.head.commit.hexsha)
    cloned.close()
    if actual_sha != commit_sha:
        _remove_generated_path(target)
        raise ToolError(
            f"Snapshot checkout mismatch for {owner}/{repo}: expected {commit_sha}, got {actual_sha}"
        )

    return target, actual_sha


def _remove_generated_path(path: Path) -> None:
    home = catalog_core.ensure_home().resolve()
    resolved = path.resolve()
    if home not in resolved.parents:
        raise ToolError(f"Refusing to remove path outside source_scout home: {resolved}")
    shutil.rmtree(resolved, ignore_errors=True)
