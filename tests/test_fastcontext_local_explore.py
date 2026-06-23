import json
from pathlib import Path

import httpx
import pytest

from source_scout import catalog, fastcontext, lmstudio
from tests.fastcontext_helpers import _payload_message_text, _write_snapshot, isolated_catalog

pytestmark = pytest.mark.usefixtures(isolated_catalog.__name__)

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
