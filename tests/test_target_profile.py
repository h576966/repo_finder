import json
import os
from collections.abc import Iterable
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from source_scout.target_profile import (
    DependencySpec,
    TargetProfileError,
    TargetProfileV1,
    build_target_profile,
    npm_unambiguous_major,
)


def _constraints(
    profile_dependencies: Iterable[DependencySpec],
) -> dict[tuple[str, str, str], str]:
    return {
        (dependency.ecosystem, dependency.name, dependency.manifest_path): dependency.constraint
        for dependency in profile_dependencies
    }


def test_build_target_profile_collects_canonical_target_metadata(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.tsx").write_text(
        "export const SOURCE_SECRET = 'must-not-be-profiled'\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text("def test_api(): pass\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "type": "module",
                "packageManager": "pnpm@10.0.0",
                "dependencies": {
                    "local-lib": f"file:{tmp_path / 'private-lib'}",
                    "next": "^16.0.0",
                    "react": "19.2.0",
                },
                "devDependencies": {"typescript": "~6.0.0", "vitest": "^4.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                "name = 'target'",
                "dependencies = ['FastAPI>=0.115,<1', 'Pydantic[email]~=2.10']",
                "",
                "[project.optional-dependencies]",
                "postgres = ['asyncpg>=0.30']",
                "test = ['pytest>=9']",
                "",
                "[dependency-groups]",
                "lint = ['ruff==0.15.4']",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    profile = build_target_profile(tmp_path)

    assert profile.profile_version == "target-profile-v1"
    assert profile.languages == ("python", "typescript")
    assert profile.source_roots == ("src",)
    assert profile.test_roots == ("tests",)
    assert profile.package_managers == ("pip", "pnpm")
    assert profile.manifest_paths == (
        "package.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
    )
    assert profile.framework_signals == ("fastapi", "nextjs", "react")
    assert profile.test_signals == ("pytest", "tests-present", "vitest")
    assert profile.node_module_format == "esm"
    assert profile.has_tsconfig is True

    runtime = _constraints(profile.runtime_dependencies)
    dev = _constraints(profile.dev_dependencies)
    assert runtime[("npm", "next", "package.json")] == "^16.0.0"
    assert runtime[("npm", "local-lib", "package.json")] == "local-path"
    assert runtime[("pypi", "fastapi", "pyproject.toml")] == ">=0.115,<1"
    assert runtime[("pypi", "pydantic", "pyproject.toml")] == "[email]~=2.10"
    assert runtime[("pypi", "asyncpg", "pyproject.toml")] == ">=0.30"
    assert dev[("npm", "typescript", "package.json")] == "~6.0.0"
    assert dev[("pypi", "pytest", "pyproject.toml")] == ">=9"
    assert dev[("pypi", "ruff", "pyproject.toml")] == "==0.15.4"

    payload = profile.to_jsonable()
    encoded = json.dumps(payload, sort_keys=True)
    assert payload["fingerprint"] == profile.fingerprint
    assert str(tmp_path) not in encoded
    assert "must-not-be-profiled" not in encoded
    assert build_target_profile(tmp_path).fingerprint == profile.fingerprint

    with pytest.raises(FrozenInstanceError):
        profile.has_tsconfig = False  # type: ignore[misc]


def test_target_profile_fingerprint_is_canonical_across_tuple_order() -> None:
    dependency_a = DependencySpec("npm", "react", "^19", "package.json")
    dependency_b = DependencySpec("pypi", "fastapi", ">=0.115", "pyproject.toml")
    profile_a = TargetProfileV1(
        languages=("typescript", "python"),
        source_roots=("src",),
        test_roots=(),
        package_managers=("pnpm", "uv"),
        manifest_paths=("pyproject.toml", "package.json"),
        runtime_dependencies=(dependency_a, dependency_b),
        dev_dependencies=(),
        framework_signals=("react", "fastapi"),
        test_signals=(),
        node_module_format="esm",
        has_tsconfig=True,
    )
    profile_b = TargetProfileV1(
        languages=tuple(reversed(profile_a.languages)),
        source_roots=profile_a.source_roots,
        test_roots=profile_a.test_roots,
        package_managers=tuple(reversed(profile_a.package_managers)),
        manifest_paths=tuple(reversed(profile_a.manifest_paths)),
        runtime_dependencies=tuple(reversed(profile_a.runtime_dependencies)),
        dev_dependencies=profile_a.dev_dependencies,
        framework_signals=tuple(reversed(profile_a.framework_signals)),
        test_signals=profile_a.test_signals,
        node_module_format=profile_a.node_module_format,
        has_tsconfig=profile_a.has_tsconfig,
    )

    assert profile_a.canonical_jsonable() == profile_b.canonical_jsonable()
    assert profile_a.fingerprint == profile_b.fingerprint


def test_build_target_profile_parses_requirements_and_lock_fallbacks(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "Requests[socks]>=2.31; python_version >= '3.11'\n"
        "-r requirements-dev.txt\n"
        "https://example.invalid/archive.whl\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements-dev.txt").write_text("pytest~=9.1  # runner\n", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "dependencies": {"hono": "4.7.0"},
                        "devDependencies": {"jest": "30.0.0"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    profile = build_target_profile(tmp_path)

    runtime = _constraints(profile.runtime_dependencies)
    dev = _constraints(profile.dev_dependencies)
    assert profile.package_managers == ("npm", "pip")
    assert runtime[("pypi", "requests", "requirements.txt")] == (
        "[socks]>=2.31; python_version >= '3.11'"
    )
    assert runtime[("npm", "hono", "package-lock.json")] == "4.7.0"
    assert dev[("pypi", "pytest", "requirements-dev.txt")] == "~=9.1"
    assert dev[("npm", "jest", "package-lock.json")] == "30.0.0"
    assert profile.framework_signals == ("hono",)
    assert profile.test_signals == ("jest", "pytest")
    assert profile.node_module_format == "unspecified"


def test_build_target_profile_does_not_follow_symlinked_content(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "package.json").write_text(
        json.dumps({"dependencies": {"secret-framework": "99.0.0"}}),
        encoding="utf-8",
    )
    try:
        os.symlink(outside, project / "linked", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")

    profile = build_target_profile(project)

    assert profile.manifest_paths == ()
    assert profile.runtime_dependencies == ()
    assert "linked/package.json" not in json.dumps(profile.to_jsonable())


def test_build_target_profile_reports_invalid_paths(tmp_path: Path) -> None:
    with pytest.raises(TargetProfileError, match="project_path is required"):
        build_target_profile("  ")
    with pytest.raises(TargetProfileError, match="does not exist"):
        build_target_profile(tmp_path / "missing")

    file_path = tmp_path / "file.txt"
    file_path.write_text("not a project", encoding="utf-8")
    with pytest.raises(TargetProfileError, match="must be a directory"):
        build_target_profile(file_path)


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [
        ("19.2.0", 19),
        ("^19.0.0", 19),
        ("~19.1.0", 19),
        ("workspace:~19.1.0", None),
        (">=19.0.0 <20.0.0", None),
        ("19.0.0 - 19.9.0", None),
        ("^19 || ~19.2", None),
        (">=19", None),
        (">=19 <21", None),
        ("latest", None),
        ("^18 || ^19", None),
    ],
)
def test_npm_unambiguous_major(constraint: str, expected: int | None) -> None:
    assert npm_unambiguous_major(constraint) == expected
