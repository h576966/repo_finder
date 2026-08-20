import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from source_scout import deepseek, fastcontext, fastcontext_tools
from tests.fastcontext_helpers import _write_snapshot


def test_fastcontext_tools_are_sandboxed_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    (root / "node_modules" / "noise").mkdir(parents=True)
    (root / "node_modules" / "noise" / "ignored.tsx").write_text(
        "export const ignored = true",
        encoding="utf-8",
    )
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    grep_result = fastcontext.grep_paths(root, "useReactTable", file_glob="**/*.tsx")
    assert grep_result["matches"][0]["citation"] == "src/components/data-table.tsx:1-1"

    read_result = fastcontext.read_file(root, "src/components/data-table.tsx", start=1, end=2)
    assert read_result["content"].startswith("1|import")

    glob_result = fastcontext.glob_paths(root, "**/*.tsx")
    assert glob_result["matches"] == ["src/components/data-table.tsx"]

    with pytest.raises(fastcontext.FastContextError):
        fastcontext.read_file(root, "../secret.txt")

    with pytest.raises(fastcontext.FastContextError):
        fastcontext.glob_paths(root, "../*.txt")


def test_glob_and_grep_prefer_rg_backend(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    commands: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        assert name == "rg"
        return "rg"

    def fake_run(command: list[str], **kwargs: Any) -> object:
        commands.append(command)
        assert kwargs["cwd"] == root
        if "--files" in command:
            stdout = "src/components/data-table.tsx\n"
        else:
            stdout = "src/components/data-table.tsx:1:import { useReactTable } from '@tanstack/react-table'\n"
        return type("Completed", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    monkeypatch.setattr(fastcontext_tools.shutil, "which", fake_which)
    monkeypatch.setattr(fastcontext_tools.subprocess, "run", fake_run)

    glob_result = fastcontext.glob_paths(root, "**/*.tsx")
    grep_result = fastcontext.grep_paths(root, "useReactTable", file_glob="**/*.tsx")

    assert glob_result["backend"] == "rg"
    assert glob_result["matches"] == ["src/components/data-table.tsx"]
    assert grep_result["backend"] == "rg"
    assert grep_result["matches"][0]["citation"] == "src/components/data-table.tsx:1-1"
    assert any("--glob" in command for command in commands)
    assert not any("--ignore-case" in command for command in commands)
    assert all("--no-config" in command for command in commands)
    grep_command = next(command for command in commands if "--files" not in command)
    assert grep_command[-3:] == ["--", "useReactTable", "."]


def test_rg_grep_uses_delimiter_before_model_controlled_pattern(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    commands: list[list[str]] = []

    monkeypatch.setattr(fastcontext_tools.shutil, "which", lambda name: "rg")

    def fake_run(command: list[str], **kwargs: Any) -> object:
        commands.append(command)
        return type("Completed", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(fastcontext_tools.subprocess, "run", fake_run)

    fastcontext.grep_paths(root, "-dangerous-pattern", file_glob="**/*.tsx")

    assert commands == [
        [
            "rg",
            "--no-config",
            "--color",
            "never",
            "--no-heading",
            "--with-filename",
            "--line-number",
            "--glob",
            "**/*.tsx",
            *fastcontext._rg_skip_globs(),
            "--",
            "-dangerous-pattern",
            ".",
        ]
    ]


def test_grep_is_case_sensitive_unless_ignore_case_requested(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    monkeypatch.setattr(fastcontext_tools.shutil, "which", lambda name: None)

    sensitive_result = fastcontext.grep_paths(
        root,
        "usereacttable",
        file_glob="**/*.tsx",
    )
    insensitive_result = fastcontext.grep_paths(
        root,
        "usereacttable",
        file_glob="**/*.tsx",
        ignore_case=True,
    )
    tool_result = fastcontext.execute_tool(
        root,
        {
            "tool": "Grep",
            "args": {"pattern": "usereacttable", "glob": "**/*.tsx"},
        },
    )

    assert sensitive_result["matches"] == []
    assert insensitive_result["matches"][0]["path"] == "src/components/data-table.tsx"
    assert tool_result["ok"] is True
    assert tool_result["result"]["matches"] == []


def test_workspace_prefix_paths_are_normalized_safely(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "source_scout"
    root.mkdir()
    _write_snapshot(root)
    monkeypatch.setattr(fastcontext_tools.shutil, "which", lambda name: None)

    pseudo_absolute = fastcontext.read_file(
        root,
        "/source_scout/src/components/data-table.tsx",
        start=1,
        end=1,
    )
    prefixed_relative = fastcontext.read_file(
        root,
        "source_scout/src/components/data-table.tsx",
        start=1,
        end=1,
    )
    suffix_absolute = fastcontext.read_file(
        root,
        str(tmp_path / "elsewhere" / "source_scout" / "src" / "components" / "data-table.tsx"),
        start=1,
        end=1,
    )
    glob_result = fastcontext.glob_paths(
        root,
        "/source_scout/src/**/*.tsx",
        directory="/source_scout/src",
    )
    grep_result = fastcontext.grep_paths(
        root,
        "useReactTable",
        file_glob="/source_scout/src/**/*.tsx",
        search_path="/source_scout/src",
    )

    assert pseudo_absolute["path"] == "src/components/data-table.tsx"
    assert prefixed_relative["path"] == "src/components/data-table.tsx"
    assert suffix_absolute["path"] == "src/components/data-table.tsx"
    assert glob_result["matches"] == ["src/components/data-table.tsx"]
    assert grep_result["matches"][0]["path"] == "src/components/data-table.tsx"

    renamed_root = tmp_path / "workspace_root"
    renamed_root.mkdir()
    _write_snapshot(renamed_root)
    renamed_read = fastcontext.read_file(
        renamed_root,
        "/source_scout/src/components/data-table.tsx",
        start=1,
        end=1,
    )
    renamed_glob = fastcontext.glob_paths(
        renamed_root,
        "/source_scout/src/**/*.tsx",
        directory="/source_scout/src",
    )
    renamed_grep = fastcontext.grep_paths(
        renamed_root,
        "useReactTable",
        file_glob="/source_scout/src/**/*.tsx",
        search_path="/source_scout/src",
    )

    assert renamed_read["path"] == "src/components/data-table.tsx"
    assert renamed_glob["matches"] == ["src/components/data-table.tsx"]
    assert renamed_grep["matches"][0]["path"] == "src/components/data-table.tsx"


def test_unrelated_absolute_paths_still_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "source_scout"
    root.mkdir()
    _write_snapshot(root)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(fastcontext.FastContextError, match="escapes snapshot root"):
        fastcontext.read_file(root, str(outside))


def test_parse_fastcontext_response_ignores_content_tool_calls_and_reads_final_json() -> None:
    content_tool_response = fastcontext.parse_fastcontext_response(
        json.dumps(
            {"tool_calls": [{"tool": "GREP", "args": {"pattern": "useReactTable", "glob": "**/*.tsx"}}]}
        )
    )
    assert content_tool_response.citations == []
    assert content_tool_response.citation_ids == []
    assert content_tool_response.notes == []

    final_response = fastcontext.parse_fastcontext_response(
        json.dumps(
            {
                "final_answer": {
                    "evidence": [
                        {
                            "path": "src/components/data-table.tsx",
                            "start_line": 1,
                            "end_line": 4,
                        }
                    ]
                }
            }
        )
    )
    assert final_response.citations[0].evidence_path() == "src/components/data-table.tsx:1-4"

    id_response = fastcontext.parse_fastcontext_response(
        json.dumps({"final_answer": {"citation_ids": ["c1", "C2", "C1"], "notes": []}})
    )
    assert id_response.citation_ids == ["C1", "C2"]


def test_citation_validation_rejects_bad_ranges_and_unsupported_observations(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)

    evidence, notes = fastcontext._validated_evidence_paths(
        root,
        [
            fastcontext.FastContextCitation("src/components/data-table.tsx", 5, 1),
            fastcontext.FastContextCitation("src/components/data-table.tsx", 1, 999),
            fastcontext.FastContextCitation("src/components/data-table.tsx", 6, 6),
            fastcontext.FastContextCitation("src/components/data-table.tsx", 1, 2),
            fastcontext.FastContextCitation("source_scout/src/**/*.tsx", 1, 2),
            fastcontext.FastContextCitation("src/components/data-table.tsx"),
        ],
        observation_support=fastcontext.ObservationSupport(
            files={"src/components/data-table.tsx"},
            ranges={"src/components/data-table.tsx": [(4, 4)]},
        ),
    )

    assert evidence == []
    assert any("reversed line range" in note for note in notes)
    assert any("overly broad citation" in note for note in notes)
    assert any("beyond EOF" in note for note in notes)
    assert any("outside observed line ranges" in note for note in notes)
    assert any("wildcard or glob citation" in note for note in notes)
    assert any("without exact line range" in note for note in notes)


def test_citation_id_validation_rejects_unknown_ids(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)

    evidence, notes = fastcontext._validated_response_evidence_paths(
        root,
        fastcontext.ParsedFastContextResponse(
            citations=[],
            citation_ids=["C99"],
            notes=[],
        ),
        fastcontext.ObservationSupport(
            files={"src/components/data-table.tsx"},
            ranges={"src/components/data-table.tsx": [(1, 4)]},
        ),
    )

    assert evidence == []
    assert notes == ["Skipped unknown citation_id: C99"]


def test_evidence_budget_detects_too_many_files() -> None:
    result = fastcontext._apply_evidence_budget(
        [
            "src/a.ts:1-1",
            "src/b.ts:1-1",
            "src/c.ts:1-1",
            "src/d.ts:1-1",
        ]
    )

    assert result.over_budget is True
    assert result.truncated is True
    assert result.accepted_count == fastcontext.MAX_FINAL_CITATIONS
    assert result.accepted_file_count == fastcontext.MAX_FINAL_FILES


def test_local_seed_context_uses_generated_repo_map_without_repo_specific_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "generic_project"
    (root / "src" / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "evals" / "golden").mkdir(parents=True)
    (root / "src" / "app" / "catalog_store.py").write_text(
        "def search_assets():\n    return []\n",
        encoding="utf-8",
    )
    (root / "src" / "app" / "mcp_server.py").write_text(
        "@mcp.tool()\ndef find_reusable_code():\n    pass\n",
        encoding="utf-8",
    )
    (root / "src" / "app" / "cli.py").write_text(
        "subparsers.add_parser('catalog')\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_catalog_store.py").write_text(
        "def test_search_assets():\n    assert True\n",
        encoding="utf-8",
    )
    (root / "evals" / "golden" / "catalog_v1.json").write_text(
        '{"suite_id":"catalog-suite","tasks":[{"id":"catalog_search"}]}\n',
        encoding="utf-8",
    )

    catalog_seed = fastcontext._local_seed_context(
        root,
        "Find where catalog search_assets scoring is implemented.",
    )
    mcp_seed = fastcontext._local_seed_context(
        root,
        "Find the MCP tool that exposes find_reusable_code.",
    )
    eval_seed = fastcontext._local_seed_context(
        root,
        "Find the golden eval fixture suite file.",
    )

    assert catalog_seed["likely_source_files"][0] == "src/app/catalog_store.py"
    assert mcp_seed["likely_source_files"][0] == "src/app/mcp_server.py"
    assert eval_seed["likely_source_files"][0] == "evals/golden/catalog_v1.json"
    assert "Generated repo map hints" in catalog_seed["repo_map_hints"]
    assert catalog_seed["repo_map"]
    assert catalog_seed["priority_file_matches"]
    assert catalog_seed["priority_file_matches"][0]["path"] == "src/app/catalog_store.py"
    assert fastcontext._seed_priority_paths(catalog_seed)[0] == "src/app/catalog_store.py"


def test_final_answer_choices_prioritize_primary_source_paths() -> None:
    support = fastcontext.ObservationSupport(
        files=set(),
        ranges={
            "tests/test_server.py": [(1, 3)],
            "README.md": [(10, 12)],
            "src/source_scout/server.py": [(20, 30)],
            "src/source_scout/models.py": [(5, 8)],
        },
    )

    choices = fastcontext._observed_citation_choices(support)

    assert choices[:2] == [
        "src/source_scout/models.py:5-8",
        "src/source_scout/server.py:20-30",
    ]
    assert "C1: src/source_scout/models.py:5-8" in fastcontext._observed_citation_choices_text(support)


def test_final_answer_choices_and_budget_honor_task_priority_paths() -> None:
    support = fastcontext.ObservationSupport(
        files=set(),
        ranges={
            "src/source_scout/assessor.py": [(20, 30)],
            "src/source_scout/evidence.py": [(270, 300)],
            "src/source_scout/catalog.py": [(140, 150)],
        },
    )

    choices = fastcontext._observed_citation_choices(
        support,
        priority_paths=["src/source_scout/evidence.py"],
    )
    budget = fastcontext._apply_evidence_budget(
        [
            "src/source_scout/assessor.py:20-30",
            "src/source_scout/catalog.py:140-150",
            "src/source_scout/evidence.py:270-300",
            "src/source_scout/evidence.py:400-420",
        ],
        priority_paths=["src/source_scout/evidence.py"],
    )

    assert choices[0] == "src/source_scout/evidence.py:270-300"
    assert budget.evidence_paths[:2] == [
        "src/source_scout/evidence.py:270-300",
        "src/source_scout/evidence.py:400-420",
    ]


def test_finalization_waits_for_priority_observation() -> None:
    support = fastcontext.ObservationSupport(
        files=set(),
        ranges={
            "src/source_scout/assessor.py": [(20, 30)],
            "src/source_scout/catalog.py": [(140, 150)],
            "src/source_scout/evidence.py": [(270, 300)],
        },
    )

    assert (
        fastcontext._finalization_reason(
            1,
            6,
            support,
            priority_paths=["src/source_scout/pipeline.py"],
        )
        is None
    )
    assert (
        fastcontext._finalization_reason(
            5,
            6,
            support,
            priority_paths=["src/source_scout/pipeline.py"],
        )
        is not None
    )
    assert (
        fastcontext._finalization_reason(
            1,
            6,
            support,
            priority_paths=["src/source_scout/evidence.py"],
        )
        == "enough_primary_source_ranges"
    )


def test_local_seed_context_includes_likely_source_files(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src" / "source_scout").mkdir(parents=True)
    (root / "src" / "source_scout" / "deepseek.py").write_text("def status(): pass\n", encoding="utf-8")
    (root / "src" / "source_scout" / "__main__.py").write_text("def cli(): pass\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_deepseek.py").write_text("def test_status(): pass\n", encoding="utf-8")
    monkeypatch.setattr(fastcontext_tools.shutil, "which", lambda name: None)

    seed = fastcontext._local_seed_context(root, "Find the DeepSeek status CLI command")

    likely = seed["likely_source_files"]
    assert "src/source_scout/__main__.py" in likely
    assert "src/source_scout/deepseek.py" in likely
    assert likely.index("src/source_scout/deepseek.py") < likely.index("tests/test_deepseek.py")


@pytest.mark.asyncio
async def test_fastcontext_structured_output_failure_is_not_retried() -> None:
    response_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_calls
        assert request.url.path == "/responses"
        response_calls += 1
        payload = json.loads(request.content)
        assert payload["text"]["format"]["type"] == "json_schema"
        return httpx.Response(400, json={"error": "structured output unsupported"})

    with pytest.raises(deepseek.ModelError, match="HTTP 400"):
        await deepseek.response_json(
            messages=[{"role": "user", "content": "Find the data table"}],
            transport=httpx.MockTransport(handler),
            max_tokens=3000,
            temperature=0.0,
            attempts=1,
            response_format=fastcontext._fastcontext_response_format(),
        )

    assert response_calls == 1
