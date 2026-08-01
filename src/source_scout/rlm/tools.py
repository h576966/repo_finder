import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from source_scout import catalog, path_safety, repo_map

from .schemas import RlmToolCall, RlmToolResult


class RlmToolError(ValueError):
    pass


class RlmReadOnlyTools:
    def __init__(
        self,
        root: str | Path,
        *,
        max_files: int = 200,
        max_scan_files: int = 5_000,
        max_read_lines: int = 80,
        max_file_bytes: int = 200_000,
        max_grep_results: int = 50,
    ):
        self.root = Path(root).expanduser().resolve()
        if not self.root.exists() or not self.root.is_dir():
            raise RlmToolError(f"RLM tool root must be an existing directory: {root}")
        self.max_files = max(1, max_files)
        self.max_scan_files = max(self.max_files, max_scan_files)
        self.max_read_lines = max(1, max_read_lines)
        self.max_file_bytes = max(1, max_file_bytes)
        self.max_grep_results = max(1, max_grep_results)

    def run(self, call: RlmToolCall) -> RlmToolResult:
        try:
            result = self._run_result(call.tool, call.arguments)
            return RlmToolResult(tool=call.tool, ok=True, result=result, call_id=call.call_id)
        except Exception as exc:
            return RlmToolResult(tool=call.tool, ok=False, error=str(exc), call_id=call.call_id)

    def list_files(self, pattern: str = "**/*", limit: int | None = None) -> dict[str, Any]:
        safe_pattern = self._safe_glob(self.root, pattern)
        effective_limit = self._limit(limit, self.max_files)
        scan = self._iter_files(self.root)
        matches: list[str] = []
        truncated = False
        for path in scan.files:
            rel_path = path_safety.relative_path(self.root, path)
            if not _matches_glob(rel_path, safe_pattern):
                continue
            if len(matches) >= effective_limit:
                truncated = True
                break
            matches.append(rel_path)
        return {
            "root": str(self.root),
            "pattern": safe_pattern,
            "matches": matches,
            "truncated": truncated,
            "scan_truncated": scan.truncated,
        }

    def read_file_range(
        self,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return self._read_file_range_under_root(
            self.root,
            path,
            start_line=start_line,
            end_line=end_line,
            limit=limit,
        )

    def grep_text(
        self,
        pattern: str,
        *,
        file_glob: str = "**/*",
        search_path: str = ".",
        limit: int | None = None,
        ignore_case: bool = False,
    ) -> dict[str, Any]:
        if not pattern.strip():
            raise RlmToolError("RLM grep pattern is required.")
        safe_glob = self._safe_glob(self.root, file_glob)
        search_root, safe_search_path = self._resolve_directory(self.root, search_path)
        return self._grep_under_root(
            root=self.root,
            search_root=search_root,
            safe_search_path=safe_search_path,
            pattern=pattern,
            file_glob=safe_glob,
            limit=limit,
            ignore_case=ignore_case,
        )

    def load_candidate_asset(self, candidate_id: str) -> dict[str, Any]:
        asset = self._asset(candidate_id)
        return {"candidate_id": candidate_id, "asset": _jsonable(asset)}

    def load_repository_card(self, candidate_id: str) -> dict[str, Any]:
        asset = self._asset(candidate_id)
        card = self._repository_card(str(asset["snapshot_id"]))
        return {"candidate_id": candidate_id, "card": _compact_card(card)}

    def load_candidate_context(self, candidate_id: str) -> dict[str, Any]:
        asset = self._asset(candidate_id)
        repo = catalog.get_repository(str(asset["repo_id"]))
        if repo is None:
            raise RlmToolError(f"Repository metadata is missing for {asset['repo_id']}")
        snapshot = catalog.get_snapshot(str(asset["snapshot_id"]))
        if snapshot is None:
            raise RlmToolError(f"Snapshot metadata is missing for {asset['snapshot_id']}")
        card = catalog.get_repository_card_for_snapshot(str(asset["snapshot_id"]))
        return {
            "candidate_id": candidate_id,
            "asset": _compact_asset(asset),
            "repo": _compact_repo(repo),
            "snapshot": _compact_snapshot(snapshot),
            "card": _compact_card(card) if card is not None else None,
        }

    def load_bundle_manifest(self, candidate_id: str, assessment_id: str) -> dict[str, Any]:
        manifest_path = self._bundle_manifest_path(candidate_id, assessment_id)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RlmToolError(f"Could not read bundle manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise RlmToolError(f"Bundle manifest has invalid shape: {manifest_path}")
        return {
            "candidate_id": candidate_id,
            "assessment_id": assessment_id,
            "manifest_path": str(manifest_path),
            "manifest": _jsonable(manifest),
        }

    def load_repo_map(
        self,
        path: str | None = None,
        *,
        candidate_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        effective_limit = self._limit(limit, self.max_files)
        if candidate_id:
            asset = self._asset(candidate_id)
            root = self._snapshot_root(asset)
            if path:
                root, safe_path = self._resolve_directory(root, path)
            else:
                safe_path = "."
            source = {"kind": "candidate_snapshot", "candidate_id": candidate_id}
        else:
            if path:
                root, safe_path = self._resolve_directory(self.root, path)
            else:
                root = self.root
                safe_path = "."
            source = {"kind": "local_root"}

        built = repo_map.build_repo_map(
            root,
            max_files=max(effective_limit * 2, effective_limit),
            max_symbols=max(effective_limit * 2, effective_limit),
        )
        return {
            "root": str(root),
            "path": safe_path,
            "source": source,
            "entries": [entry.compact() for entry in built.all_entries()[:effective_limit]],
            "file_count": len(built.files),
        }

    def read_candidate_source(
        self,
        candidate_id: str,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        asset = self._asset(candidate_id)
        snapshot_root = self._snapshot_root(asset)
        result = self._read_file_range_under_root(
            snapshot_root,
            path,
            start_line=start_line,
            end_line=end_line,
            limit=limit,
        )
        result["candidate_id"] = candidate_id
        result["root"] = str(snapshot_root)
        return result

    def read_bundle_source(
        self,
        candidate_id: str,
        assessment_id: str,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        source_root = self._bundle_source_root(candidate_id, assessment_id)
        result = self._read_file_range_under_root(
            source_root,
            path,
            start_line=start_line,
            end_line=end_line,
            limit=limit,
        )
        result["candidate_id"] = candidate_id
        result["assessment_id"] = assessment_id
        result["root"] = str(source_root)
        return result

    def load_reuse_loop_report(self, path: str) -> dict[str, Any]:
        report_path, safe_rel = self._resolve_report_file(path)
        try:
            parsed = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RlmToolError(f"Could not read reuse-loop report: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RlmToolError(f"Reuse-loop report has invalid shape: {safe_rel}")
        return {
            "path": safe_rel,
            "report": _compact_reuse_loop_report(parsed),
        }

    def _run_result(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool == "list_files":
            return self.list_files(
                pattern=str(arguments.get("pattern") or "**/*"),
                limit=_optional_int(arguments.get("limit")),
            )
        if tool == "read_file_range":
            return self.read_file_range(
                str(arguments.get("path") or ""),
                start_line=_optional_int(arguments.get("start_line")) or 1,
                end_line=_optional_int(arguments.get("end_line")),
                limit=_optional_int(arguments.get("limit")),
            )
        if tool == "grep_text":
            return self.grep_text(
                str(arguments.get("pattern") or ""),
                file_glob=str(arguments.get("file_glob") or "**/*"),
                search_path=str(arguments.get("search_path") or "."),
                limit=_optional_int(arguments.get("limit")),
                ignore_case=bool(arguments.get("ignore_case", False)),
            )
        if tool == "load_candidate_asset":
            return self.load_candidate_asset(str(arguments.get("candidate_id") or ""))
        if tool == "load_repository_card":
            return self.load_repository_card(str(arguments.get("candidate_id") or ""))
        if tool == "load_candidate_context":
            return self.load_candidate_context(str(arguments.get("candidate_id") or ""))
        if tool == "load_bundle_manifest":
            return self.load_bundle_manifest(
                str(arguments.get("candidate_id") or ""),
                str(arguments.get("assessment_id") or ""),
            )
        if tool == "load_repo_map":
            return self.load_repo_map(
                str(arguments["path"]) if arguments.get("path") else None,
                candidate_id=str(arguments["candidate_id"]) if arguments.get("candidate_id") else None,
                limit=_optional_int(arguments.get("limit")),
            )
        if tool == "read_candidate_source":
            return self.read_candidate_source(
                str(arguments.get("candidate_id") or ""),
                str(arguments.get("path") or ""),
                start_line=_optional_int(arguments.get("start_line")) or 1,
                end_line=_optional_int(arguments.get("end_line")),
                limit=_optional_int(arguments.get("limit")),
            )
        if tool == "read_bundle_source":
            return self.read_bundle_source(
                str(arguments.get("candidate_id") or ""),
                str(arguments.get("assessment_id") or ""),
                str(arguments.get("path") or ""),
                start_line=_optional_int(arguments.get("start_line")) or 1,
                end_line=_optional_int(arguments.get("end_line")),
                limit=_optional_int(arguments.get("limit")),
            )
        if tool == "load_reuse_loop_report":
            return self.load_reuse_loop_report(str(arguments.get("path") or ""))
        raise RlmToolError(f"Unsupported RLM tool: {tool}")

    def _read_file_range_under_root(
        self,
        root: Path,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        resolved, safe_rel = self._resolve_file(root, path)
        if resolved.stat().st_size > self.max_file_bytes:
            raise RlmToolError(f"RLM read target is too large: {safe_rel}")

        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return {
                "path": safe_rel,
                "start_line": 1,
                "end_line": 0,
                "line_count": 0,
                "content": "",
                "truncated": False,
            }

        bounded_start = min(max(1, start_line), len(lines))
        effective_limit = self._limit(limit, self.max_read_lines)
        requested_end = min(max(bounded_start, end_line or len(lines)), len(lines))
        bounded_end = min(requested_end, bounded_start + effective_limit - 1)
        selected = lines[bounded_start - 1 : bounded_end]
        return {
            "path": safe_rel,
            "start_line": bounded_start,
            "end_line": bounded_end,
            "line_count": len(lines),
            "content": "\n".join(
                f"{line_number}|{line}"
                for line_number, line in enumerate(selected, start=bounded_start)
            ),
            "truncated": bounded_end < requested_end,
        }

    def _grep_under_root(
        self,
        *,
        root: Path,
        search_root: Path,
        safe_search_path: str,
        pattern: str,
        file_glob: str,
        limit: int | None,
        ignore_case: bool,
    ) -> dict[str, Any]:
        compiled, regex_mode = _compile_pattern(pattern, ignore_case=ignore_case)
        effective_limit = self._limit(limit, self.max_grep_results)
        scan = self._iter_files(search_root)
        matches: list[dict[str, Any]] = []
        truncated = False

        for path in scan.files:
            rel_path = path_safety.relative_path(root, path)
            if not _matches_glob(rel_path, file_glob):
                continue
            if path.stat().st_size > self.max_file_bytes:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if not compiled.search(line):
                    continue
                if len(matches) >= effective_limit:
                    truncated = True
                    break
                matches.append(
                    {
                        "path": rel_path,
                        "line": line_number,
                        "text": line[:500],
                    }
                )
            if truncated:
                break

        return {
            "pattern": pattern,
            "regex": regex_mode,
            "file_glob": file_glob,
            "search_path": safe_search_path,
            "matches": matches,
            "truncated": truncated,
            "scan_truncated": scan.truncated,
        }

    def _iter_files(self, start: Path) -> "_FileScan":
        files: list[Path] = []
        truncated = False
        for path in start.rglob("*"):
            if not path.is_file() or path_safety.should_skip_path(self._scan_root(start), path):
                continue
            if len(files) >= self.max_scan_files:
                truncated = True
                break
            files.append(path)
        files.sort(key=lambda path: path_safety.relative_path(self._scan_root(start), path))
        return _FileScan(files=files, truncated=truncated)

    def _scan_root(self, start: Path) -> Path:
        try:
            start.resolve().relative_to(self.root)
            return self.root
        except ValueError:
            return start.resolve()

    def _resolve_file(self, root: Path, path: str) -> tuple[Path, str]:
        try:
            resolved, safe_rel = path_safety.resolve_under_root(root, path)
        except path_safety.PathSafetyError as exc:
            raise RlmToolError(str(exc)) from exc
        if not resolved.is_file():
            raise RlmToolError(f"RLM read target is not a file: {safe_rel}")
        return resolved, safe_rel

    def _resolve_report_file(self, path: str) -> tuple[Path, str]:
        cleaned = path_safety.normalize_workspace_reference(self.root, path)
        if not cleaned:
            raise RlmToolError("Report path is required.")
        parts = PurePosixPath(cleaned).parts
        if ".." in parts:
            raise RlmToolError(f"Path escapes snapshot root: {path}")
        if not parts or parts[0] != ".source_scout":
            raise RlmToolError("Reuse-loop reports must live under .source_scout.")
        candidate = (self.root / cleaned).resolve()
        try:
            safe_rel = candidate.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise RlmToolError(f"Path escapes snapshot root: {path}") from exc
        if not candidate.is_file():
            raise RlmToolError(f"RLM report target is not a file: {safe_rel}")
        return candidate, safe_rel

    def _resolve_directory(self, root: Path, path: str) -> tuple[Path, str]:
        try:
            resolved, safe_rel = path_safety.resolve_under_root(root, path)
        except path_safety.PathSafetyError as exc:
            raise RlmToolError(str(exc)) from exc
        if not resolved.is_dir():
            raise RlmToolError(f"RLM search target is not a directory: {safe_rel}")
        return resolved, safe_rel

    def _safe_glob(self, root: Path, pattern: str) -> str:
        try:
            return path_safety.safe_glob_pattern(root, pattern)
        except path_safety.PathSafetyError as exc:
            raise RlmToolError(str(exc)) from exc

    def _limit(self, requested: int | None, maximum: int) -> int:
        if requested is None:
            return maximum
        return min(max(1, requested), maximum)

    def _asset(self, candidate_id: str) -> dict[str, Any]:
        if not candidate_id.strip():
            raise RlmToolError("candidate_id is required.")
        asset = catalog.get_asset_detail(candidate_id)
        if asset is None:
            raise RlmToolError(f"Unknown candidate_id: {candidate_id}")
        return asset

    def _repository_card(self, snapshot_id: str) -> dict[str, Any]:
        card = catalog.get_repository_card_for_snapshot(snapshot_id)
        if card is None:
            raise RlmToolError(f"Repository card is missing for snapshot {snapshot_id}")
        return card

    def _snapshot_root(self, asset: dict[str, Any]) -> Path:
        root = Path(str(asset["snapshot_path"])).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise RlmToolError(f"Snapshot path does not exist: {root}")
        return root

    def _bundle_manifest_path(self, candidate_id: str, assessment_id: str) -> Path:
        if not candidate_id.strip():
            raise RlmToolError("candidate_id is required.")
        if not assessment_id.strip():
            raise RlmToolError("assessment_id is required.")
        try:
            manifest_path = catalog.assessment_bundle_path(candidate_id, assessment_id) / "bundle.json"
        except ValueError as exc:
            raise RlmToolError(str(exc)) from exc
        if not manifest_path.exists():
            raise RlmToolError(f"Bundle manifest not found: {manifest_path}")
        return manifest_path

    def _bundle_source_root(self, candidate_id: str, assessment_id: str) -> Path:
        self._bundle_manifest_path(candidate_id, assessment_id)
        source_root = catalog.assessment_bundle_path(candidate_id, assessment_id) / "source"
        if not source_root.exists() or not source_root.is_dir():
            raise RlmToolError(f"Bundle source root not found: {source_root}")
        return source_root.resolve()


@dataclass(frozen=True)
class _FileScan:
    files: list[Path]
    truncated: bool


def _glob_variants(pattern: str) -> list[str]:
    variants = [pattern]
    if "**/" in pattern:
        variants.append(pattern.replace("**/", ""))
    return variants


def _matches_glob(path: str, pattern: str) -> bool:
    if pattern in {"", ".", "**/*"}:
        return True
    return any(fnmatch.fnmatch(path, variant) for variant in _glob_variants(pattern))


def _compile_pattern(pattern: str, *, ignore_case: bool) -> tuple[re.Pattern[str], bool]:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern, flags=flags), True
    except re.error:
        return re.compile(re.escape(pattern), flags=flags), False


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _compact_asset(asset: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        asset,
        [
            "asset_id",
            "repo_id",
            "snapshot_id",
            "capability",
            "entry_paths",
            "dependency_paths",
            "external_dependencies",
            "evidence_paths",
            "reuse_score",
            "commit_sha",
            "snapshot_path",
            "html_url",
            "synthesis",
        ],
    )


def _compact_repo(repo: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        repo,
        [
            "repo_id",
            "owner",
            "name",
            "html_url",
            "description",
            "detected_languages",
            "topics",
            "stars",
            "license_spdx",
            "repo_size_kb",
            "pushed_at",
            "source_channel",
        ],
    )


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        snapshot,
        [
            "snapshot_id",
            "repo_id",
            "commit_sha",
            "default_branch",
            "snapshot_path",
            "indexed_at",
            "analyzer_version",
        ],
    )


def _compact_card(card: dict[str, Any]) -> dict[str, Any]:
    return _pick(
        card,
        [
            "card_id",
            "snapshot_id",
            "card_version",
            "repo_id",
            "commit_sha",
            "html_url",
            "package_manifests",
            "tree_summary",
            "readme_excerpt",
            "stack_signals",
            "deterministic_features",
            "gemma_profile",
        ],
    )


def _compact_reuse_loop_report(report: dict[str, Any]) -> dict[str, Any]:
    tasks = report.get("tasks", [])
    compact_tasks: list[dict[str, Any]] = []
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict):
                compact_tasks.append(
                    _pick(
                        task,
                        [
                            "id",
                            "task",
                            "task_signature",
                            "returned_candidates",
                            "expected_or_acceptable_repo_in_top_k",
                            "first_expected_or_acceptable_rank",
                            "selected_candidate_id",
                            "selected_repo_id",
                            "selected_is_expected_or_acceptable",
                            "assessment_final_verdict",
                            "reuse_score",
                            "confidence",
                            "evidence_coverage",
                            "bundle_path",
                            "copied_file_count",
                            "missing_file_count",
                            "notable_validation_notes",
                            "assessment_error",
                            "bundle_error",
                        ],
                    )
                )
    return {
        "suite_id": report.get("suite_id"),
        "label": report.get("label"),
        "top_k": report.get("top_k"),
        "passed": report.get("passed"),
        "metrics": _jsonable(report.get("metrics", {})),
        "tasks": compact_tasks,
    }


def _pick(data: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: _jsonable(data[key]) for key in keys if key in data}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
