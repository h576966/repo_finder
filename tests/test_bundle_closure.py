from pathlib import Path

import pytest

from source_scout.bundle_closure import BundleClosureError, plan_bundle_closure


def _write(root: Path, relative: str, content: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_python_imports_follow_two_levels_and_report_missing(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    _write(
        root,
        "pkg/main.py",
        "from .service import run\nimport os\nfrom . import missing\n",
    )
    _write(root, "pkg/service.py", "from .helper import value\n")
    _write(root, "pkg/helper.py", "from . import deeper\nvalue = 1\n")
    _write(root, "pkg/deeper.py", "value = 2\n")

    plan = plan_bundle_closure(root, ["pkg/main.py"])

    assert plan.required_files == ["pkg/main.py"]
    assert plan.supporting_files == ["pkg/service.py", "pkg/helper.py"]
    assert [(item.importer, item.specifier, item.reason) for item in plan.unresolved_local_imports] == [
        ("pkg/main.py", ".missing", "not_found")
    ]
    assert plan.truncated is True
    assert any("depth limit" in warning.lower() for warning in plan.warnings)
    assert plan.total_bytes == sum((root / path).stat().st_size for path in plan.files)


def test_python_absolute_imports_resolve_from_root_or_importer_directory(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    _write(root, "main.py", "import package.module\n")
    _write(root, "package/module.py")
    _write(root, "scripts/tool.py", "import sibling\nimport local.missing\n")
    _write(root, "scripts/sibling.py")
    (root / "local").mkdir()

    plan = plan_bundle_closure(root, ["main.py", "scripts/tool.py"])

    assert plan.supporting_files == ["package/module.py", "scripts/sibling.py"]
    assert [(item.specifier, item.reason) for item in plan.unresolved_local_imports] == [
        ("local.missing", "not_found")
    ]


def test_typescript_import_forms_and_directory_index_resolution(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    _write(
        root,
        "src/main.ts",
        "\n".join(
            [
                'import type {\n  Shape\n} from "./types";',
                'export { feature } from "./feature";',
                'const lazy = import("./lazy");',
                'const utility = require("./utility");',
            ]
        ),
    )
    _write(root, "src/types.ts", "export type Shape = string;\n")
    _write(root, "src/feature.ts", 'export * from "./parts";\n')
    _write(root, "src/lazy.ts", "export const lazy = true;\n")
    _write(root, "src/utility.js", "exports.utility = true;\n")
    _write(root, "src/parts/index.ts", "export const feature = true;\n")

    plan = plan_bundle_closure(root, ["src/main.ts"])

    assert plan.supporting_files == [
        "src/types.ts",
        "src/feature.ts",
        "src/lazy.ts",
        "src/utility.js",
        "src/parts/index.ts",
    ]
    assert plan.unresolved_local_imports == []
    assert plan.truncated is False
    assert plan.warnings == []


def test_typescript_alias_is_unresolved_but_external_package_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    _write(
        root,
        "src/main.ts",
        'import { local } from "@/lib/local";\nimport React from "react";\n',
    )

    plan = plan_bundle_closure(root, ["src/main.ts"])

    assert [(item.specifier, item.reason) for item in plan.unresolved_local_imports] == [
        ("@/lib/local", "path_alias_not_supported")
    ]
    assert plan.supporting_files == []


def test_supporting_file_and_byte_caps_truncate_without_exceeding_limits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    main = _write(
        root,
        "main.ts",
        'import "./a";\nimport "./b";\nimport "./c";\n',
    )
    a = _write(root, "a.ts", "export const a = 1;\n")
    _write(root, "b.ts", "export const b = 2;\n")
    _write(root, "c.ts", "export const c = 3;\n")

    file_limited = plan_bundle_closure(root, ["main.ts"], max_supporting_files=1)
    byte_limited = plan_bundle_closure(
        root,
        ["main.ts"],
        max_total_bytes=main.stat().st_size + a.stat().st_size - 1,
    )

    assert file_limited.supporting_files == ["a.ts"]
    assert file_limited.truncated is True
    assert any("supporting file limit" in warning.lower() for warning in file_limited.warnings)
    assert byte_limited.supporting_files == []
    assert byte_limited.total_bytes == main.stat().st_size
    assert byte_limited.truncated is True
    assert any("byte limit" in warning.lower() for warning in byte_limited.warnings)


def test_required_files_must_fit_total_file_cap(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    _write(root, "one.py")
    _write(root, "two.py")

    with pytest.raises(BundleClosureError, match="Required seed files exceed"):
        plan_bundle_closure(root, ["one.py", "two.py"], max_total_files=1)


def test_rejects_unsafe_required_paths_and_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    outside = _write(tmp_path, "outside.py")

    with pytest.raises(BundleClosureError, match="escapes snapshot root"):
        plan_bundle_closure(root, ["../outside.py"])

    _write(root, "src/main.ts", 'import "../../outside";\n')
    unsafe_plan = plan_bundle_closure(root, ["src/main.ts"])
    assert [(item.specifier, item.reason) for item in unsafe_plan.unresolved_local_imports] == [
        ("../../outside", "unsafe_path")
    ]
    assert any("outside the snapshot" in warning for warning in unsafe_plan.warnings)

    link = root / "link.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this system.")

    with pytest.raises(BundleClosureError, match="escapes snapshot root"):
        plan_bundle_closure(root, ["link.py"])
