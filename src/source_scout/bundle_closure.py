from __future__ import annotations

import ast
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .path_safety import PathSafetyError, has_glob_meta, resolve_under_root

DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_SUPPORTING_FILES = 5
DEFAULT_MAX_TOTAL_FILES = 10
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024

_JS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".json")
_JS_SOURCE_EXTENSIONS = frozenset(_JS_EXTENSIONS[:-1])
_PYTHON_SOURCE_EXTENSIONS = frozenset({".py", ".pyi"})

_STATIC_IMPORT_RE = re.compile(
    r"""(?msx)
    ^[ \t]*import[ \t]+(?:type[ \t]+)?
    (?:[^;\"']+?\bfrom[ \t]*)?
    [\"'](?P<specifier>[^\"']+)[\"']
    """
)
_EXPORT_FROM_RE = re.compile(
    r"""(?msx)
    ^[ \t]*export[ \t]+(?:type[ \t]+)?(?:\*(?:[ \t]+as[ \t]+\w+)?|\{[^}]*\})
    [ \t]+from[ \t]*[\"'](?P<specifier>[^\"']+)[\"']
    """
)
_DYNAMIC_IMPORT_RE = re.compile(r"\bimport\s*\(\s*[\"'](?P<specifier>[^\"']+)[\"']\s*\)")
_REQUIRE_RE = re.compile(r"\brequire\s*\(\s*[\"'](?P<specifier>[^\"']+)[\"']\s*\)")


class BundleClosureError(ValueError):
    """Raised when a closure cannot be planned safely within required limits."""


@dataclass(frozen=True)
class UnresolvedLocalImport:
    importer: str
    specifier: str
    reason: str


@dataclass(frozen=True)
class BundleClosurePlan:
    required_files: list[str]
    supporting_files: list[str]
    unresolved_local_imports: list[UnresolvedLocalImport]
    truncated: bool
    warnings: list[str]
    total_bytes: int

    @property
    def files(self) -> list[str]:
        return [*self.required_files, *self.supporting_files]


@dataclass(frozen=True)
class _ImportReference:
    specifier: str
    candidates: tuple[str, ...] = ()
    report_missing: bool = True
    missing_reason: str = "not_found"


def plan_bundle_closure(
    snapshot_root: Path,
    required_seed_paths: Sequence[str],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_supporting_files: int = DEFAULT_MAX_SUPPORTING_FILES,
    max_total_files: int = DEFAULT_MAX_TOTAL_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> BundleClosurePlan:
    """Plan a bounded, read-only source bundle around required seed files."""
    root = snapshot_root.resolve()
    if not root.is_dir():
        raise BundleClosureError(f"Snapshot root is not a directory: {snapshot_root}")
    _validate_limits(
        max_depth=max_depth,
        max_supporting_files=max_supporting_files,
        max_total_files=max_total_files,
        max_total_bytes=max_total_bytes,
    )

    required_files: list[str] = []
    required_paths: dict[str, Path] = {}
    total_bytes = 0
    for seed in required_seed_paths:
        path, relative = _resolve_required_seed(root, seed)
        if relative in required_paths:
            continue
        required_paths[relative] = path
        required_files.append(relative)
        total_bytes += _file_size(path, relative)

    if not required_files:
        raise BundleClosureError("At least one required seed path is required.")
    if len(required_files) > max_total_files:
        raise BundleClosureError(
            "Required seed files exceed the maximum total file count "
            f"({len(required_files)} > {max_total_files})."
        )
    if total_bytes > max_total_bytes:
        raise BundleClosureError(
            f"Required seed files exceed the maximum total byte size ({total_bytes} > {max_total_bytes})."
        )

    supporting_files: list[str] = []
    selected = set(required_files)
    queue = deque((relative, path, 0) for relative, path in required_paths.items())
    unresolved: list[UnresolvedLocalImport] = []
    unresolved_seen: set[tuple[str, str, str]] = set()
    warnings: list[str] = []
    truncated = False

    while queue:
        importer, importer_path, depth = queue.popleft()
        references, parse_warning = _read_imports(root, importer_path, importer)
        if parse_warning is not None:
            _add_warning(warnings, parse_warning)
            continue

        for reference in references:
            dependency, unsafe = _resolve_reference(root, reference)
            if dependency is None:
                if reference.report_missing or unsafe:
                    reason = "unsafe_path" if unsafe else reference.missing_reason
                    _add_unresolved(
                        unresolved,
                        unresolved_seen,
                        importer=importer,
                        specifier=reference.specifier,
                        reason=reason,
                    )
                    if unsafe:
                        _add_warning(
                            warnings,
                            "One or more imports resolved outside the snapshot root and were ignored.",
                        )
                continue

            dependency_path, dependency_relative = dependency
            if dependency_relative in selected:
                continue
            if depth >= max_depth:
                truncated = True
                _add_warning(
                    warnings,
                    f"Dependency depth limit ({max_depth}) reached; deeper files were omitted.",
                )
                continue
            if len(supporting_files) >= max_supporting_files:
                truncated = True
                _add_warning(
                    warnings,
                    f"Supporting file limit ({max_supporting_files}) reached; additional files were omitted.",
                )
                continue
            if len(selected) >= max_total_files:
                truncated = True
                _add_warning(
                    warnings,
                    f"Total file limit ({max_total_files}) reached; additional files were omitted.",
                )
                continue

            dependency_size = _file_size(dependency_path, dependency_relative)
            if total_bytes + dependency_size > max_total_bytes:
                truncated = True
                _add_warning(
                    warnings,
                    f"Total byte limit ({max_total_bytes}) reached; additional files were omitted.",
                )
                continue

            selected.add(dependency_relative)
            supporting_files.append(dependency_relative)
            total_bytes += dependency_size
            queue.append((dependency_relative, dependency_path, depth + 1))

    return BundleClosurePlan(
        required_files=required_files,
        supporting_files=supporting_files,
        unresolved_local_imports=unresolved,
        truncated=truncated,
        warnings=warnings,
        total_bytes=total_bytes,
    )


def _validate_limits(
    *,
    max_depth: int,
    max_supporting_files: int,
    max_total_files: int,
    max_total_bytes: int,
) -> None:
    if max_depth < 0:
        raise BundleClosureError("max_depth must be zero or greater.")
    if max_supporting_files < 0:
        raise BundleClosureError("max_supporting_files must be zero or greater.")
    if max_total_files < 1:
        raise BundleClosureError("max_total_files must be at least one.")
    if max_total_bytes < 1:
        raise BundleClosureError("max_total_bytes must be at least one.")


def _resolve_required_seed(root: Path, seed: str) -> tuple[Path, str]:
    cleaned = seed.strip().replace("\\", "/")
    if not cleaned:
        raise BundleClosureError("Required seed paths cannot be empty.")
    if Path(cleaned).is_absolute() or PurePosixPath(cleaned).is_absolute():
        raise BundleClosureError(f"Required seed path must be relative: {seed}")
    if has_glob_meta(cleaned):
        raise BundleClosureError(f"Required seed path cannot contain a glob: {seed}")
    try:
        path, relative = resolve_under_root(root, cleaned)
    except PathSafetyError as exc:
        raise BundleClosureError(str(exc)) from exc
    if not path.is_file():
        raise BundleClosureError(f"Required seed is not a file: {seed}")
    return path, relative


def _file_size(path: Path, relative: str) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise BundleClosureError(f"Could not inspect source file: {relative}") from exc


def _read_imports(
    root: Path,
    path: Path,
    relative: str,
) -> tuple[list[_ImportReference], str | None]:
    suffix = path.suffix.lower()
    if suffix not in _PYTHON_SOURCE_EXTENSIONS | _JS_SOURCE_EXTENSIONS:
        return [], None
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], f"Could not read imports from {relative}."

    if suffix in _PYTHON_SOURCE_EXTENSIONS:
        try:
            return _python_imports(source, relative, path, root), None
        except SyntaxError:
            return [], f"Could not parse Python imports from {relative}."
    return _javascript_imports(source, relative), None


def _python_imports(
    source: str,
    importer: str,
    importer_path: Path,
    root: Path,
) -> list[_ImportReference]:
    tree = ast.parse(source, filename=str(importer_path))
    references: list[_ImportReference] = []
    nodes = sorted(
        (node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))),
        key=lambda node: (node.lineno, node.col_offset),
    )
    importer_parent = PurePosixPath(importer).parent

    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_path = PurePosixPath(alias.name.replace(".", "/"))
                references.append(
                    _ImportReference(
                        specifier=alias.name,
                        candidates=_python_absolute_candidates(module_path, importer_parent),
                        report_missing=_python_local_top_level_exists(
                            root,
                            module_path,
                            importer_parent,
                        ),
                    )
                )
            continue

        module_parts = PurePosixPath(*(node.module or "").split("."))
        if node.level:
            base = importer_parent
            escaped = False
            for _ in range(node.level - 1):
                if base == PurePosixPath("."):
                    escaped = True
                    break
                base = base.parent
            if escaped:
                references.append(
                    _ImportReference(
                        specifier=_python_display_specifier(node),
                        candidates=("../__outside_snapshot__.py",),
                    )
                )
                continue
        else:
            base = PurePosixPath(".")

        module_path = base / module_parts if node.module else base
        if node.module:
            candidates = (
                _python_module_candidates(module_path)
                if node.level
                else _python_absolute_candidates(module_parts, importer_parent)
            )
            references.append(
                _ImportReference(
                    specifier=_python_display_specifier(node),
                    candidates=candidates,
                    report_missing=bool(node.level)
                    or _python_local_top_level_exists(root, module_parts, importer_parent),
                )
            )
        for alias in node.names:
            if alias.name == "*":
                continue
            alias_path = module_path / alias.name
            alias_candidates = (
                _python_module_candidates(alias_path)
                if node.level
                else _python_absolute_candidates(module_parts / alias.name, importer_parent)
            )
            alias_specifier = (
                _python_display_specifier(node) if node.module else f"{'.' * node.level}{alias.name}"
            )
            references.append(
                _ImportReference(
                    specifier=alias_specifier,
                    candidates=alias_candidates,
                    report_missing=bool(node.level and not node.module),
                )
            )
    return references


def _python_display_specifier(node: ast.ImportFrom) -> str:
    return f"{'.' * node.level}{node.module or ''}"


def _python_module_candidates(module_path: PurePosixPath) -> tuple[str, ...]:
    value = module_path.as_posix()
    if value == ".":
        return ()
    return (f"{value}.py", f"{value}/__init__.py")


def _python_absolute_candidates(
    module_path: PurePosixPath,
    importer_parent: PurePosixPath,
) -> tuple[str, ...]:
    candidates = list(_python_module_candidates(module_path))
    if importer_parent != PurePosixPath("."):
        candidates.extend(_python_module_candidates(importer_parent / module_path))
    return tuple(dict.fromkeys(candidates))


def _python_local_top_level_exists(
    root: Path,
    module_path: PurePosixPath,
    importer_parent: PurePosixPath,
) -> bool:
    if not module_path.parts:
        return False
    top_level = PurePosixPath(module_path.parts[0])
    bases = [PurePosixPath("."), importer_parent]
    for base in bases:
        for candidate in (base / f"{top_level}.py", base / top_level):
            candidate_path = root / Path(*candidate.parts)
            try:
                path, _ = resolve_under_root(root, str(candidate_path.resolve()))
            except PathSafetyError:
                continue
            if path.exists():
                return True
    return False


def _javascript_imports(source: str, importer: str) -> list[_ImportReference]:
    matches: list[tuple[int, str]] = []
    for pattern in (_STATIC_IMPORT_RE, _EXPORT_FROM_RE, _DYNAMIC_IMPORT_RE, _REQUIRE_RE):
        matches.extend((match.start(), match.group("specifier")) for match in pattern.finditer(source))
    matches.sort(key=lambda item: item[0])

    references: list[_ImportReference] = []
    seen: set[str] = set()
    for _, specifier in matches:
        if specifier in seen:
            continue
        seen.add(specifier)
        if specifier.startswith(("./", "../")):
            base = PurePosixPath(importer).parent / specifier
            references.append(
                _ImportReference(
                    specifier=specifier,
                    candidates=_javascript_candidates(base, PurePosixPath(importer).suffix),
                )
            )
        elif specifier.startswith(("@/", "~/", "#/")):
            references.append(
                _ImportReference(
                    specifier=specifier,
                    missing_reason="path_alias_not_supported",
                )
            )
    return references


def _javascript_candidates(base: PurePosixPath, importer_suffix: str) -> tuple[str, ...]:
    candidates = [base.as_posix()]
    if not base.suffix:
        extensions = list(_JS_EXTENSIONS)
        if importer_suffix in {".js", ".jsx", ".mjs", ".cjs"}:
            extensions.sort(key=lambda extension: extension not in {".js", ".jsx", ".mjs", ".cjs"})
        candidates.extend(f"{base.as_posix()}{extension}" for extension in extensions)
        candidates.extend(f"{base.as_posix()}/index{extension}" for extension in extensions)
    return tuple(candidates)


def _resolve_reference(
    root: Path,
    reference: _ImportReference,
) -> tuple[tuple[Path, str] | None, bool]:
    unsafe = False
    for candidate in reference.candidates:
        candidate_path = root / Path(*PurePosixPath(candidate).parts)
        try:
            path, relative = resolve_under_root(root, str(candidate_path.resolve()))
        except PathSafetyError:
            unsafe = True
            continue
        if path.is_file():
            return (path, relative), unsafe
    return None, unsafe


def _add_unresolved(
    unresolved: list[UnresolvedLocalImport],
    seen: set[tuple[str, str, str]],
    *,
    importer: str,
    specifier: str,
    reason: str,
) -> None:
    key = (importer, specifier, reason)
    if key in seen:
        return
    seen.add(key)
    unresolved.append(UnresolvedLocalImport(importer=importer, specifier=specifier, reason=reason))


def _add_warning(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)
