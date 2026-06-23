import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from source_scout import catalog, fastcontext, lmstudio


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(tmp_path / ".source_scout"))
    catalog.reset_connection()
    yield
    catalog.reset_connection()


def _repo_metadata(owner: str, name: str) -> dict[str, Any]:
    return {
        "owner": {"login": owner},
        "name": name,
        "full_name": f"{owner}/{name}",
        "html_url": f"https://github.com/{owner}/{name}",
        "private": False,
        "archived": False,
        "mirror_url": None,
        "fork": False,
        "is_template": False,
        "language": "TypeScript",
        "size": 10,
        "created_at": "2026-01-15T00:00:00Z",
        "pushed_at": "2026-06-20T12:00:00Z",
        "topics": ["nextjs"],
    }


def _write_snapshot(root: Path) -> None:
    (root / "src" / "components").mkdir(parents=True)
    (root / "src" / "components" / "data-table.tsx").write_text(
        "\n".join(
            [
                "import { useReactTable } from '@tanstack/react-table'",
                "export function DataTable() {",
                "  const table = useReactTable({ columns: [] })",
                "  return <table>{table.getRowModel().rows.length}</table>",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15", "@tanstack/react-table": "8"}}),
        encoding="utf-8",
    )


def _write_budget_snapshot(root: Path) -> None:
    _write_snapshot(root)
    (root / "README.md").write_text("Project docs\n", encoding="utf-8")
    (root / "src" / "components" / "form.tsx").write_text(
        "\n".join(
            [
                "export function Form() {",
                "  return <form />",
                "}",
            ]
        ),
        encoding="utf-8",
    )


def _payload_message_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(message.get("content") or "")
        for message in payload.get("messages", [])
        if isinstance(message, dict)
    )


def _create_candidate(tmp_path: Path) -> str:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    _write_snapshot(snapshot_root)
    repo_id = catalog.upsert_repository(_repo_metadata("owner", "repo"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
    return catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "data-table",
        {
            "entry_paths": ["src/components/data-table.tsx"],
            "dependency_paths": ["package.json"],
            "external_dependencies": ["@tanstack/react-table"],
            "evidence_paths": ["src/components/data-table.tsx:1-4"],
            "reuse_score": 0.9,
            "synthesis": {
                "adaptation_notes": [],
                "ui_path_score": 1.0,
                "noise_penalty": 0.0,
                "capability_path_score": 1.0,
            },
        },
    )


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
    for name in ["catalog.py", "constants.py", "models.py", "pipeline.py", "server.py"]:
        (root / "src" / "source_scout" / name).write_text(
            "repository catalog ranking scoring freshness archived template mirror\n",
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
        "Find the project documentation that explains standalone local exploration usage.",
    )["likely_source_files"][:2] == ["README.md", "AGENTS.md"]


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


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_uses_openai_tool_calls(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        assert request.url.path == "/v1/chat/completions"
        chat_calls += 1
        payload = json.loads(request.content)
        if chat_calls == 1:
            assert payload["tools"][0]["function"]["name"] == "Read"
            assert payload["chat_template_kwargs"]["enable_thinking"] is False
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-read-1",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 1,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-read-2",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 3,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )

        assert "tools" not in payload
        tool_messages = [message for message in payload["messages"] if message["role"] == "tool"]
        assert tool_messages[-1]["tool_call_id"] == "call-read-2"
        assert "src/components/data-table.tsx:3-3" in tool_messages[-1]["content"]
        assert payload["messages"][-1]["role"] == "user"
        assert "final_answer JSON" in payload["messages"][-1]["content"]
        assert "Observed citation choices" in payload["messages"][-1]["content"]
        assert "C1: src/components/data-table.tsx:1-1" in payload["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "final_answer": {
                                        "citation_ids": ["C1"],
                                        "notes": ["Observed with Read."],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        max_turns=2,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-1"]
    assert result.notes == ["Observed with Read."]
    assert result.trajectory[0]["tools_enabled"] is True
    assert result.trajectory[1]["tools_enabled"] is False
    assert result.trajectory[1]["selected_citation_ids"] == ["C1"]
    assert result.trajectory[0]["tool_calls"][0]["tool"] == "Read"
    assert result.trajectory[0]["tool_observations"][0]["tool_call_id"] == "call-read-1"


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_falls_back_when_lmstudio_rejects_tools(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        chat_calls += 1
        payload = json.loads(request.content)
        if "tools" in payload:
            return httpx.Response(
                400,
                json={"error": "Cannot combine structured output constraints with lazy grammar"},
            )
        assert payload["response_format"]["type"] == "json_schema"
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
                                                "end_line": 1,
                                            }
                                        ],
                                        "notes": ["Fallback content mode."],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        max_turns=1,
        transport=httpx.MockTransport(handler),
    )

    assert chat_calls == 2
    assert result.status == "completed"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-1"]
    assert result.notes == ["Fallback content mode."]
    assert result.trajectory[0]["finish_reason"] == "fallback_content"


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_downgrades_max_turn_observation_fallback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "tools" in payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-read",
                                    "type": "function",
                                    "function": {
                                        "name": "Read",
                                        "arguments": json.dumps(
                                            {
                                                "path": "src/components/data-table.tsx",
                                                "offset": 1,
                                                "limit": 4,
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        max_turns=1,
        transport=httpx.MockTransport(handler),
        allow_observation_fallback=True,
    )

    assert result.status == "fallback_observations"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-4"]
    assert result.trajectory[-1]["finish_reason"] == "max_turn_observation_fallback"


def test_fastcontext_observation_fallback_is_capped() -> None:
    result = fastcontext._fallback_observation_result(
        fastcontext.ObservationSupport(
            files=set(),
            ranges={
                "src/a.ts": [(1, 1)],
                "src/b.ts": [(1, 1)],
                "src/c.ts": [(1, 1)],
                "src/d.ts": [(1, 1)],
            },
        ),
        [],
        note="fallback",
    )

    assert result.evidence_paths == ["src/a.ts:1-1", "src/b.ts:1-1", "src/c.ts:1-1"]
    assert result.trajectory[0]["citation_budget"]["truncated"] is True


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_keeps_tools_enabled_after_insufficient_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        payload = json.loads(request.content)
        chat_calls += 1
        if chat_calls == 1:
            assert "tools" in payload
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-read",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 1,
                                                    "limit": 4,
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        if chat_calls == 2:
            assert "tools" in payload
            assert "not enough strong citation support" in payload["messages"][-1]["content"]
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
                                            "notes": ["Completed after continued tool-enabled turn."],
                                        }
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        raise AssertionError("Unexpected extra chat turn")

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        max_turns=4,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.notes == ["Completed after continued tool-enabled turn."]
    assert result.trajectory[0]["tools_enabled"] is True
    assert result.trajectory[0]["finalization_reason"] is None
    assert result.trajectory[1]["tools_enabled"] is True


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_keeps_tools_enabled_for_noisy_ranges(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    (root / "README.md").write_text("FastContext setup notes\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_fastcontext.py").write_text(
        "def test_fastcontext_setup():\n    assert True\n",
        encoding="utf-8",
    )
    (root / "src" / "source_scout").mkdir(parents=True)
    (root / "src" / "source_scout" / "fastcontext.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 130)),
        encoding="utf-8",
    )
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        payload = json.loads(request.content)
        chat_calls += 1
        if chat_calls == 1:
            assert "tools" in payload
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-read-source",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/source_scout/fastcontext.py",
                                                    "offset": 1,
                                                    "limit": 120,
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-read-docs",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {"path": "README.md", "offset": 1, "limit": 1}
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-read-tests",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "tests/test_fastcontext.py",
                                                    "offset": 1,
                                                    "limit": 2,
                                                }
                                            ),
                                        },
                                    },
                                ],
                            },
                        }
                    ]
                },
            )
        assert "tools" in payload
        assert "primary source, broad" in payload["messages"][-1]["content"]
        assert "supporting/noisy" in payload["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "final_answer": {
                                        "citation_ids": ["C1"],
                                        "notes": ["Completed after continued exploration."],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find FastContext implementation"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        max_turns=6,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.trajectory[0]["finalization_reason"] is None
    assert result.trajectory[1]["tools_enabled"] is True


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_retries_glob_style_final_answer_without_tools(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        payload = json.loads(request.content)
        chat_calls += 1
        if chat_calls == 1:
            assert "tools" in payload
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-read-1",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 1,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-read-2",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 3,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        if chat_calls == 2:
            assert "tools" not in payload
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
                                                    "path": "src/**/*.tsx",
                                                    "start_line": 1,
                                                    "end_line": 4,
                                                }
                                            ],
                                            "notes": [],
                                        }
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        assert "tools" not in payload
        assert "wildcard or glob citation" in payload["messages"][-1]["content"]
        assert "C1: src/components/data-table.tsx:1-1" in payload["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "final_answer": {
                                        "citation_ids": ["C1"],
                                        "notes": ["Used exact observed citation."],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        max_turns=3,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-1"]
    assert result.trajectory[0]["finalization_reason"] == "enough_primary_source_ranges"
    assert result.trajectory[1]["validation_notes"] == [
        "Skipped wildcard or glob citation: src/**/*.tsx:1-4"
    ]
    assert result.trajectory[2]["tools_enabled"] is False
    assert result.trajectory[2]["selected_citation_ids"] == ["C1"]


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_retries_over_budget_citation_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_budget_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        payload = json.loads(request.content)
        chat_calls += 1
        if chat_calls == 1:
            assert "tools" in payload
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-read-1",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 1,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-read-2",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 3,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-read-3",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 5,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-read-4",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/form.tsx",
                                                    "offset": 1,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                ],
                            },
                        }
                    ]
                },
            )
        if chat_calls == 2:
            assert "tools" not in payload
            assert "1-3 citation IDs" in payload["messages"][-1]["content"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "final_answer": {
                                            "citation_ids": ["C1", "C2", "C3", "C4"],
                                            "notes": ["Too many."],
                                        }
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        assert "tools" not in payload
        assert "selected too many citations" in payload["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "final_answer": {
                                        "citation_ids": ["C1", "C2"],
                                        "notes": ["Narrowed."],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        max_turns=4,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.evidence_paths == [
        "src/components/data-table.tsx:1-1",
        "src/components/data-table.tsx:3-3",
    ]
    assert result.trajectory[1]["citation_budget"]["over_budget"] is True
    assert result.trajectory[2]["citation_budget"]["over_budget"] is False
    assert chat_calls == 3


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_truncates_over_budget_retry_source_first(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_budget_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        payload = json.loads(request.content)
        chat_calls += 1
        if chat_calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-readme",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {"path": "README.md", "offset": 1, "limit": 1}
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-src-1",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 1,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-src-2",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 3,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-src-3",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/form.tsx",
                                                    "offset": 1,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                ],
                            },
                        }
                    ]
                },
            )
        assert "tools" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "final_answer": {
                                        "citation_ids": ["C1", "C2", "C3", "C4"],
                                        "notes": ["Still too many."],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        max_turns=3,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.evidence_paths == [
        "src/components/data-table.tsx:1-1",
        "src/components/data-table.tsx:3-3",
        "src/components/form.tsx:1-1",
    ]
    assert result.trajectory[-1]["citation_budget"] == {
        "original_count": 4,
        "accepted_count": 3,
        "original_file_count": 3,
        "accepted_file_count": 2,
        "over_budget": True,
        "truncated": True,
    }
    assert any("Citation budget applied" in note for note in result.notes)


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_uses_local_fallback_after_failed_final_retry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        payload = json.loads(request.content)
        chat_calls += 1
        if chat_calls == 1:
            assert "tools" in payload
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-read-1",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 1,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call-read-2",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 3,
                                                    "limit": 1,
                                                }
                                            ),
                                        },
                                    },
                                ],
                            },
                        }
                    ]
                },
            )
        if chat_calls == 2:
            assert "tools" not in payload
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "final_answer": {
                                            "citation_ids": ["C99"],
                                            "notes": [],
                                        }
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        if chat_calls == 3:
            assert "tools" not in payload
            assert "unknown citation_id" in payload["messages"][-1]["content"]
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
                                                    "path": "src/missing.ts",
                                                    "start_line": 1,
                                                    "end_line": 2,
                                                }
                                            ],
                                            "notes": [],
                                        }
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        raise AssertionError("Tools should not reopen after local fallback evidence exists.")

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        max_turns=4,
        transport=httpx.MockTransport(handler),
        allow_observation_fallback=True,
    )

    assert result.status == "fallback_observations"
    assert result.evidence_paths == [
        "src/components/data-table.tsx:1-1",
        "src/components/data-table.tsx:3-3",
    ]
    assert result.trajectory[-1]["finish_reason"] == "final_answer_retry_observation_fallback"
    assert chat_calls == 3


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_accepts_repaired_final_answer_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source_scout"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        payload = json.loads(request.content)
        chat_calls += 1
        if chat_calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-read",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "/source_scout/src/components/data-table.tsx",
                                                    "offset": 1,
                                                    "limit": 4,
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        assert "tools" not in payload
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
                                                "path": "/source_scout/src/components/data-table.tsx",
                                                "start_line": 1,
                                                "end_line": 4,
                                            }
                                        ],
                                        "notes": ["Path was repaired."],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
        max_turns=2,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-4"]


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_fails_max_turn_observation_fallback_for_catalog(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-read",
                                    "type": "function",
                                    "function": {
                                        "name": "Read",
                                        "arguments": json.dumps(
                                            {
                                                "path": "src/components/data-table.tsx",
                                                "offset": 1,
                                                "limit": 4,
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    with pytest.raises(fastcontext.FastContextLoopError):
        await fastcontext._run_tool_loop(
            root=root,
            messages=[{"role": "user", "content": "Find the data table"}],
            model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
            config=lmstudio.get_config(),
            max_turns=1,
            transport=httpx.MockTransport(handler),
            allow_observation_fallback=False,
        )


@pytest.mark.asyncio
async def test_refine_candidate_stores_fastcontext_evidence(tmp_path: Path) -> None:
    candidate_id = _create_candidate(tmp_path)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": lmstudio.DEFAULT_FASTCONTEXT_MODEL}]},
            )
        assert request.url.path == "/v1/chat/completions"
        chat_calls += 1
        payload = json.loads(request.content)
        assert payload["model"] == lmstudio.DEFAULT_FASTCONTEXT_MODEL
        if chat_calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "tool_calls": [
                                            {
                                                "tool": "GREP",
                                                "args": {
                                                    "pattern": "useReactTable",
                                                    "glob": "**/*.tsx",
                                                },
                                            }
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        assert "src/components/data-table.tsx" in _payload_message_text(payload)
        assert "tools" not in payload
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
                                                "reason": "TanStack table implementation",
                                            }
                                        ],
                                        "notes": ["Reusable table component"],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext.refine_candidate(
        candidate_id,
        "Find a reusable TanStack data table",
        transport=httpx.MockTransport(handler),
    )

    assert result["candidate_id"] == candidate_id
    assert result["evidence_paths"] == ["src/components/data-table.tsx:1-4"]
    assert result["notes"] == ["Reusable table component"]

    refinements = catalog.get_connection().execute(
        """
        SELECT asset_id, task_signature, evidence_paths, notes
        FROM evidence_refinements
        """
    ).fetchall()
    assert len(refinements) == 1
    assert refinements[0][0] == candidate_id
    assert json.loads(refinements[0][2]) == ["src/components/data-table.tsx:1-4"]

    runs = catalog.get_connection().execute(
        """
        SELECT stage_name, status, model_id
        FROM analysis_runs
        WHERE stage_name = 'fastcontext-refine'
        """
    ).fetchall()
    assert runs == [("fastcontext-refine", "completed", lmstudio.DEFAULT_FASTCONTEXT_MODEL)]


@pytest.mark.asyncio
async def test_refine_candidate_stores_parent_task_signature(tmp_path: Path) -> None:
    candidate_id = _create_candidate(tmp_path)
    parent_signature = "parent-task-123"
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": lmstudio.DEFAULT_FASTCONTEXT_MODEL}]},
            )
        assert request.url.path == "/v1/chat/completions"
        chat_calls += 1
        if chat_calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "tool_calls": [
                                            {
                                                "tool": "READ",
                                                "args": {
                                                    "path": "src/components/data-table.tsx",
                                                    "offset": 1,
                                                    "limit": 20,
                                                },
                                            }
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        payload = json.loads(request.content)
        assert "src/components/data-table.tsx" in _payload_message_text(payload)
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
                                                "reason": "TanStack table implementation",
                                            }
                                        ],
                                        "notes": [],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext.refine_candidate(
        candidate_id,
        "Focused FastContext query text",
        transport=httpx.MockTransport(handler),
        task_signature_override=parent_signature,
    )

    rows = catalog.get_connection().execute(
        "SELECT task_signature FROM evidence_refinements"
    ).fetchall()
    assert result["task_signature"] == parent_signature
    assert result["query_signature"] == catalog.task_signature("Focused FastContext query text")
    assert rows == [(parent_signature,)]


@pytest.mark.asyncio
async def test_explore_local_project_returns_ephemeral_citations(tmp_path: Path) -> None:
    root = tmp_path / "local"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": lmstudio.DEFAULT_FASTCONTEXT_MODEL}]},
            )
        assert request.url.path == "/v1/chat/completions"
        chat_calls += 1
        payload = json.loads(request.content)
        assert payload["model"] == lmstudio.DEFAULT_FASTCONTEXT_MODEL
        if chat_calls == 1:
            assert "local-project-exploration" in payload["messages"][-1]["content"]
            assert "Find the data table" in payload["messages"][-1]["content"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "tool_calls": [
                                            {
                                                "tool": "GREP",
                                                "args": {
                                                    "pattern": "useReactTable",
                                                    "glob": "**/*.tsx",
                                                },
                                            }
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        assert "src/components/data-table.tsx" in _payload_message_text(payload)
        assert "tools" not in payload
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
                                                "reason": "Relevant table implementation",
                                            }
                                        ],
                                        "notes": ["Inspect this component before editing."],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext.explore_local_project(
        "Find the data table",
        project_path=root,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.project_path == str(root.resolve())
    assert result.evidence_paths == ["src/components/data-table.tsx:1-4"]
    assert result.notes == ["Inspect this component before editing."]
    assert result.tool_trace[0]["tools_enabled"] is True
    assert result.tool_trace[0]["tool_calls"] == ["Grep"]
    assert result.tool_trace[0]["tool_call_count"] == 1
    assert result.tool_trace[0]["observation_count"] == 1
    assert result.tool_trace[0]["finalization_reason"] == "enough_primary_source_ranges"
    assert result.tool_trace[1]["tools_enabled"] is False
    assert result.tool_trace[1]["tool_calls"] == []
    assert result.tool_trace[1]["final_citations"] == ["src/components/data-table.tsx:1-4"]

    conn = catalog.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM evidence_refinements").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reuse_outcomes").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_explore_local_project_recovers_from_invalid_citation(tmp_path: Path) -> None:
    root = tmp_path / "local"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": lmstudio.DEFAULT_FASTCONTEXT_MODEL}]},
            )
        chat_calls += 1
        payload = json.loads(request.content)
        if chat_calls == 1:
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
                                                    "path": "src/missing.ts",
                                                    "start_line": 1,
                                                    "end_line": 3,
                                                }
                                            ],
                                            "notes": [],
                                        }
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        if chat_calls == 2:
            assert "did not validate" in payload["messages"][-1]["content"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "tool_calls": [
                                            {
                                                "tool": "GREP",
                                                "args": {
                                                    "pattern": "useReactTable",
                                                    "glob": "**/*.tsx",
                                                },
                                            }
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        assert "src/components/data-table.tsx" in _payload_message_text(payload)
        assert "tools" not in payload
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
                                        "notes": ["Recovered after validation feedback."],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    result = await fastcontext.explore_local_project(
        "Find the data table",
        project_path=root,
        transport=httpx.MockTransport(handler),
    )

    assert result.evidence_paths == ["src/components/data-table.tsx:1-4"]
    assert result.notes == ["Recovered after validation feedback."]
    assert result.tool_trace[0]["final_citations"] == ["src/missing.ts:1-3"]
    assert result.tool_trace[0]["validation_notes"] == ["Skipped missing citation file: src/missing.ts"]
    assert result.tool_trace[1]["tool_calls"] == ["Grep"]


@pytest.mark.asyncio
async def test_explore_local_project_writes_trace_file(tmp_path: Path) -> None:
    root = tmp_path / "local"
    root.mkdir()
    _write_snapshot(root)
    trace_path = ".source_scout/fastcontext_traces/unit.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": lmstudio.DEFAULT_FASTCONTEXT_MODEL}]},
            )
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
                                                "end_line": 1,
                                            }
                                        ],
                                        "notes": [],
                                    }
                                }
                            )
                        }
                    }
                ]
            },
        )

    await fastcontext.explore_local_project(
        "Find the data table",
        project_path=root,
        transport=httpx.MockTransport(handler),
        trace_path=trace_path,
    )

    stored_trace = json.loads((root / trace_path).read_text(encoding="utf-8"))
    assert stored_trace["task"] == "Find the data table"
    assert stored_trace["trajectory"][0]["final_citations"] == [
        "src/components/data-table.tsx:1-1"
    ]


@pytest.mark.asyncio
async def test_refine_suite_writes_comparison_report(tmp_path: Path, monkeypatch) -> None:
    candidate_id = _create_candidate(tmp_path)
    suite_path = tmp_path / "suite.json"
    output_path = tmp_path / "report.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_id": "ui-reuse",
                "description": "unit suite",
                "tasks": [
                    {
                        "id": "table",
                        "task": "Find a reusable TanStack data table",
                        "capability": "data-table",
                        "expected_repo_ids": ["owner/repo"],
                        "acceptable_repo_ids": [],
                        "avoid_repo_ids": [],
                        "required_path_terms_any": ["data-table"],
                        "required_dependencies_any": ["@tanstack/react-table"],
                        "max_rank_for_hit": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    async def fake_ensure_fastcontext_available(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_refine_candidate(
        candidate_id: str,
        task: str,
        max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
        transport: httpx.AsyncBaseTransport | None = None,
        validate_model: bool = True,
    ) -> dict[str, object]:
        assert task == "Find a reusable TanStack data table"
        assert max_turns == 2
        assert validate_model is False
        assert transport is None
        return {
            "candidate_id": candidate_id,
            "task_signature": catalog.task_signature(task),
            "repo_id": "owner/repo",
            "snapshot_id": "snapshot",
            "capability": "data-table",
            "model_id": lmstudio.DEFAULT_FASTCONTEXT_MODEL,
            "prompt_version": fastcontext.PROMPT_VERSION,
            "schema_version": fastcontext.SCHEMA_VERSION,
            "refinement_id": "refined",
            "analysis_run_id": "run",
            "evidence_paths": ["src/components/data-table.tsx:1-4"],
            "notes": ["focused evidence"],
        }

    monkeypatch.setattr(fastcontext, "ensure_fastcontext_available", fake_ensure_fastcontext_available)
    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine_candidate)

    result = await fastcontext.refine_suite(
        str(suite_path),
        top_k=1,
        label="unit",
        output_path=output_path,
        max_turns=2,
    )

    assert result["report_path"] == str(output_path)
    assert result["metrics"]["candidate_count"] == 1
    assert result["metrics"]["completed_refinements"] == 1
    assert result["scoring_recommendation"]["status"] == "tie_breaker_ready"
    assert result["tasks"][0]["candidates"][0]["candidate_id"] == candidate_id
    assert result["tasks"][0]["candidates"][0]["refined_evidence_paths"] == [
        "src/components/data-table.tsx:1-4"
    ]

    stored_report = json.loads(output_path.read_text(encoding="utf-8"))
    assert stored_report["tasks"][0]["candidates"][0]["deterministic_evidence_paths"] == [
        "src/components/data-table.tsx:1-4"
    ]

    runs = catalog.get_connection().execute(
        """
        SELECT stage_name, status, model_id
        FROM analysis_runs
        WHERE stage_name = 'fastcontext-batch-refine'
        """
    ).fetchall()
    assert runs == [("fastcontext-batch-refine", "completed", lmstudio.DEFAULT_FASTCONTEXT_MODEL)]


def test_fastcontext_status_cli_prints_json(monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    async def fake_status(
        start_server: bool,
        smoke_test: bool,
        load_model: bool = False,
        context_length: int = 65_536,
        gpu: str = "max",
    ) -> dict[str, object]:
        assert start_server is True
        assert smoke_test is True
        assert load_model is True
        assert context_length == 65536
        assert gpu == "max"
        return {"reachable": True, "fastcontext_available": True}

    monkeypatch.setattr(main_module, "_fastcontext_status", fake_status)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "fastcontext-status",
            "--start-server",
            "--smoke-test",
            "--load-model",
        ],
    )
    main_module.main()
    captured = capsys.readouterr()
    assert '"fastcontext_available": true' in captured.out


@pytest.mark.asyncio
async def test_fastcontext_status_loads_model_when_requested(monkeypatch) -> None:
    import source_scout.__main__ as main_module

    loaded = False

    async def fake_validate_models(config: lmstudio.LMStudioConfig) -> dict[str, object]:
        return {
            "base_url": config.base_url,
            "models": [config.fastcontext_model],
            "gemma_model": config.gemma_model,
            "fastcontext_model": config.fastcontext_model,
            "gemma_available": False,
            "fastcontext_available": True,
        }

    def fake_model_inventory(config: lmstudio.LMStudioConfig) -> dict[str, object]:
        return {
            "downloaded_models": [config.fastcontext_model],
            "loaded_models": [config.fastcontext_model] if loaded else [],
            "configured_models": {
                "gemma": {
                    "model_id": config.gemma_model,
                    "downloaded": False,
                    "loaded": False,
                    "loaded_detail": None,
                },
                "fastcontext": {
                    "model_id": config.fastcontext_model,
                    "downloaded": True,
                    "loaded": loaded,
                    "loaded_detail": {"contextLength": 65536} if loaded else None,
                },
            },
        }

    def fake_load_fastcontext_model(
        config: lmstudio.LMStudioConfig,
        context_length: int,
        gpu: str,
    ) -> dict[str, object]:
        nonlocal loaded
        assert context_length == 65536
        assert gpu == "max"
        loaded = True
        return {"loaded": True, "model_id": config.fastcontext_model}

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(lmstudio, "validate_models", fake_validate_models)
    monkeypatch.setattr(lmstudio, "model_inventory", fake_model_inventory)
    monkeypatch.setattr(lmstudio, "load_fastcontext_model", fake_load_fastcontext_model)
    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    result = await main_module._fastcontext_status(
        start_server=False,
        smoke_test=False,
        load_model=True,
        context_length=65536,
        gpu="max",
    )

    configured = result["configured_models"]
    assert result["load_model"]["loaded"] is True
    assert configured["fastcontext"]["loaded"] is True
    assert configured["fastcontext"]["api_listed"] is True


def test_refine_evidence_cli_invokes_fastcontext(monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    async def fake_refine_candidate(
        candidate_id: str,
        task: str,
        max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
    ) -> dict[str, object]:
        assert candidate_id == "abc"
        assert task == "Find evidence"
        assert max_turns == 2
        return {"candidate_id": candidate_id, "evidence_paths": ["src/file.ts:1-2"]}

    monkeypatch.setattr(fastcontext, "refine_candidate", fake_refine_candidate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "refine-evidence",
            "--candidate-id",
            "abc",
            "--task",
            "Find evidence",
            "--max-turns",
            "2",
        ],
    )
    main_module.main()
    captured = capsys.readouterr()
    assert '"candidate_id": "abc"' in captured.out


def test_refine_evidence_cli_invokes_suite_batch(monkeypatch, capsys, tmp_path: Path) -> None:
    import source_scout.__main__ as main_module

    output_path = tmp_path / "report.json"

    async def fake_refine_suite(
        suite: str,
        top_k: int,
        label: str | None = None,
        output_path: Path | None = None,
        max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
        limit_tasks: int | None = None,
    ) -> dict[str, object]:
        assert suite == "ui-reuse"
        assert top_k == 2
        assert label == "unit"
        assert output_path == tmp_path / "report.json"
        assert max_turns == 3
        assert limit_tasks == 1
        return {
            "suite_id": "ui-reuse",
            "label": label,
            "metrics": {"candidate_count": 2},
            "scoring_recommendation": {"status": "tie_breaker_ready"},
            "report_path": str(output_path),
        }

    monkeypatch.setattr(fastcontext, "refine_suite", fake_refine_suite)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "refine-evidence",
            "--suite",
            "ui-reuse",
            "--top-k",
            "2",
            "--label",
            "unit",
            "--output",
            str(output_path),
            "--max-turns",
            "3",
            "--limit-tasks",
            "1",
        ],
    )
    main_module.main()
    captured = capsys.readouterr()
    assert '"suite_id": "ui-reuse"' in captured.out
    assert '"candidate_count": 2' in captured.out


def test_explore_local_cli_invokes_fastcontext(monkeypatch, capsys, tmp_path: Path) -> None:
    import source_scout.__main__ as main_module

    async def fake_explore_local_project(
        task: str,
        project_path: str | Path = ".",
        max_turns: int = fastcontext.DEFAULT_MAX_TURNS,
        trace_path: str | Path | None = None,
    ) -> object:
        assert task == "Find MCP tools"
        assert project_path == str(tmp_path)
        assert max_turns == 2
        assert trace_path == str(tmp_path / "trace.json")
        return fastcontext.LocalExploreResult(
            task=task,
            project_path=str(tmp_path),
            model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
            prompt_version=fastcontext.PROMPT_VERSION,
            schema_version=fastcontext.SCHEMA_VERSION,
            analyzer_version=fastcontext.ANALYZER_VERSION,
            status="completed",
            evidence_paths=["src/source_scout/server.py:1-20"],
            notes=["MCP tools are registered here."],
            tool_trace=[],
        )

    monkeypatch.setattr(fastcontext, "explore_local_project", fake_explore_local_project)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "source-scout",
            "explore-local",
            "--task",
            "Find MCP tools",
            "--project-path",
            str(tmp_path),
            "--max-turns",
            "2",
            "--format",
            "text",
            "--trace-path",
            str(tmp_path / "trace.json"),
        ],
    )
    main_module.main()
    captured = capsys.readouterr()
    assert "src/source_scout/server.py:1-20" in captured.out
    assert "MCP tools are registered here." in captured.out
