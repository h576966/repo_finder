import json
from pathlib import Path

from source_scout.repo_map import (
    build_repo_map,
    repo_map_relevant_paths,
    repo_map_seed_items,
    repo_map_seed_text,
)


def test_repo_map_discovers_python_symbols_cli_mcp_and_tests(tmp_path: Path) -> None:
    root = tmp_path / "project"
    package = root / "src" / "project"
    package.mkdir(parents=True)
    (package / "server.py").write_text(
        "\n".join(
            [
                "from dataclasses import dataclass",
                "",
                "@dataclass",
                "class SearchResult:",
                "    path: str",
                "",
                "class ToolServer:",
                "    def register(self):",
                "        pass",
                "",
                "@mcp.tool()",
                "def explore_local_code():",
                "    pass",
                "",
                "subparsers.add_parser('scan')",
            ]
        ),
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_server.py").write_text(
        "def test_explore_local_code():\n    assert True\n",
        encoding="utf-8",
    )

    repo_map = build_repo_map(root)

    assert any(entry.name == "ToolServer" for entry in repo_map.symbols)
    assert any(
        entry.kind == "python_dataclass" and entry.name == "SearchResult" for entry in repo_map.symbols
    )
    assert any(entry.name == "explore_local_code" for entry in repo_map.mcp_tools)
    assert any(entry.name == "scan" for entry in repo_map.cli_entrypoints)
    assert any(entry.name == "test_explore_local_code" for entry in repo_map.tests)


def test_repo_map_discovers_ts_exports_next_routes_manifests_and_evals(tmp_path: Path) -> None:
    root = tmp_path / "next-app"
    (root / "app" / "api" / "chat").mkdir(parents=True)
    (root / "app" / "api" / "chat" / "route.ts").write_text(
        "export async function POST() { return Response.json({ ok: true }) }\n",
        encoding="utf-8",
    )
    (root / "components").mkdir()
    (root / "components" / "ChatPanel.tsx").write_text(
        "export const ChatPanel = () => <div />\ndescribe('chat panel', () => {})\n",
        encoding="utf-8",
    )
    (root / "evals" / "golden").mkdir(parents=True)
    (root / "evals" / "golden" / "suite.json").write_text(
        json.dumps({"suite_id": "chat-suite", "tasks": [{"id": "streaming_route"}]}),
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest", "dev": "next dev"}}),
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project.scripts]",
                "source-scout = 'source_scout.__main__:main'",
                "source_scout = 'source_scout.__main__:main'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "setup.cfg").write_text(
        "[options.entry_points]\nconsole_scripts =\n    scout-check = source_scout.__main__:main\n",
        encoding="utf-8",
    )
    (root / "setup.py").write_text(
        "from setuptools import setup\n"
        "setup(entry_points={'console_scripts': ['scout-profile=source_scout.profiler:main']})\n",
        encoding="utf-8",
    )
    (root / "tsconfig.json").write_text("{}", encoding="utf-8")
    (root / "next.config.ts").write_text("export default {}\n", encoding="utf-8")
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / "tests" / "fixtures" / "chat.json").write_text("[]", encoding="utf-8")
    (root / "node_modules" / "ignored").mkdir(parents=True)
    (root / "node_modules" / "ignored" / "bad.ts").write_text(
        "export const ShouldNotAppear = true\n",
        encoding="utf-8",
    )

    repo_map = build_repo_map(root)

    assert any(entry.path == "app/api/chat/route.ts" for entry in repo_map.next_routes)
    assert any(entry.name == "ChatPanel" for entry in repo_map.symbols)
    assert any(entry.name == "test" for entry in repo_map.cli_entrypoints)
    assert any(entry.name == "source-scout" for entry in repo_map.cli_entrypoints)
    assert any(entry.name == "source_scout" for entry in repo_map.cli_entrypoints)
    assert any(entry.name == "scout-check" for entry in repo_map.cli_entrypoints)
    assert any(entry.name == "scout-profile" for entry in repo_map.cli_entrypoints)
    assert any(entry.name == "streaming_route" for entry in repo_map.eval_suites)
    assert any(entry.path == "package.json" for entry in repo_map.manifests)
    assert any(entry.path == "pyproject.toml" for entry in repo_map.manifests)
    assert any(entry.path == "tsconfig.json" for entry in repo_map.manifests)
    assert any(entry.path == "next.config.ts" for entry in repo_map.manifests)
    assert any(entry.path == "tests/fixtures/chat.json" for entry in repo_map.fixtures)
    assert all(not entry.path.startswith("node_modules/") for entry in repo_map.all_entries())


def test_repo_map_includes_modern_module_extensions_and_requirements_variants(
    tmp_path: Path,
) -> None:
    root = tmp_path / "modern-modules"
    (root / "src").mkdir(parents=True)
    files = {
        "index.mjs": "export const modernModule = true\n",
        "worker.cjs": "export function commonWorker() {}\n",
        "types.mts": "export class ModuleType {}\n",
        "config.cts": "export const typedConfig = {}\n",
    }
    for filename, content in files.items():
        (root / "src" / filename).write_text(content, encoding="utf-8")
    (root / "requirements-test.txt").write_text("pytest>=9\n", encoding="utf-8")

    repo_map = build_repo_map(root)

    mapped_paths = {entry.path for entry in repo_map.files}
    symbol_names = {entry.name for entry in repo_map.symbols}
    assert {f"src/{filename}" for filename in files} <= mapped_paths
    assert {"modernModule", "commonWorker", "ModuleType", "typedConfig"} <= symbol_names
    assert any(entry.path == "requirements-test.txt" for entry in repo_map.manifests)


def test_repo_map_keeps_manifests_when_source_tree_is_large(tmp_path: Path) -> None:
    root = tmp_path / "large"
    (root / "src" / "large").mkdir(parents=True)
    for index in range(30):
        (root / "src" / "large" / f"module_{index}.py").write_text(
            f"def function_{index}():\n    return {index}\n",
            encoding="utf-8",
        )
    (root / "pyproject.toml").write_text(
        "[project.scripts]\nlarge-cli = 'large.cli:main'\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text('{"scripts":{"dev":"next dev"}}', encoding="utf-8")

    repo_map = build_repo_map(root, max_files=5)

    assert any(entry.path == "package.json" for entry in repo_map.manifests)
    assert any(entry.path == "pyproject.toml" for entry in repo_map.manifests)
    assert any(entry.name == "large-cli" for entry in repo_map.cli_entrypoints)


def test_repo_map_seed_context_is_relevant_and_marked_as_hints(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "src" / "project").mkdir(parents=True)
    (root / "src" / "project" / "catalog_store.py").write_text(
        "def search_assets():\n    return []\n",
        encoding="utf-8",
    )
    (root / "src" / "project" / "cli.py").write_text(
        "subparsers.add_parser('catalog')\n",
        encoding="utf-8",
    )

    repo_map = build_repo_map(root)
    paths = repo_map_relevant_paths(repo_map, ["catalog", "search"], limit=3)
    text = repo_map_seed_text(repo_map, "Find catalog search assets")
    items = repo_map_seed_items(repo_map, "Find catalog search assets", limit=3)

    assert paths[0] == "src/project/catalog_store.py"
    assert "not final evidence" in text
    assert items
    assert items[0]["path"] == "src/project/catalog_store.py"
