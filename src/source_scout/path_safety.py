import re
from pathlib import Path, PurePosixPath

from .constants import SKIP_DIRS

EXTRA_SKIP_DIRS: set[str] = {".next", ".source_scout", "build", "coverage", "dist"}


class PathSafetyError(ValueError):
    pass


def default_skip_dirs(extra_skip_dirs: set[str] | None = None) -> set[str]:
    return set(SKIP_DIRS) | set(extra_skip_dirs or EXTRA_SKIP_DIRS)


def normalize_workspace_reference(
    root: Path,
    value: str,
    *,
    strip_line: bool = True,
    allow_glob: bool = False,
) -> str:
    cleaned = value.strip().strip("`\"'").replace("\\", "/")
    if strip_line:
        cleaned = strip_line_suffix(cleaned)
    if not cleaned:
        return ""
    if cleaned == ".":
        return "."

    root_resolved = root.resolve()
    root_posix = root_resolved.as_posix().rstrip("/")
    for candidate_text in (cleaned, cleaned.lstrip("/")):
        if candidate_text.lower() == root_posix.lower():
            return "."
        prefix = f"{root_posix}/"
        if candidate_text.lower().startswith(prefix.lower()):
            return candidate_text[len(prefix):] or "."

    if not allow_glob and not has_glob_meta(cleaned) and Path(cleaned).is_absolute():
        try:
            return Path(cleaned).resolve().relative_to(root_resolved).as_posix()
        except ValueError:
            pass

    workspace_suffix = workspace_suffix_reference(root_resolved, cleaned, allow_glob=allow_glob)
    if workspace_suffix is not None:
        return workspace_suffix
    if cleaned.startswith("./"):
        return cleaned[2:]
    return cleaned


def resolve_under_root(
    root: Path,
    rel_path: str,
    *,
    extra_skip_dirs: set[str] | None = None,
) -> tuple[Path, str]:
    root_resolved = root.resolve()
    cleaned = normalize_workspace_reference(root_resolved, rel_path)
    if not cleaned:
        raise PathSafetyError("Path is required.")

    if Path(cleaned).is_absolute():
        candidate = Path(cleaned).resolve()
    else:
        parts = PurePosixPath(cleaned).parts
        if ".." in parts:
            raise PathSafetyError(f"Path escapes snapshot root: {rel_path}")
        candidate = (root_resolved / cleaned).resolve()

    try:
        relative = candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise PathSafetyError(f"Path escapes snapshot root: {rel_path}") from exc

    skip_dirs = default_skip_dirs(extra_skip_dirs)
    if any(part in skip_dirs for part in relative.parts):
        raise PathSafetyError(f"Path is under a skipped directory: {rel_path}")
    return candidate, relative.as_posix()


def relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def should_skip_path(
    root: Path,
    path: Path,
    *,
    extra_skip_dirs: set[str] | None = None,
) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    skip_dirs = default_skip_dirs(extra_skip_dirs)
    return any(part in skip_dirs for part in relative.parts)


def is_safe_relative_result(
    root: Path,
    rel_path: str,
    *,
    extra_skip_dirs: set[str] | None = None,
) -> bool:
    try:
        resolve_under_root(root, rel_path, extra_skip_dirs=extra_skip_dirs)
    except PathSafetyError:
        return False
    return True


def safe_glob_pattern(root: Path, pattern: str) -> str:
    cleaned = normalize_workspace_reference(
        root.resolve(),
        pattern,
        strip_line=False,
        allow_glob=True,
    ) or "**/*"
    if Path(cleaned).is_absolute() or cleaned.startswith("/"):
        raise PathSafetyError(f"Glob pattern must be relative: {pattern}")
    if ".." in PurePosixPath(cleaned).parts:
        raise PathSafetyError(f"Glob pattern escapes snapshot root: {pattern}")
    return cleaned


def has_glob_meta(value: str) -> bool:
    return any(char in value for char in "*?[")


def strip_line_suffix(path: str) -> str:
    return re.sub(r":\d+(?:-\d+)?$", "", path)


def workspace_suffix_reference(
    root: Path,
    cleaned: str,
    *,
    allow_glob: bool,
) -> str | None:
    parts = [
        part
        for part in PurePosixPath(cleaned).parts
        if part not in {"", ".", "/"}
    ]
    root_name = root.name.lower()
    for index, part in enumerate(parts):
        if part.lower() != root_name:
            continue
        suffix_parts = parts[index + 1:]
        if not suffix_parts:
            return "."
        suffix = PurePosixPath(*suffix_parts).as_posix()
        if index == 0 or workspace_suffix_target_exists(root, suffix, allow_glob=allow_glob):
            return suffix
    return None


def workspace_suffix_target_exists(
    root: Path,
    suffix: str,
    *,
    allow_glob: bool,
) -> bool:
    if not allow_glob or not has_glob_meta(suffix):
        return (root / suffix).exists()
    prefix_parts: list[str] = []
    for part in PurePosixPath(suffix).parts:
        if has_glob_meta(part):
            break
        prefix_parts.append(part)
    if not prefix_parts:
        return True
    return (root / PurePosixPath(*prefix_parts).as_posix()).exists()
