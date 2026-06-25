import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from . import path_safety
from .constants import SKIP_DIRS
from .fastcontext_constants import (
    LOCAL_EXTRA_SKIP_DIRS,
    LOCAL_SKIP_FILE_NAMES,
    MAX_GLOB_RESULTS,
    MAX_GREP_FILE_BYTES,
    MAX_GREP_RESULTS,
    MAX_READ_FILE_BYTES,
    MAX_READ_LINES,
    NOISY_EVIDENCE_FILES,
    NOISY_EVIDENCE_PREFIXES,
    PRIMARY_SOURCE_PREFIXES,
    RG_TIMEOUT_SECONDS,
)
from .fastcontext_types import FastContextError


def _evidence_path_sort_key(path: str) -> tuple[int, str]:
    normalized = path.replace("\\", "/")
    if _is_primary_source_path(normalized):
        return (0, normalized)
    if _is_noisy_evidence_path(normalized):
        return (2, normalized)
    return (1, normalized)


def _is_primary_source_path(path: str) -> bool:
    return path.replace("\\", "/").startswith(PRIMARY_SOURCE_PREFIXES)


def _is_noisy_evidence_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized in NOISY_EVIDENCE_FILES or normalized.startswith(NOISY_EVIDENCE_PREFIXES)


def _match_sort_key(match: dict[str, Any]) -> tuple[int, str, int]:
    path = str(match.get("path", ""))
    line = _optional_int(match.get("start_line") or match.get("line")) or 0
    priority, normalized = _evidence_path_sort_key(path)
    return priority, normalized, line


def execute_tool(root: Path, call: dict[str, Any]) -> dict[str, Any]:
    tool = _canonical_tool_name(_tool_name(call))
    args = _tool_args(call)
    try:
        if args.get("arguments_error") or call.get("arguments_error"):
            raise FastContextError(str(args.get("arguments_error") or call["arguments_error"]))
        if tool == "Read":
            result = read_file(
                root,
                str(args.get("path", "")),
                offset=_optional_int(args.get("offset") or args.get("start") or args.get("start_line")),
                limit=_optional_int(args.get("limit")),
                end=_optional_int(args.get("end") or args.get("end_line")),
            )
        elif tool == "Glob":
            result = glob_paths(
                root,
                str(args.get("pattern") or args.get("glob") or "**/*"),
                directory=str(args.get("directory") or "."),
            )
        elif tool == "Grep":
            result = grep_paths(
                root,
                str(args.get("pattern", "")),
                file_glob=str(args["glob"]) if args.get("glob") else None,
                search_path=str(args.get("path") or "."),
                output_mode=str(args.get("output_mode") or "content"),
                before_context=_optional_int(args.get("-B")) or 0,
                after_context=_optional_int(args.get("-A")) or 0,
                context=_optional_int(args.get("-C")) or 0,
                line_numbers=bool(args.get("-n", True)),
                ignore_case=bool(args.get("-i", False)),
                file_type=str(args["type"]) if args.get("type") else None,
                head_limit=_optional_int(args.get("head_limit")) or MAX_GREP_RESULTS,
                multiline=bool(args.get("multiline", False)),
            )
        else:
            raise FastContextError(f"Unsupported tool: {tool}")
        return {
            "tool_call_id": call.get("id"),
            "tool": tool,
            "args": args,
            "ok": True,
            "result": result,
            "text": _tool_result_text(tool, result),
        }
    except Exception as exc:
        return {
            "tool_call_id": call.get("id"),
            "tool": tool,
            "args": args,
            "ok": False,
            "error": _tool_error_text(root, exc),
        }


def _tool_error_text(root: Path, exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if any(term in lowered for term in ["escapes", "absolute", "relative", "does not exist"]):
        return (
            f"{message} Use paths relative to {root.resolve()}, for example "
            "src/source_scout/server.py, not /source_scout/src/source_scout/server.py."
        )
    return message


def read_file(
    root: Path,
    rel_path: str,
    start: int | None = None,
    offset: int | None = None,
    limit: int | None = None,
    end: int | None = None,
) -> dict[str, Any]:
    path, safe_rel = _resolve_under_root(root, rel_path)
    if not path.is_file():
        raise FastContextError(f"READ target is not a file: {safe_rel}")
    if path.stat().st_size > MAX_READ_FILE_BYTES:
        raise FastContextError(f"READ target is too large: {safe_rel}")

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return {"path": safe_rel, "start_line": 1, "end_line": 0, "content": "", "line_count": 0}

    start_line = min(max(1, offset or start or 1), len(lines))
    read_limit = min(max(1, limit or MAX_READ_LINES), MAX_READ_LINES)
    end_line = min(len(lines), end or start_line + read_limit - 1)
    if end_line - start_line + 1 > MAX_READ_LINES:
        end_line = start_line + MAX_READ_LINES - 1
    selected = lines[start_line - 1 : end_line]
    content = "\n".join(
        f"{line_number}|{line}" for line_number, line in enumerate(selected, start=start_line)
    )
    return {
        "path": safe_rel,
        "start_line": start_line,
        "end_line": end_line,
        "content": content,
        "line_count": len(lines),
    }


def glob_paths(
    root: Path,
    pattern: str,
    limit: int = MAX_GLOB_RESULTS,
    directory: str = ".",
) -> dict[str, Any]:
    safe_pattern = _safe_glob_pattern(root, pattern)
    directory_path, safe_directory = _resolve_directory(root, directory)
    rg_matches = _rg_glob(root, directory_path, safe_pattern, limit)
    if rg_matches is not None:
        return {
            "directory": safe_directory,
            "pattern": safe_pattern,
            "matches": rg_matches[:limit],
            "truncated": len(rg_matches) >= limit,
            "backend": "rg",
        }
    matches: list[str] = []
    for path in _iter_files(root, file_glob=safe_pattern):
        if len(matches) >= limit:
            break
        if not _path_is_under(path, directory_path):
            continue
        matches.append(_relative_path(root, path))
    matches.sort(key=_evidence_path_sort_key)
    return {
        "directory": safe_directory,
        "pattern": safe_pattern,
        "matches": matches,
        "truncated": len(matches) >= limit,
        "backend": "python",
    }


def grep_paths(
    root: Path,
    pattern: str,
    file_glob: str | None = None,
    limit: int = MAX_GREP_RESULTS,
    search_path: str = ".",
    output_mode: str = "content",
    before_context: int = 0,
    after_context: int = 0,
    context: int = 0,
    line_numbers: bool = True,
    ignore_case: bool = False,
    file_type: str | None = None,
    head_limit: int | None = None,
    multiline: bool = False,
) -> dict[str, Any]:
    if not pattern.strip():
        raise FastContextError("GREP requires a non-empty pattern.")
    effective_limit = min(max(1, head_limit or limit), MAX_GREP_RESULTS)
    search_root, safe_search_path = _resolve_search_path(root, search_path)
    rg_result = _rg_grep(
        root=root,
        search_path=search_root,
        safe_search_path=safe_search_path,
        pattern=pattern,
        file_glob=file_glob,
        limit=effective_limit,
        output_mode=output_mode,
        before_context=before_context,
        after_context=after_context,
        context=context,
        line_numbers=line_numbers,
        ignore_case=ignore_case,
        file_type=file_type,
        multiline=multiline,
    )
    if rg_result is not None:
        return rg_result
    try:
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(pattern, flags=flags)
        regex_mode = True
    except re.error:
        compiled = re.compile(re.escape(pattern), flags=re.IGNORECASE if ignore_case else 0)
        regex_mode = False

    candidates = [
        path for path in _grep_candidate_files(root, file_glob) if _path_is_under(path, search_root)
    ]
    candidates.sort(key=lambda path: _evidence_path_sort_key(_relative_path(root, path)))
    matches: list[dict[str, Any]] = []
    for path in candidates:
        if len(matches) >= effective_limit:
            break
        if path.stat().st_size > MAX_GREP_FILE_BYTES:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel_path = _relative_path(root, path)
        file_matched = False
        for line_number, line in enumerate(lines, start=1):
            if not compiled.search(line):
                continue
            file_matched = True
            if output_mode in {"files", "files_with_matches"}:
                matches.append({"path": rel_path})
                break
            if output_mode == "count":
                continue
            start_line = max(1, line_number - (context or before_context))
            end_line = min(len(lines), line_number + (context or after_context))
            matches.append(
                {
                    "path": rel_path,
                    "line": line_number,
                    "start_line": start_line,
                    "end_line": end_line,
                    "citation": f"{rel_path}:{line_number}-{line_number}",
                    "text": line.strip()[:220],
                }
            )
            if len(matches) >= effective_limit:
                break
        if output_mode == "count" and file_matched:
            count = sum(1 for line in lines if compiled.search(line))
            matches.append({"path": rel_path, "count": count})

    return {
        "path": safe_search_path,
        "pattern": pattern,
        "glob": file_glob,
        "regex_mode": regex_mode,
        "matches": matches,
        "truncated": len(matches) >= effective_limit,
        "output_mode": output_mode,
        "backend": "python",
    }


def _tool_name(call: dict[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"]).upper()
    return str(call.get("tool") or call.get("name") or "").upper()


def _tool_args(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function")
    raw_args: Any = call.get("args") or call.get("arguments")
    if isinstance(function, dict) and function.get("arguments") is not None:
        raw_args = function["arguments"]
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return _parse_call_args(raw_args)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_call_args(body: str) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for match in re.finditer(
        r"(?P<key>\w+)\s*=\s*(?P<value>\"[^\"]*\"|'[^']*'|\d+)",
        body,
        flags=re.DOTALL,
    ):
        value = match.group("value").strip()
        if value.isdigit():
            args[match.group("key")] = int(value)
        else:
            args[match.group("key")] = value.strip("\"'")
    return args


def _first_quoted(value: str) -> str | None:
    match = re.search(r"\"([^\"]+)\"|'([^']+)'", value)
    if not match:
        return None
    return str(match.group(1) or match.group(2))


def _canonical_tool_name(name: str) -> str:
    normalized = name.strip().upper()
    if normalized == "READ":
        return "Read"
    if normalized == "GLOB":
        return "Glob"
    if normalized == "GREP":
        return "Grep"
    return name


def _tool_result_text(tool: str, result: dict[str, Any]) -> str:
    if tool == "Read":
        path = str(result.get("path", ""))
        start = int(result.get("start_line", 1))
        end = int(result.get("end_line", start))
        return f"```{path}:{start}-{end}\n{result.get('content', '')}\n```"
    if tool == "Glob":
        matches = result.get("matches")
        if isinstance(matches, list):
            return "\n".join(str(match) for match in matches)
    if tool == "Grep":
        matches = result.get("matches")
        if isinstance(matches, list):
            lines = []
            for match in matches:
                if not isinstance(match, dict):
                    continue
                path = str(match.get("path", ""))
                line = match.get("line")
                text = str(match.get("text", ""))
                if line is None:
                    lines.append(path)
                else:
                    lines.append(f"{path}:{line}:{text}")
            return "\n".join(lines)
    return json.dumps(result, sort_keys=True)


def _rg_glob(
    root: Path,
    directory_path: Path,
    pattern: str,
    limit: int,
) -> list[str] | None:
    rg = shutil.which("rg")
    if rg is None:
        return None
    safe_directory = _relative_path(root, directory_path) if directory_path != root.resolve() else "."
    command = [
        rg,
        "--no-config",
        "--files",
        safe_directory,
        "--glob",
        pattern,
        *_rg_skip_globs(),
    ]
    completed = _run_rg(root, command)
    if completed is None:
        return None
    if completed.returncode not in {0, 1}:
        return None
    matches = [_normalize_rg_path(line) for line in completed.stdout.splitlines() if line.strip()]
    safe_matches = [path for path in matches if _is_safe_relative_result(root, path)]
    return sorted(safe_matches, key=_evidence_path_sort_key)[:limit]


def _rg_grep(
    *,
    root: Path,
    search_path: Path,
    safe_search_path: str,
    pattern: str,
    file_glob: str | None,
    limit: int,
    output_mode: str,
    before_context: int,
    after_context: int,
    context: int,
    line_numbers: bool,
    ignore_case: bool,
    file_type: str | None,
    multiline: bool,
) -> dict[str, Any] | None:
    rg = shutil.which("rg")
    if rg is None:
        return None
    safe_glob = _safe_glob_pattern(root, file_glob) if file_glob else None
    search_arg = safe_search_path or "."
    command = [rg, "--no-config", "--color", "never", "--no-heading", "--with-filename"]
    if line_numbers:
        command.append("--line-number")
    if ignore_case:
        command.append("--ignore-case")
    if multiline:
        command.append("--multiline")
    if output_mode in {"files", "files_with_matches"}:
        command.append("--files-with-matches")
    elif output_mode == "count":
        command.append("--count")
    if context:
        command.extend(["-C", str(max(0, context))])
    else:
        if before_context:
            command.extend(["-B", str(max(0, before_context))])
        if after_context:
            command.extend(["-A", str(max(0, after_context))])
    if safe_glob:
        command.extend(["--glob", safe_glob])
    if file_type:
        command.extend(["--type", file_type])
    command.extend([*_rg_skip_globs(), "--", pattern, search_arg])
    completed = _run_rg(root, command)
    if completed is None:
        return None
    if completed.returncode not in {0, 1}:
        return None
    matches = _parse_rg_output(
        root=root,
        output=completed.stdout,
        output_mode=output_mode,
        limit=limit,
    )
    return {
        "path": safe_search_path,
        "pattern": pattern,
        "glob": safe_glob,
        "regex_mode": True,
        "matches": matches,
        "truncated": len(matches) >= limit,
        "output_mode": output_mode,
        "backend": "rg",
        "searched_path": _relative_path(root, search_path) if search_path != root.resolve() else ".",
    }


def _run_rg(root: Path, command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=RG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _rg_skip_globs() -> list[str]:
    args: list[str] = []
    for dirname in sorted(SKIP_DIRS | LOCAL_EXTRA_SKIP_DIRS):
        args.extend(["--glob", f"!{dirname}/**"])
    for filename in sorted(LOCAL_SKIP_FILE_NAMES):
        args.extend(["--glob", f"!{filename}"])
    return args


def _parse_rg_output(
    *,
    root: Path,
    output: str,
    output_mode: str,
    limit: int,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    scan_limit = max(limit, limit * 5)
    for raw_line in output.splitlines():
        if len(matches) >= scan_limit:
            break
        line = raw_line.strip()
        if not line:
            continue
        if output_mode in {"files", "files_with_matches"}:
            path = _normalize_rg_path(line)
            if _is_safe_relative_result(root, path):
                matches.append({"path": path})
            continue
        if output_mode == "count":
            path, raw_count = _split_rg_pair(line)
            if path and _is_safe_relative_result(root, path):
                counts[path] = counts.get(path, 0) + (_optional_int(raw_count) or 0)
            continue
        parsed = _parse_rg_content_line(line)
        if parsed is None:
            continue
        path, line_number, text = parsed
        if not _is_safe_relative_result(root, path):
            continue
        matches.append(
            {
                "path": path,
                "line": line_number,
                "start_line": line_number,
                "end_line": line_number,
                "citation": f"{path}:{line_number}-{line_number}",
                "text": text[:220],
            }
        )
    if output_mode == "count":
        matches.extend({"path": path, "count": count} for path, count in sorted(counts.items()))
    return sorted(matches, key=_match_sort_key)[:limit]


def _split_rg_pair(line: str) -> tuple[str, str]:
    if ":" not in line:
        return _normalize_rg_path(line), ""
    path, value = line.rsplit(":", 1)
    return _normalize_rg_path(path), value


def _parse_rg_content_line(line: str) -> tuple[str, int, str] | None:
    match = re.match(r"(?P<path>.*?)(?P<sep>[:-])(?P<line>\d+)(?P=sep)(?P<text>.*)", line)
    if match is None:
        return None
    line_number = _optional_int(match.group("line"))
    if line_number is None:
        return None
    return _normalize_rg_path(match.group("path")), line_number, match.group("text").strip()


def _normalize_rg_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("./")


def _is_safe_relative_result(root: Path, rel_path: str) -> bool:
    return path_safety.is_safe_relative_result(
        root,
        rel_path,
        extra_skip_dirs=LOCAL_EXTRA_SKIP_DIRS,
    )


def _resolve_under_root(root: Path, rel_path: str) -> tuple[Path, str]:
    cleaned = _normalize_workspace_reference(root.resolve(), rel_path)
    try:
        return path_safety.resolve_under_root(
            root,
            cleaned,
            extra_skip_dirs=LOCAL_EXTRA_SKIP_DIRS,
        )
    except path_safety.PathSafetyError as exc:
        raise FastContextError(str(exc)) from exc


def _relative_path(root: Path, path: Path) -> str:
    return path_safety.relative_path(root, path)


def _normalize_workspace_reference(
    root: Path,
    value: str,
    *,
    strip_line: bool = True,
    allow_glob: bool = False,
) -> str:
    normalized = path_safety.normalize_workspace_reference(
        root,
        value,
        strip_line=strip_line,
        allow_glob=allow_glob,
    )
    alias_reference = _workspace_alias_reference(
        root,
        normalized,
        allow_glob=allow_glob,
    )
    return alias_reference or normalized


def _workspace_alias_reference(
    root: Path,
    cleaned: str,
    *,
    allow_glob: bool,
) -> str | None:
    aliases = {root.resolve().name.lower(), "source_scout"}
    parts = [part for part in PurePosixPath(cleaned).parts if part not in {"", ".", "/"}]
    for index, part in enumerate(parts):
        if part.lower() not in aliases:
            continue
        suffix_parts = parts[index + 1 :]
        if not suffix_parts:
            return "."
        suffix = PurePosixPath(*suffix_parts).as_posix()
        if _workspace_suffix_target_exists(root, suffix, allow_glob=allow_glob):
            return suffix
    return None


def _workspace_suffix_target_exists(
    root: Path,
    suffix: str,
    *,
    allow_glob: bool,
) -> bool:
    return path_safety.workspace_suffix_target_exists(root, suffix, allow_glob=allow_glob)


def _has_glob_meta(value: str) -> bool:
    return path_safety.has_glob_meta(value)


def _safe_glob_pattern(root: Path, pattern: str) -> str:
    try:
        cleaned = (
            _normalize_workspace_reference(
                root.resolve(),
                pattern,
                strip_line=False,
                allow_glob=True,
            )
            or "**/*"
        )
        if Path(cleaned).is_absolute() or cleaned.startswith("/"):
            raise path_safety.PathSafetyError(f"Glob pattern must be relative: {pattern}")
        if ".." in PurePosixPath(cleaned).parts:
            raise path_safety.PathSafetyError(f"Glob pattern escapes snapshot root: {pattern}")
        return cleaned
    except path_safety.PathSafetyError as exc:
        raise FastContextError(str(exc)) from exc


def _resolve_directory(root: Path, directory: str) -> tuple[Path, str]:
    cleaned = _normalize_workspace_reference(root.resolve(), directory) or "."
    if cleaned == ".":
        return root.resolve(), "."
    path, safe_rel = _resolve_under_root(root, cleaned)
    if not path.is_dir():
        raise FastContextError(f"Glob directory is not a directory: {safe_rel}")
    return path, safe_rel


def _resolve_search_path(root: Path, search_path: str) -> tuple[Path, str]:
    cleaned = _normalize_workspace_reference(root.resolve(), search_path) or "."
    if cleaned == ".":
        return root.resolve(), "."
    path, safe_rel = _resolve_under_root(root, cleaned)
    if not path.exists():
        raise FastContextError(f"Grep path does not exist: {safe_rel}")
    return path, safe_rel


def _path_is_under(path: Path, root: Path) -> bool:
    return path_safety.path_is_under(path, root)


def _should_skip_path(root: Path, path: Path) -> bool:
    if path.name in LOCAL_SKIP_FILE_NAMES:
        return True
    return path_safety.should_skip_path(
        root,
        path,
        extra_skip_dirs=LOCAL_EXTRA_SKIP_DIRS,
    )


def _grep_candidate_files(root: Path, file_glob: str | None) -> list[Path]:
    safe_pattern = _safe_glob_pattern(root, file_glob) if file_glob else None
    return list(_iter_files(root, file_glob=safe_pattern))


def _iter_files(root: Path, file_glob: str | None = None) -> list[Path]:
    root_resolved = root.resolve()
    matches: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root_resolved):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in SKIP_DIRS and dirname not in LOCAL_EXTRA_SKIP_DIRS
        ]
        current_path = Path(current_root)
        for filename in filenames:
            path = current_path / filename
            if _should_skip_path(root_resolved, path):
                continue
            rel_path = _relative_path(root_resolved, path)
            if file_glob and not PurePosixPath(rel_path).match(file_glob):
                continue
            matches.append(path)
    return sorted(matches, key=lambda path: _evidence_path_sort_key(_relative_path(root_resolved, path)))


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_label(label: str | None) -> str:
    if not label:
        return ""
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in label)
