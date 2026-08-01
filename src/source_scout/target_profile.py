from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import SKIP_DIRS
from .path_safety import EXTRA_SKIP_DIRS

PROFILE_VERSION = "target-profile-v1"
MAX_MANIFEST_BYTES = 2_000_000

SOURCE_SUFFIX_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
}
SOURCE_ROOT_NAMES = {"app", "components", "lib", "pages", "src"}
TEST_ROOT_NAMES = {"__tests__", "spec", "specs", "test", "tests"}
DEV_GROUP_TERMS = {"build", "dev", "docs", "lint", "qa", "test", "tests", "type", "types"}
SKIPPED_DIRS = SKIP_DIRS | EXTRA_SKIP_DIRS | {"generated", "vendor"}

LOCKFILE_MANAGERS = {
    "bun.lock": "bun",
    "bun.lockb": "bun",
    "conda-lock.yml": "conda",
    "conda-lock.yaml": "conda",
    "npm-shrinkwrap.json": "npm",
    "package-lock.json": "npm",
    "pdm.lock": "pdm",
    "pipfile.lock": "pipenv",
    "pixi.lock": "pixi",
    "pnpm-lock.yaml": "pnpm",
    "pnpm-lock.yml": "pnpm",
    "poetry.lock": "poetry",
    "pylock.toml": "pip",
    "uv.lock": "uv",
    "yarn.lock": "yarn",
}

FRAMEWORK_DEPENDENCIES = {
    "@nestjs/core": "nestjs",
    "@sveltejs/kit": "sveltekit",
    "django": "django",
    "express": "express",
    "fastapi": "fastapi",
    "fastify": "fastify",
    "flask": "flask",
    "hono": "hono",
    "next": "nextjs",
    "nuxt": "nuxt",
    "react": "react",
    "starlette": "starlette",
    "svelte": "svelte",
    "vue": "vue",
}
TEST_DEPENDENCIES = {
    "@playwright/test": "playwright",
    "@testing-library/react": "testing-library",
    "ava": "ava",
    "cypress": "cypress",
    "hypothesis": "hypothesis",
    "jest": "jest",
    "mocha": "mocha",
    "nose2": "nose2",
    "pytest": "pytest",
    "vitest": "vitest",
}

_PYTHON_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<remainder>\[[^\]]+\])?(?P<constraint>.*)$"
)
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")
_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")


class TargetProfileError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class DependencySpec:
    ecosystem: str
    name: str
    constraint: str
    manifest_path: str

    def to_jsonable(self) -> dict[str, str]:
        return {
            "ecosystem": self.ecosystem,
            "name": self.name,
            "constraint": self.constraint,
            "manifest_path": self.manifest_path,
        }


@dataclass(frozen=True)
class TargetProfileV1:
    languages: tuple[str, ...]
    source_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    package_managers: tuple[str, ...]
    manifest_paths: tuple[str, ...]
    runtime_dependencies: tuple[DependencySpec, ...]
    dev_dependencies: tuple[DependencySpec, ...]
    framework_signals: tuple[str, ...]
    test_signals: tuple[str, ...]
    node_module_format: str | None
    has_tsconfig: bool
    profile_version: str = PROFILE_VERSION

    def canonical_jsonable(self) -> dict[str, Any]:
        return {
            "profile_version": self.profile_version,
            "languages": sorted(set(self.languages)),
            "source_roots": sorted(set(self.source_roots)),
            "test_roots": sorted(set(self.test_roots)),
            "package_managers": sorted(set(self.package_managers)),
            "manifest_paths": sorted(set(self.manifest_paths)),
            "runtime_dependencies": [
                item.to_jsonable() for item in sorted(set(self.runtime_dependencies))
            ],
            "dev_dependencies": [
                item.to_jsonable() for item in sorted(set(self.dev_dependencies))
            ],
            "framework_signals": sorted(set(self.framework_signals)),
            "test_signals": sorted(set(self.test_signals)),
            "node_module_format": self.node_module_format,
            "has_tsconfig": self.has_tsconfig,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_jsonable(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def to_jsonable(self) -> dict[str, Any]:
        return {**self.canonical_jsonable(), "fingerprint": self.fingerprint}


@dataclass
class _ProfileAccumulator:
    languages: set[str]
    source_roots: set[str]
    test_roots: set[str]
    package_managers: set[str]
    manifest_paths: set[str]
    runtime_dependencies: set[DependencySpec]
    dev_dependencies: set[DependencySpec]
    framework_signals: set[str]
    test_signals: set[str]
    node_formats: set[str]
    has_node_source: bool = False
    has_node_manifest: bool = False
    has_tsconfig: bool = False

    @classmethod
    def empty(cls) -> _ProfileAccumulator:
        return cls(set(), set(), set(), set(), set(), set(), set(), set(), set(), set())


def build_target_profile(project_path: str | Path) -> TargetProfileV1:
    root = _validated_project_root(project_path)
    files = list(_project_files(root))
    relative_paths = {rel_path for _path, rel_path in files}
    accumulator = _ProfileAccumulator.empty()

    for path, rel_path in files:
        _record_file_shape(accumulator, rel_path)
        if _is_manifest_path(rel_path):
            accumulator.manifest_paths.add(rel_path)

        name = path.name.lower()
        if name == "package.json":
            accumulator.has_node_manifest = True
            _parse_package_json(path, rel_path, accumulator)
        elif name in {"package-lock.json", "npm-shrinkwrap.json"}:
            accumulator.has_node_manifest = True
            accumulator.package_managers.add("npm")
            sibling_package = (PurePosixPath(rel_path).parent / "package.json").as_posix()
            if sibling_package not in relative_paths:
                _parse_package_lock(path, rel_path, accumulator)
        elif name == "pyproject.toml":
            _parse_pyproject(path, rel_path, accumulator)
        elif name == "pipfile":
            accumulator.package_managers.add("pipenv")
            _parse_pipfile(path, rel_path, accumulator)
        elif name == "pipfile.lock":
            accumulator.package_managers.add("pipenv")
            sibling_pipfile = (PurePosixPath(rel_path).parent / "Pipfile").as_posix()
            if sibling_pipfile not in relative_paths:
                _parse_pipfile_lock(path, rel_path, accumulator)
        elif _is_requirements_path(rel_path):
            accumulator.package_managers.add("pip")
            _parse_requirements(path, rel_path, accumulator)

        manager = LOCKFILE_MANAGERS.get(name)
        if manager:
            accumulator.package_managers.add(manager)
        if name in {"environment.yml", "environment.yaml"}:
            accumulator.package_managers.add("conda")

    node_managers = {"bun", "npm", "pnpm", "yarn"}
    if accumulator.has_node_manifest and not accumulator.package_managers & node_managers:
        accumulator.package_managers.add("npm")

    dependency_names = {
        item.name for item in accumulator.runtime_dependencies | accumulator.dev_dependencies
    }
    accumulator.framework_signals.update(
        signal for name, signal in FRAMEWORK_DEPENDENCIES.items() if name in dependency_names
    )
    accumulator.test_signals.update(
        signal for name, signal in TEST_DEPENDENCIES.items() if name in dependency_names
    )
    if accumulator.test_roots:
        accumulator.test_signals.add("tests-present")

    return TargetProfileV1(
        languages=tuple(sorted(accumulator.languages)),
        source_roots=tuple(sorted(accumulator.source_roots)),
        test_roots=tuple(sorted(accumulator.test_roots)),
        package_managers=tuple(sorted(accumulator.package_managers)),
        manifest_paths=tuple(sorted(accumulator.manifest_paths)),
        runtime_dependencies=tuple(sorted(accumulator.runtime_dependencies)),
        dev_dependencies=tuple(sorted(accumulator.dev_dependencies)),
        framework_signals=tuple(sorted(accumulator.framework_signals)),
        test_signals=tuple(sorted(accumulator.test_signals)),
        node_module_format=_node_module_format(accumulator),
        has_tsconfig=accumulator.has_tsconfig,
    )


def npm_unambiguous_major(constraint: str) -> int | None:
    value = constraint.strip()
    if not value or value in {"*", "latest"}:
        return None

    simple = re.fullmatch(
        r"(?:[~^=])?\s*v?(?P<major>\d+)(?:\.\d+){0,2}(?:-[0-9A-Za-z.-]+)?",
        value,
    )
    return int(simple.group("major")) if simple else None


def _validated_project_root(project_path: str | Path) -> Path:
    if isinstance(project_path, str) and not project_path.strip():
        raise TargetProfileError("project_path is required.")
    raw_path = Path(project_path).expanduser()
    if raw_path.is_symlink():
        raise TargetProfileError(f"Target project path must not be a symlink: {project_path}")
    if not raw_path.exists():
        raise TargetProfileError(f"Target project path does not exist: {project_path}")
    if not raw_path.is_dir():
        raise TargetProfileError(f"Target project path must be a directory: {project_path}")
    try:
        return raw_path.resolve(strict=True)
    except OSError as exc:
        raise TargetProfileError(f"Could not resolve target project path: {project_path}") from exc


def _project_files(root: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for current_root_raw, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_root = Path(current_root_raw)
        dirnames[:] = sorted(
            dirname
            for dirname in dirnames
            if dirname not in SKIPPED_DIRS and not (current_root / dirname).is_symlink()
        )
        for filename in sorted(filenames):
            path = current_root / filename
            if path.is_symlink() or not path.is_file():
                continue
            rel_path = path.relative_to(root).as_posix()
            files.append((path, rel_path))
    return files


def _record_file_shape(accumulator: _ProfileAccumulator, rel_path: str) -> None:
    pure_path = PurePosixPath(rel_path)
    suffix = pure_path.suffix.lower()
    language = SOURCE_SUFFIX_LANGUAGES.get(suffix)
    if language:
        accumulator.languages.add(language)
        is_test = _is_test_source_path(rel_path)
        if is_test:
            accumulator.test_roots.add(_test_root(rel_path))
        else:
            accumulator.source_roots.add(_source_root(rel_path))
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}:
        accumulator.has_node_source = True
    if suffix in {".mjs", ".mts"}:
        accumulator.node_formats.add("esm")
    elif suffix in {".cjs", ".cts"}:
        accumulator.node_formats.add("commonjs")
    if pure_path.name.lower().startswith("tsconfig") and suffix == ".json":
        accumulator.has_tsconfig = True


def _source_root(rel_path: str) -> str:
    parts = PurePosixPath(rel_path).parts[:-1]
    if not parts:
        return "."
    for index, part in enumerate(parts):
        if part.lower() in SOURCE_ROOT_NAMES:
            return "/".join(parts[: index + 1])
    return parts[0]


def _test_root(rel_path: str) -> str:
    parts = PurePosixPath(rel_path).parts[:-1]
    if not parts:
        return "."
    for index, part in enumerate(parts):
        if part.lower() in TEST_ROOT_NAMES:
            return "/".join(parts[: index + 1])
    return "/".join(parts)


def _is_test_source_path(rel_path: str) -> bool:
    pure_path = PurePosixPath(rel_path)
    lower_parts = {part.lower() for part in pure_path.parts[:-1]}
    name = pure_path.name.lower()
    return bool(lower_parts & TEST_ROOT_NAMES) or (
        name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def _is_manifest_path(rel_path: str) -> bool:
    name = PurePosixPath(rel_path).name.lower()
    return (
        name in LOCKFILE_MANAGERS
        or name
        in {
            "environment.yml",
            "environment.yaml",
            "package.json",
            "pipfile",
            "pyproject.toml",
        }
        or _is_requirements_path(rel_path)
    )


def _is_requirements_path(rel_path: str) -> bool:
    path = PurePosixPath(rel_path)
    name = path.name.lower()
    suffix = path.suffix.lower()
    return (
        name.startswith("requirements") and suffix in {".in", ".lock", ".txt"}
    ) or ("requirements" in {part.lower() for part in path.parts[:-1]} and suffix in {".in", ".txt"})


def _read_manifest_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_json_object(path: Path) -> dict[str, Any] | None:
    raw = _read_manifest_text(path)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_toml_object(path: Path) -> dict[str, Any] | None:
    raw = _read_manifest_text(path)
    if raw is None:
        return None
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_package_json(
    path: Path,
    rel_path: str,
    accumulator: _ProfileAccumulator,
) -> None:
    parsed = _read_json_object(path)
    if parsed is None:
        return
    manager_spec = parsed.get("packageManager")
    if isinstance(manager_spec, str):
        match = re.match(r"^(npm|pnpm|yarn|bun)@", manager_spec.strip(), re.IGNORECASE)
        if match:
            accumulator.package_managers.add(match.group(1).lower())
    package_type = str(parsed.get("type", "")).strip().lower()
    if package_type == "module":
        accumulator.node_formats.add("esm")
    elif package_type == "commonjs":
        accumulator.node_formats.add("commonjs")

    for section in ("dependencies", "optionalDependencies", "peerDependencies"):
        _add_npm_dependencies(
            accumulator.runtime_dependencies,
            parsed.get(section),
            manifest_path=rel_path,
        )
    _add_npm_dependencies(
        accumulator.dev_dependencies,
        parsed.get("devDependencies"),
        manifest_path=rel_path,
    )

    scripts = parsed.get("scripts")
    if isinstance(scripts, dict):
        script_text = " ".join(str(value).lower() for value in scripts.values())
        accumulator.test_signals.update(
            signal for dependency, signal in TEST_DEPENDENCIES.items() if dependency in script_text
        )


def _add_npm_dependencies(
    destination: set[DependencySpec],
    raw_dependencies: Any,
    *,
    manifest_path: str,
) -> None:
    if not isinstance(raw_dependencies, dict):
        return
    for raw_name, raw_constraint in raw_dependencies.items():
        if not isinstance(raw_name, str) or not isinstance(raw_constraint, str):
            continue
        name = raw_name.strip().lower()
        constraint = _safe_constraint(raw_constraint)
        if name:
            destination.add(DependencySpec("npm", name, constraint, manifest_path))


def _parse_package_lock(
    path: Path,
    rel_path: str,
    accumulator: _ProfileAccumulator,
) -> None:
    parsed = _read_json_object(path)
    if parsed is None:
        return
    packages = parsed.get("packages")
    root_package = packages.get("") if isinstance(packages, dict) else None
    if not isinstance(root_package, dict):
        return
    _add_npm_dependencies(
        accumulator.runtime_dependencies,
        root_package.get("dependencies"),
        manifest_path=rel_path,
    )
    _add_npm_dependencies(
        accumulator.dev_dependencies,
        root_package.get("devDependencies"),
        manifest_path=rel_path,
    )


def _parse_pyproject(path: Path, rel_path: str, accumulator: _ProfileAccumulator) -> None:
    parsed = _read_toml_object(path)
    if parsed is None:
        return
    project = parsed.get("project")
    if isinstance(project, dict):
        _add_python_requirement_list(
            accumulator.runtime_dependencies,
            project.get("dependencies"),
            manifest_path=rel_path,
        )
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group_name, requirements in optional.items():
                destination = (
                    accumulator.dev_dependencies
                    if _is_dev_group(str(group_name))
                    else accumulator.runtime_dependencies
                )
                _add_python_requirement_list(destination, requirements, manifest_path=rel_path)

    dependency_groups = parsed.get("dependency-groups")
    if isinstance(dependency_groups, dict):
        for requirements in dependency_groups.values():
            _add_python_requirement_list(
                accumulator.dev_dependencies,
                requirements,
                manifest_path=rel_path,
            )

    tool = parsed.get("tool")
    tool = tool if isinstance(tool, dict) else {}
    if "pytest" in tool:
        accumulator.test_signals.add("pytest")
    if "uv" in tool:
        accumulator.package_managers.add("uv")
        uv = tool.get("uv")
        if isinstance(uv, dict):
            _add_python_requirement_list(
                accumulator.dev_dependencies,
                uv.get("dev-dependencies"),
                manifest_path=rel_path,
            )
    elif "poetry" in tool:
        accumulator.package_managers.add("poetry")
    elif "pdm" in tool:
        accumulator.package_managers.add("pdm")
    elif "rye" in tool:
        accumulator.package_managers.add("rye")
    else:
        accumulator.package_managers.add("pip")

    poetry = tool.get("poetry")
    if isinstance(poetry, dict):
        _add_poetry_dependencies(
            accumulator.runtime_dependencies,
            poetry.get("dependencies"),
            manifest_path=rel_path,
            skip_python=True,
        )
        _add_poetry_dependencies(
            accumulator.dev_dependencies,
            poetry.get("dev-dependencies"),
            manifest_path=rel_path,
        )
        groups = poetry.get("group")
        if isinstance(groups, dict):
            for group in groups.values():
                if isinstance(group, dict):
                    _add_poetry_dependencies(
                        accumulator.dev_dependencies,
                        group.get("dependencies"),
                        manifest_path=rel_path,
                    )

    pdm = tool.get("pdm")
    if isinstance(pdm, dict):
        dev_dependencies = pdm.get("dev-dependencies")
        if isinstance(dev_dependencies, dict):
            for requirements in dev_dependencies.values():
                _add_python_requirement_list(
                    accumulator.dev_dependencies,
                    requirements,
                    manifest_path=rel_path,
                )


def _add_python_requirement_list(
    destination: set[DependencySpec],
    raw_requirements: Any,
    *,
    manifest_path: str,
) -> None:
    if not isinstance(raw_requirements, list):
        return
    for raw_requirement in raw_requirements:
        if not isinstance(raw_requirement, str):
            continue
        dependency = _python_dependency(raw_requirement, manifest_path)
        if dependency is not None:
            destination.add(dependency)


def _add_poetry_dependencies(
    destination: set[DependencySpec],
    raw_dependencies: Any,
    *,
    manifest_path: str,
    skip_python: bool = False,
) -> None:
    if not isinstance(raw_dependencies, dict):
        return
    for raw_name, raw_constraint in raw_dependencies.items():
        name = _normalize_python_name(str(raw_name))
        if not name or (skip_python and name == "python"):
            continue
        constraint = _poetry_constraint(raw_constraint)
        destination.add(DependencySpec("pypi", name, constraint, manifest_path))


def _poetry_constraint(raw_constraint: Any) -> str:
    if isinstance(raw_constraint, str):
        return _safe_constraint(raw_constraint)
    if not isinstance(raw_constraint, dict):
        return ""
    version = raw_constraint.get("version")
    parts = [str(version).strip()] if isinstance(version, str) else []
    markers = raw_constraint.get("markers")
    if isinstance(markers, str) and markers.strip():
        parts.append(f"; {markers.strip()}")
    if not parts and "path" in raw_constraint:
        return "local-path"
    if not parts and ("git" in raw_constraint or "url" in raw_constraint):
        return "direct-reference"
    return " ".join(parts)


def _safe_constraint(raw_constraint: str) -> str:
    value = raw_constraint.strip()
    lowered = value.lower()
    if lowered.startswith(("file:", "link:")) or _ABSOLUTE_PATH_RE.match(value):
        return "local-path"
    if lowered.startswith(("git+", "git://", "http://", "https://", "ssh://")):
        return "direct-reference"
    return value


def _parse_requirements(
    path: Path,
    rel_path: str,
    accumulator: _ProfileAccumulator,
) -> None:
    raw = _read_manifest_text(path)
    if raw is None:
        return
    destination = (
        accumulator.dev_dependencies
        if _is_dev_group(PurePosixPath(rel_path).stem)
        else accumulator.runtime_dependencies
    )
    for line in raw.splitlines():
        stripped = _INLINE_COMMENT_RE.sub("", line).strip()
        if not stripped or stripped.startswith(("#", "-", "git+", "http://", "https://")):
            continue
        dependency = _python_dependency(stripped, rel_path)
        if dependency is not None:
            destination.add(dependency)


def _parse_pipfile(path: Path, rel_path: str, accumulator: _ProfileAccumulator) -> None:
    parsed = _read_toml_object(path)
    if parsed is None:
        return
    _add_pipfile_dependencies(
        accumulator.runtime_dependencies,
        parsed.get("packages"),
        manifest_path=rel_path,
    )
    _add_pipfile_dependencies(
        accumulator.dev_dependencies,
        parsed.get("dev-packages"),
        manifest_path=rel_path,
    )


def _parse_pipfile_lock(path: Path, rel_path: str, accumulator: _ProfileAccumulator) -> None:
    parsed = _read_json_object(path)
    if parsed is None:
        return
    _add_pipfile_dependencies(
        accumulator.runtime_dependencies,
        parsed.get("default"),
        manifest_path=rel_path,
    )
    _add_pipfile_dependencies(
        accumulator.dev_dependencies,
        parsed.get("develop"),
        manifest_path=rel_path,
    )


def _add_pipfile_dependencies(
    destination: set[DependencySpec],
    raw_dependencies: Any,
    *,
    manifest_path: str,
) -> None:
    if not isinstance(raw_dependencies, dict):
        return
    for raw_name, raw_constraint in raw_dependencies.items():
        name = _normalize_python_name(str(raw_name))
        if not name:
            continue
        constraint = _poetry_constraint(raw_constraint)
        destination.add(DependencySpec("pypi", name, constraint, manifest_path))


def _python_dependency(raw_requirement: str, manifest_path: str) -> DependencySpec | None:
    match = _PYTHON_REQUIREMENT_RE.match(raw_requirement.strip())
    if match is None:
        return None
    name = _normalize_python_name(match.group("name"))
    if not name:
        return None
    constraint = f"{match.group('remainder') or ''}{match.group('constraint') or ''}".strip()
    if constraint.startswith("@"):
        constraint = "direct-reference"
    return DependencySpec("pypi", name, constraint, manifest_path)


def _normalize_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _is_dev_group(group_name: str) -> bool:
    tokens = set(re.split(r"[-_.]+", group_name.strip().lower()))
    return bool(tokens & DEV_GROUP_TERMS)


def _node_module_format(accumulator: _ProfileAccumulator) -> str | None:
    if len(accumulator.node_formats) > 1:
        return "mixed"
    if accumulator.node_formats:
        return next(iter(accumulator.node_formats))
    if accumulator.has_node_source or accumulator.has_node_manifest:
        return "unspecified"
    return None


__all__ = [
    "DependencySpec",
    "TargetProfileError",
    "TargetProfileV1",
    "build_target_profile",
    "npm_unambiguous_major",
]
