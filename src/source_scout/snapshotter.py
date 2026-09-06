"""Bounded Git-blob materialization. Never checkout or execute fetched code."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import git
from gitdb.db import CompoundDB, PackedDB  # type: ignore[import-untyped]

from . import catalog_core
from .path_safety import PathSafetyError, resolve_under_root, should_skip_path

MAX_BLOB_BYTES = 240_000
MAX_SNAPSHOT_BYTES = 30_000_000
MAX_TREE_ENTRIES = 6_000
SNAPSHOT_FORMAT = "git-blobs-v2"


class SnapshotError(ValueError):
    pass


@contextmanager
def open_snapshot(root: Path) -> Iterator[git.Repo]:
    """Read long paths without subprocess cwd limits; release every pack mapping."""
    repo = git.Repo(git_path(root), odbt=git.GitDB)
    try:
        yield repo
    finally:
        pending = [repo.odb]
        while pending:
            database = pending.pop()
            if isinstance(database, CompoundDB):
                pending.extend(database.databases())
            elif isinstance(database, PackedDB):
                for entity in database.entities():
                    entity.close()
        repo.close()


def git_path(path: Path) -> str:
    value = str(path.resolve())
    if os.name == "nt" and len(value) >= 240 and not value.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return value


def _git_options() -> list[str]:
    # No checkout means attributes, smudge filters and LFS helpers never run.
    return ["-c", "core.longpaths=true", "-c", "core.hooksPath=", "-c", "protocol.ext.allow=never"]


def _regular_blobs(repo: git.Repo, commit: str) -> tuple[list[Any], dict[str, Any]]:
    blobs = []
    total = 0
    omitted = 0
    entries = 0
    root = Path(str(repo.working_tree_dir))
    for entry in cast(Any, repo.commit(commit).tree.traverse()):
        entries += 1
        if entries > MAX_TREE_ENTRIES:
            omitted += 1
            break
        if entry.type != "blob" or entry.mode not in {0o100644, 0o100755}:
            if entry.type != "tree":
                omitted += 1
            continue
        # Literal portable Git paths, not navigator aliases or Windows ADS.
        if any(part in {"", ".", ".."} for part in entry.path.split("/")) or any(
            char in entry.path for char in "\\:\r\n"
        ):
            omitted += 1
            continue
        if should_skip_path(root, root / entry.path):
            omitted += 1
            continue
        if entry.size > MAX_BLOB_BYTES or total + entry.size > MAX_SNAPSHOT_BYTES:
            omitted += 1
            continue
        total += entry.size
        blobs.append(entry)
    return blobs, {
        "format": SNAPSHOT_FORMAT,
        "omitted_entries": omitted,
        "limited": omitted > 0,
        "materialized_bytes": total,
        "max_blob_bytes": MAX_BLOB_BYTES,
        "max_total_bytes": MAX_SNAPSHOT_BYTES,
        "max_tree_entries": MAX_TREE_ENTRIES,
    }


def read_blob(root: Path, commit: str, rel_path: str, *, limit: int = MAX_BLOB_BYTES) -> bytes:
    try:
        path, safe_path = resolve_under_root(root, rel_path)
        if safe_path != rel_path or path.is_symlink():
            raise SnapshotError("Pinned source path is not a literal regular file.")
        with open_snapshot(root) as repo:
            blob = repo.commit(commit).tree / rel_path
            if blob.type != "blob" or blob.mode not in {0o100644, 0o100755}:
                raise SnapshotError("Pinned source is not a regular Git blob.")
            if blob.size > limit:
                raise SnapshotError(f"Pinned source exceeds {limit} bytes: {rel_path}")
            raw = blob.data_stream.read(limit + 1)
        if len(raw) > limit:
            raise SnapshotError(f"Pinned source exceeds {limit} bytes: {rel_path}")
        return bytes(raw)
    except (KeyError, OSError, PathSafetyError, git.GitError) as exc:
        raise SnapshotError(f"Pinned Git blob unavailable: {rel_path}") from exc


def validate_snapshot(root: Path, commit: str) -> dict[str, Any]:
    try:
        with open_snapshot(root) as repo:
            if repo.head.commit.hexsha != commit:
                raise SnapshotError("Pinned snapshot commit changed.")
            blobs, facts = _regular_blobs(repo, commit)
            expected_paths = {str(blob.path) for blob in blobs}
            seen = 0
            for current, directories, filenames in os.walk(root, followlinks=False):
                directories[:] = [name for name in directories if name != ".git"]
                for name in [*directories, *filenames]:
                    actual_path = Path(current) / name
                    seen += 1
                    if seen > MAX_TREE_ENTRIES * 2 or actual_path.is_symlink():
                        raise SnapshotError("Pinned snapshot has changed files or paths.")
                    if (
                        actual_path.is_file()
                        and actual_path.relative_to(root).as_posix() not in expected_paths
                    ):
                        raise SnapshotError("Pinned snapshot has changed files (untracked source).")
            for blob in blobs:
                path, safe_path = resolve_under_root(root, blob.path)
                if safe_path != blob.path or path.is_symlink():
                    raise SnapshotError("Pinned snapshot has changed files or paths.")
                with path.open("rb") as stream:
                    actual = stream.read(MAX_BLOB_BYTES + 1)
                if actual != blob.data_stream.read(MAX_BLOB_BYTES + 1):
                    raise SnapshotError(
                        "Pinned snapshot has changed files; refusing uncertain source context."
                    )
            return facts
    except (OSError, PathSafetyError, git.GitError) as exc:
        raise SnapshotError("Pinned snapshot is missing or has changed files.") from exc


def clone_snapshot(
    repo_url: str,
    owner: str,
    repo: str,
    commit_sha: str,
    default_branch: str | None,
    *,
    preserve_bytes: bool = True,
) -> tuple[Path, str]:
    _ = default_branch, preserve_bytes  # Transitional keyword; new snapshots always use blobs.
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise SnapshotError("Snapshot requires a full hexadecimal Git commit SHA.")
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."} for part in (owner, repo)):
        raise SnapshotError("Invalid snapshot repository identity.")
    # Never replace a historical checkout or silently repair changed user data.
    base = catalog_core.snapshot_path(owner, repo, commit_sha)
    target = base.with_name(base.name + "-" + SNAPSHOT_FORMAT)
    if target.exists():
        validate_snapshot(target, commit_sha)
        return target, commit_sha
    target.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = target.parent
    if os.name == "nt" and len(str(staging_parent)) > 180:
        staging_parent = Path.cwd() / ".source_scout" / "staging"
        if len(str(staging_parent)) > 180:
            staging_parent = Path(tempfile.gettempdir()) / ".source_scout" / "staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".fetch-", dir=staging_parent))
    try:
        # GITHUB_TOKEN authenticates REST only. Git uses configured credentials;
        # no secret is added to a remote, command line or saved config.
        subprocess.run(
            ["git", *_git_options(), "init", git_path(staging)],
            cwd=Path(sys.executable).parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=10,
        )
        with open_snapshot(staging) as cloned:
            subprocess.run(
                [
                    "git",
                    *_git_options(),
                    "-C",
                    git_path(staging),
                    "fetch",
                    "--depth",
                    "1",
                    "--no-tags",
                    "--",
                    repo_url,
                    commit_sha,
                ],
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"},
                cwd=Path(sys.executable).parent,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            actual_sha = str(cloned.commit("FETCH_HEAD").hexsha)
            if actual_sha != commit_sha:
                raise SnapshotError("Fetched snapshot commit mismatch.")
            (staging / ".git" / "HEAD").write_text(commit_sha + "\n", encoding="ascii")
            blobs, _facts = _regular_blobs(cloned, commit_sha)
            for blob in blobs:
                destination, safe_path = resolve_under_root(staging, blob.path)
                if safe_path != blob.path:
                    raise SnapshotError("Unsafe Git tree path.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(blob.data_stream.read(MAX_BLOB_BYTES + 1))
        try:
            staging.rename(target)
        except OSError as exc:
            if exc.errno == 18 or getattr(exc, "winerror", None) == 17:
                # Temporary Git storage may be on another Windows volume.
                publish = Path(tempfile.mkdtemp(prefix=".fetch-", dir=target.parent))
                try:
                    shutil.copytree(staging, publish, dirs_exist_ok=True)
                    try:
                        publish.rename(target)
                    except OSError:
                        if not target.exists():
                            raise
                        validate_snapshot(target, commit_sha)
                finally:
                    if publish.exists():
                        _remove_generated_path(publish, expected_parent=target.parent)
                return target, commit_sha
            # Concurrent adds may publish the same immutable snapshot first.
            if not target.exists():
                raise
            validate_snapshot(target, commit_sha)
        return target, commit_sha
    except (git.GitError, subprocess.SubprocessError) as exc:
        # Git stderr can contain credential-helper output. Do not serialize it.
        raise SnapshotError(
            "Git fetch failed. Verify Git credentials (GITHUB_TOKEN is REST-only), "
            "commit access, connectivity and local path support."
        ) from exc
    finally:
        if staging.exists():
            _remove_generated_path(staging, expected_parent=staging_parent)


def _remove_generated_path(path: Path, *, expected_parent: Path) -> None:
    resolved = path.resolve()
    if (
        resolved.parent != expected_parent.resolve()
        or not resolved.name.startswith(".fetch-")
        or path.is_symlink()
    ):
        raise SnapshotError(f"Refusing to remove a non-staging path: {resolved}")

    def writable_retry(function: Any, name: str, _error: Any) -> None:
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
        function(name)

    shutil.rmtree(resolved, onerror=writable_retry)
