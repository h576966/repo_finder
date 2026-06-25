import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from source_scout import fastcontext, lmstudio
from tests.fastcontext_helpers import _write_snapshot, isolated_catalog

pytestmark = pytest.mark.usefixtures(isolated_catalog.__name__)

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

    monkeypatch.setattr(fastcontext.shutil, "which", fake_which)
    monkeypatch.setattr(fastcontext.subprocess, "run", fake_run)

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

    monkeypatch.setattr(fastcontext.shutil, "which", lambda name: "rg")

    def fake_run(command: list[str], **kwargs: Any) -> object:
        commands.append(command)
        return type("Completed", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(fastcontext.subprocess, "run", fake_run)

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
    monkeypatch.setattr(fastcontext.shutil, "which", lambda name: None)

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
    monkeypatch.setattr(fastcontext.shutil, "which", lambda name: None)

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


def test_parse_fastcontext_json_and_final_answer_formats() -> None:
    tool_response = fastcontext.parse_fastcontext_response(
        json.dumps(
            {
                "tool_calls": [
                    {"tool": "GREP", "args": {"pattern": "useReactTable", "glob": "**/*.tsx"}}
                ]
            }
        )
    )
    assert tool_response.tool_calls == [
        {"tool": "GREP", "args": {"pattern": "useReactTable", "glob": "**/*.tsx"}}
    ]

    final_response = fastcontext.parse_fastcontext_response(
        "<final_answer>\nsrc/components/data-table.tsx:1-4\n</final_answer>"
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
            tool_calls=[],
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


def test_local_seed_context_prioritizes_known_project_files(tmp_path: Path) -> None:
    root = tmp_path / "source_scout"
    (root / "src" / "source_scout").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "evals" / "golden").mkdir(parents=True)
    for name in [
        "bundles.py",
        "catalog.py",
        "capabilities.py",
        "cli_status.py",
        "constants.py",
        "evidence.py",
        "fastcontext.py",
        "github_client.py",
        "lmstudio.py",
        "local_explore_eval.py",
        "models.py",
        "pipeline.py",
        "profiler.py",
        "server.py",
        "__main__.py",
    ]:
        (root / "src" / "source_scout" / name).write_text(
            "repository catalog ranking scoring freshness archived template mirror\n",
            encoding="utf-8",
        )
    (root / "src" / "source_scout" / "assessment_eval.py").write_text(
        "assessment eval suite loading metrics\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_assessor.py").write_text(
        "def test_unknown_evidence_id_repair(): assert True\n",
        encoding="utf-8",
    )
    (root / "evals" / "golden" / "assessment_smoke_v1.json").write_text(
        '{"suite_id":"assessment-smoke"}\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "Standalone local exploration usage documentation\n",
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text("FastContext local usage\n", encoding="utf-8")

    assert fastcontext._local_seed_context(
        root,
        "Find where repository qualification rejects archived template mirror repos.",
    )["likely_source_files"][0] == "src/source_scout/pipeline.py"
    assert fastcontext._local_seed_context(
        root,
        "Find where reusable catalog candidates are scored with capability intent and Gemma profile signals.",
    )["likely_source_files"][0] == "src/source_scout/catalog.py"
    assert fastcontext._local_seed_context(
        root,
        "Find where LM Studio status and FastContext status commands are registered and implemented.",
    )["likely_source_files"][:2] == [
        "src/source_scout/__main__.py",
        "src/source_scout/cli_status.py",
    ]
    assert fastcontext._local_seed_context(
        root,
        "Find where the local exploration eval suite is loaded, scored, and exposed as a CLI command.",
    )["likely_source_files"][:2] == [
        "src/source_scout/__main__.py",
        "src/source_scout/local_explore_eval.py",
    ]
    assert fastcontext._local_seed_context(
        root,
        "Find where catalog assets are searched and scored with Gemma profile weighting.",
    )["likely_source_files"][:2] == [
        "src/source_scout/catalog.py",
        "src/source_scout/capabilities.py",
    ]
    assert fastcontext._local_seed_context(
        root,
        "Find the project documentation that explains standalone local exploration usage.",
    )["likely_source_files"][:2] == ["README.md", "AGENTS.md"]
    assert fastcontext._local_seed_context(
        root,
        "Find tests that verify assessor repair behavior for unknown evidence IDs.",
    )["likely_source_files"][0] == "tests/test_assessor.py"
    assert fastcontext._local_seed_context(
        root,
        "Find the golden assessment eval fixture suite.",
    )["likely_source_files"][0] == "evals/golden/assessment_smoke_v1.json"
    assert fastcontext._local_seed_context(
        root,
        "Find where source bundles are created and opened_bundle reuse outcomes are recorded.",
    )["likely_source_files"][:2] == [
        "src/source_scout/bundles.py",
        "src/source_scout/server.py",
    ]
    assert fastcontext._local_seed_context(
        root,
        "Find where Gemma profiles repository cards into strict JSON and stores gemma_profile.",
    )["likely_source_files"][0] == "src/source_scout/profiler.py"
    assert fastcontext._local_seed_context(
        root,
        "Find where deterministic evidence scanner records dependency and capability signals.",
    )["likely_source_files"][0] == "src/source_scout/evidence.py"
    assert fastcontext._local_seed_context(
        root,
        "Find where GitHub API requests, rate-limit handling, and repository search calls are implemented.",
    )["likely_source_files"][0] == "src/source_scout/github_client.py"
    assert fastcontext._local_seed_context(
        root,
        "Find where FastContext local exploration runs the tool loop and returns LocalExploreResult.",
    )["likely_source_files"][:2] == [
        "src/source_scout/fastcontext.py",
        "src/source_scout/models.py",
    ]
    assert fastcontext._local_seed_context(
        root,
        "Find where FastContext sends LM Studio structured output schema and falls back to robust parsing.",
    )["likely_source_files"][:2] == [
        "src/source_scout/lmstudio.py",
        "src/source_scout/fastcontext.py",
    ]
    assert fastcontext._local_seed_context(
        root,
        "Find the dataclasses that define reusable candidates, bundles, outcomes, "
        "and local exploration results.",
    )["likely_source_files"][0] == "src/source_scout/models.py"
    seed = fastcontext._local_seed_context(
        root,
        "Find where repository qualification rejects archived template mirror repos.",
    )
    priority_paths = fastcontext._seed_priority_paths(seed)
    assert priority_paths[0] == "src/source_scout/pipeline.py"
    assert "src/source_scout/constants.py" in priority_paths
    catalog_seed = fastcontext._local_seed_context(
        root,
        "Find where reusable catalog candidates are scored with capability intent and Gemma profile signals.",
    )
    assert catalog_seed["priority_file_matches"]
    assert catalog_seed["priority_file_matches"][0]["path"] == "src/source_scout/catalog.py"
    assert "citation" in catalog_seed["priority_file_matches"][0]


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

    assert fastcontext._finalization_reason(
        1,
        6,
        support,
        priority_paths=["src/source_scout/pipeline.py"],
    ) is None
    assert fastcontext._finalization_reason(
        5,
        6,
        support,
        priority_paths=["src/source_scout/pipeline.py"],
    ) is not None
    assert fastcontext._finalization_reason(
        1,
        6,
        support,
        priority_paths=["src/source_scout/evidence.py"],
    ) == "enough_primary_source_ranges"


def test_fastcontext_seed_defaults_and_env_override(monkeypatch) -> None:
    monkeypatch.delenv("SOURCE_SCOUT_FASTCONTEXT_SEED", raising=False)
    assert fastcontext._fastcontext_seed() == fastcontext.DEFAULT_FASTCONTEXT_SEED

    monkeypatch.setenv("SOURCE_SCOUT_FASTCONTEXT_SEED", "123")
    assert fastcontext._fastcontext_seed() == 123

    monkeypatch.setenv("SOURCE_SCOUT_FASTCONTEXT_SEED", "none")
    assert fastcontext._fastcontext_seed() is None


def test_local_seed_context_includes_likely_source_files(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src" / "source_scout").mkdir(parents=True)
    (root / "src" / "source_scout" / "lmstudio.py").write_text("def status(): pass\n", encoding="utf-8")
    (root / "src" / "source_scout" / "__main__.py").write_text("def cli(): pass\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_lmstudio.py").write_text("def test_status(): pass\n", encoding="utf-8")
    monkeypatch.setattr(fastcontext.shutil, "which", lambda name: None)

    seed = fastcontext._local_seed_context(root, "Find the LM Studio status CLI command")

    likely = seed["likely_source_files"]
    assert "src/source_scout/__main__.py" in likely
    assert "src/source_scout/lmstudio.py" in likely
    assert likely.index("src/source_scout/lmstudio.py") < likely.index("tests/test_lmstudio.py")

@pytest.mark.asyncio
async def test_fastcontext_uses_structured_output_and_retries_without_schema() -> None:
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        assert request.url.path == "/v1/chat/completions"
        chat_calls += 1
        payload = json.loads(request.content)
        if chat_calls == 1:
            assert payload["response_format"]["type"] == "json_schema"
            return httpx.Response(400, json={"error": "structured output unsupported"})
        assert "response_format" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "final_answer": {
                                        "evidence": [
                                            {
                                                "path": "src/components/data-table.tsx",
                                                "start_line": 1,
                                                "end_line": 4,
                                            }
                                        ],
                                        "notes": ["Fallback parser still works."],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    content = await fastcontext._chat_fastcontext(
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        transport=httpx.MockTransport(handler),
        max_tokens=3000,
        temperature=0.0,
    )

    assert chat_calls == 2
    assert json.loads(content)["final_answer"]["notes"] == ["Fallback parser still works."]
