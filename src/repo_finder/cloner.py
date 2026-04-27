import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import git
from fastmcp.exceptions import ToolError

from .constants import SKIP_DIRS
from .urls import normalize_url

MAX_CLONE_SIZE_MB = 200


def _clone_path(repo_url: str) -> str:
    sanitized = normalize_url(repo_url)
    key = hashlib.sha256(sanitized.encode()).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"repo_finder_{key}")


def clone_repo(repo_url: str) -> str:
    clone_dir = _clone_path(repo_url)
    path = Path(clone_dir)

    if path.exists():
        try:
            repo = git.Repo(clone_dir)
            url_normalized = normalize_url(repo_url)
            remote_url = next(repo.remote().urls, "")
            remote_normalized = normalize_url(remote_url)
            if url_normalized == remote_normalized:
                return clone_dir
        except git.InvalidGitRepositoryError:
            shutil.rmtree(clone_dir, ignore_errors=True)

    try:
        git.Repo.clone_from(repo_url, clone_dir, depth=1)
    except git.GitCommandError as exc:
        raise ToolError(f"Failed to clone repository: {exc.stderr.strip()}")

    total_size = 0
    for dirpath, _dirnames, filenames in os.walk(clone_dir):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total_size += os.path.getsize(fp)
            except OSError:
                pass
            if total_size > MAX_CLONE_SIZE_MB * 1024 * 1024:
                shutil.rmtree(clone_dir, ignore_errors=True)
                raise ToolError(
                    f"Repository exceeds size limit ({MAX_CLONE_SIZE_MB}MB)"
                )

    return clone_dir


def cleanup_clone(repo_path: str) -> None:
    shutil.rmtree(repo_path, ignore_errors=True)


def get_directory_tree(path: str, prefix: str = "", max_entries: int = 500) -> str:
    lines: list[str] = []
    try:
        entries = sorted(os.listdir(path))
    except PermissionError:
        return ""

    if max_entries <= 0:
        return ""

    entries = [e for e in entries[:max_entries] if e not in SKIP_DIRS]

    for i, entry in enumerate(entries):
        full = os.path.join(path, entry)
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        lines.append(prefix + connector + entry)
        if os.path.isdir(full):
            next_prefix = prefix + ("    " if is_last else "│   ")
            subtree = get_directory_tree(full, next_prefix, max_entries=50)
            if subtree:
                lines.append(subtree)

    return "\n".join(lines)


def format_tree(directory_tree: str, max_lines: int = 200) -> str:
    if not directory_tree:
        return ""
    lines = directory_tree.split("\n")
    if len(lines) <= max_lines:
        return directory_tree
    return "\n".join(lines[:max_lines]) + "\n[...truncated]"
