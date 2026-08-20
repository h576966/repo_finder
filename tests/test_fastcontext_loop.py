import json
from pathlib import Path

import httpx
import pytest

from source_scout import deepseek, fastcontext
from source_scout.fastcontext_constants import MAX_TOOL_CALLS_PER_TURN
from tests.fastcontext_helpers import (
    _response_message_json,
    _response_tool_call_json,
    _response_tool_calls_json,
    _write_budget_snapshot,
    _write_snapshot,
)


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_uses_openai_tool_calls(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        assert request.url.path == "/responses"
        chat_calls += 1
        payload = json.loads(request.content)
        if chat_calls == 1:
            assert payload["tools"][0]["name"] == "Read"
            assert payload["reasoning"] == {"effort": "none"}
            assert payload["text"]["format"]["type"] == "json_schema"
            return httpx.Response(
                200,
                json={
                    "id": "resp-read",
                    "object": "response",
                    "created_at": 0,
                    "model": deepseek.DEEPSEEK_MODEL,
                    "output": [
                        {
                            "id": "fc-read-1",
                            "type": "function_call",
                            "call_id": "call-read-1",
                            "name": "Read",
                            "arguments": json.dumps(
                                {
                                    "path": "src/components/data-table.tsx",
                                    "offset": 1,
                                    "limit": 1,
                                }
                            ),
                            "status": "completed",
                        },
                        {
                            "id": "fc-read-2",
                            "type": "function_call",
                            "call_id": "call-read-2",
                            "name": "Read",
                            "arguments": json.dumps(
                                {
                                    "path": "src/components/data-table.tsx",
                                    "offset": 3,
                                    "limit": 1,
                                }
                            ),
                            "status": "completed",
                        },
                    ],
                    "parallel_tool_calls": True,
                    "status": "completed",
                    "text": {"format": {"type": "text"}},
                },
            )

        assert "tools" not in payload
        tool_messages = [
            message for message in payload["input"] if message.get("type") == "function_call_output"
        ]
        assert tool_messages[-1]["call_id"] == "call-read-2"
        assert "src/components/data-table.tsx:3-3" in tool_messages[-1]["output"]
        assert payload["input"][-1]["role"] == "user"
        assert "final_answer JSON" in payload["input"][-1]["content"]
        assert "Observed citation choices" in payload["input"][-1]["content"]
        assert "C1: src/components/data-table.tsx:1-1" in payload["input"][-1]["content"]
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "citation_ids": ["C1"],
                            "notes": ["Observed with Read."],
                        }
                    }
                )
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
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
async def test_fastcontext_answers_every_capped_deepseek_tool_call(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0
    tool_calls = [
        (
            "Read",
            {"path": "src/components/data-table.tsx", "offset": 1, "limit": 1},
            f"call-read-{index}",
        )
        for index in range(1, MAX_TOOL_CALLS_PER_TURN + 2)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        chat_calls += 1
        payload = json.loads(request.content)
        if chat_calls == 1:
            return httpx.Response(
                200,
                json=_response_tool_calls_json(tool_calls),
            )

        tool_messages = [
            message for message in payload["input"] if message.get("type") == "function_call_output"
        ]
        assert len(tool_messages) == len(tool_calls)
        skipped = next(
            message
            for message in tool_messages
            if message["call_id"] == f"call-read-{MAX_TOOL_CALLS_PER_TURN + 1}"
        )
        assert "per-turn limit" in skipped["output"]
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "citation_ids": ["C1"],
                            "notes": [],
                        }
                    }
                )
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id="deepseek-v4-flash",
        config=deepseek.get_config(),
        max_turns=2,
        transport=httpx.MockTransport(handler),
    )

    assert chat_calls == 2
    assert result.status == "completed"
    assert len(result.trajectory[0]["tool_observations"]) == len(tool_calls)
    assert result.trajectory[0]["tool_observations"][-1]["ok"] is False


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_nudges_no_tool_turn_to_priority_path(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        chat_calls += 1
        payload = json.loads(request.content)
        if chat_calls == 1:
            assert "tools" in payload
            return httpx.Response(
                200,
                json=_response_message_json("The answer is probably in the data table."),
            )
        if chat_calls == 2:
            assert "tools" in payload
            assert "You did not call a tool" in payload["input"][-1]["content"]
            assert "src/components/data-table.tsx" in payload["input"][-1]["content"]
            return httpx.Response(
                200,
                json=_response_tool_call_json(
                    "Read",
                    {
                        "path": "src/components/data-table.tsx",
                        "offset": 1,
                        "limit": 1,
                    },
                    call_id="call-read-priority",
                ),
            )

        assert "tools" not in payload
        assert "C1: src/components/data-table.tsx:1-1" in payload["input"][-1]["content"]
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "citation_ids": ["C1"],
                            "notes": ["Read after priority-path nudge."],
                        }
                    }
                )
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
        max_turns=3,
        transport=httpx.MockTransport(handler),
        priority_paths=["src/components/data-table.tsx"],
    )

    assert result.status == "completed"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-1"]
    assert result.notes == ["Read after priority-path nudge."]
    assert result.trajectory[0]["validation_notes"] == [
        "Model did not call a tool; nudging it to inspect generated priority paths."
    ]
    assert result.trajectory[1]["tool_calls"][0]["tool"] == "Read"


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_surfaces_deepseek_401_without_content_fallback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        chat_calls += 1
        assert request.url.path == "/responses"
        payload = json.loads(request.content)
        assert payload["tools"][0]["name"] == "Read"
        assert payload["reasoning"] == {"effort": "none"}
        return httpx.Response(
            401,
            json={"error": {"message": "Invalid API key"}},
        )

    with pytest.raises(deepseek.ModelError, match="HTTP 401"):
        await fastcontext._run_tool_loop(
            root=root,
            messages=[{"role": "user", "content": "Find the data table"}],
            model_id="deepseek-v4-flash",
            config=deepseek.get_config(),
            max_turns=1,
            transport=httpx.MockTransport(handler),
        )

    assert chat_calls == 1


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
            json=_response_tool_call_json(
                "Read",
                {
                    "path": "src/components/data-table.tsx",
                    "offset": 1,
                    "limit": 4,
                },
                call_id="call-read",
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
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
                json=_response_tool_call_json(
                    "Read",
                    {
                        "path": "src/components/data-table.tsx",
                        "offset": 1,
                        "limit": 4,
                    },
                    call_id="call-read",
                ),
            )
        if chat_calls == 2:
            assert "tools" in payload
            assert "not enough strong citation support" in payload["input"][-1]["content"]
            return httpx.Response(
                200,
                json=_response_message_json(
                    json.dumps(
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
                ),
            )
        raise AssertionError("Unexpected extra chat turn")

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
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
    (root / "tests" / "test_fastcontext_loop.py").write_text(
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
                json=_response_tool_calls_json(
                    [
                        (
                            "Read",
                            {
                                "path": "src/source_scout/fastcontext.py",
                                "offset": 1,
                                "limit": 120,
                            },
                            "call-read-source",
                        ),
                        (
                            "Read",
                            {"path": "README.md", "offset": 1, "limit": 1},
                            "call-read-docs",
                        ),
                        (
                            "Read",
                            {
                                "path": "tests/test_fastcontext_loop.py",
                                "offset": 1,
                                "limit": 2,
                            },
                            "call-read-tests",
                        ),
                    ]
                ),
            )
        assert "tools" in payload
        assert "primary source, broad" in payload["input"][-1]["content"]
        assert "supporting/noisy" in payload["input"][-1]["content"]
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "citation_ids": ["C1"],
                            "notes": ["Completed after continued exploration."],
                        }
                    }
                )
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find FastContext implementation"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
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
                json=_response_tool_calls_json(
                    [
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 1,
                                "limit": 1,
                            },
                            "call-read-1",
                        ),
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 3,
                                "limit": 1,
                            },
                            "call-read-2",
                        ),
                    ]
                ),
            )
        if chat_calls == 2:
            assert "tools" not in payload
            return httpx.Response(
                200,
                json=_response_message_json(
                    json.dumps(
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
                ),
            )
        assert "tools" not in payload
        assert "wildcard or glob citation" in payload["input"][-1]["content"]
        assert "C1: src/components/data-table.tsx:1-1" in payload["input"][-1]["content"]
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "citation_ids": ["C1"],
                            "notes": ["Used exact observed citation."],
                        }
                    }
                )
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
        max_turns=3,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-1"]
    assert result.trajectory[0]["finalization_reason"] == "enough_primary_source_ranges"
    assert result.trajectory[1]["validation_notes"] == ["Skipped wildcard or glob citation: src/**/*.tsx:1-4"]
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
                json=_response_tool_calls_json(
                    [
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 1,
                                "limit": 1,
                            },
                            "call-read-1",
                        ),
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 3,
                                "limit": 1,
                            },
                            "call-read-2",
                        ),
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 5,
                                "limit": 1,
                            },
                            "call-read-3",
                        ),
                        (
                            "Read",
                            {"path": "src/components/form.tsx", "offset": 1, "limit": 1},
                            "call-read-4",
                        ),
                    ]
                ),
            )
        if chat_calls == 2:
            assert "tools" not in payload
            assert "1-3 citation IDs" in payload["input"][-1]["content"]
            return httpx.Response(
                200,
                json=_response_message_json(
                    json.dumps(
                        {
                            "final_answer": {
                                "citation_ids": ["C1", "C2", "C3", "C4"],
                                "notes": ["Too many."],
                            }
                        }
                    )
                ),
            )
        assert "tools" not in payload
        assert "selected too many citations" in payload["input"][-1]["content"]
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "citation_ids": ["C1", "C2"],
                            "notes": ["Narrowed."],
                        }
                    }
                )
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
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
async def test_fastcontext_tool_loop_accepts_truncated_budget_on_final_turn(
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
                json=_response_tool_calls_json(
                    [
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 1,
                                "limit": 1,
                            },
                            "call-read-1",
                        ),
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 3,
                                "limit": 1,
                            },
                            "call-read-2",
                        ),
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 5,
                                "limit": 1,
                            },
                            "call-read-3",
                        ),
                        (
                            "Read",
                            {"path": "src/components/form.tsx", "offset": 1, "limit": 1},
                            "call-read-4",
                        ),
                    ]
                ),
            )
        assert "tools" not in payload
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "citation_ids": ["C1", "C2", "C3", "C4"],
                            "notes": ["Too many on final turn."],
                        }
                    }
                )
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
        max_turns=2,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.evidence_paths == [
        "src/components/data-table.tsx:1-1",
        "src/components/data-table.tsx:3-3",
        "src/components/data-table.tsx:5-5",
    ]
    assert result.trajectory[1]["citation_budget"]["over_budget"] is True
    assert chat_calls == 2


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
                json=_response_tool_calls_json(
                    [
                        ("Read", {"path": "README.md", "offset": 1, "limit": 1}, "call-readme"),
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 1,
                                "limit": 1,
                            },
                            "call-src-1",
                        ),
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 3,
                                "limit": 1,
                            },
                            "call-src-2",
                        ),
                        ("Read", {"path": "src/components/form.tsx", "offset": 1, "limit": 1}, "call-src-3"),
                    ]
                ),
            )
        assert "tools" not in payload
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "citation_ids": ["C1", "C2", "C3", "C4"],
                            "notes": ["Still too many."],
                        }
                    }
                )
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
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
                json=_response_tool_calls_json(
                    [
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 1,
                                "limit": 1,
                            },
                            "call-read-1",
                        ),
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 3,
                                "limit": 1,
                            },
                            "call-read-2",
                        ),
                    ]
                ),
            )
        if chat_calls == 2:
            assert "tools" not in payload
            return httpx.Response(
                200,
                json=_response_message_json(
                    json.dumps(
                        {
                            "final_answer": {
                                "citation_ids": ["C99"],
                                "notes": [],
                            }
                        }
                    )
                ),
            )
        if chat_calls == 3:
            assert "tools" not in payload
            assert "unknown citation_id" in payload["input"][-1]["content"]
            return httpx.Response(
                200,
                json=_response_message_json(
                    json.dumps(
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
                ),
            )
        raise AssertionError("Tools should not reopen after local fallback evidence exists.")

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
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
                json=_response_tool_call_json(
                    "Read",
                    {
                        "path": "/source_scout/src/components/data-table.tsx",
                        "offset": 1,
                        "limit": 4,
                    },
                    call_id="call-read",
                ),
            )
        assert "tools" not in payload
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
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
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
        max_turns=2,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-4"]


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_retries_priority_path_omission(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    helper = root / "src" / "components" / "helper.tsx"
    helper.write_text("export function Helper() { return null }\n", encoding="utf-8")
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        chat_calls += 1
        payload = json.loads(request.content)
        if chat_calls == 1:
            return httpx.Response(
                200,
                json=_response_tool_calls_json(
                    [
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 1,
                                "limit": 1,
                            },
                            "call-priority",
                        ),
                        (
                            "Read",
                            {
                                "path": "src/components/helper.tsx",
                                "offset": 1,
                                "limit": 1,
                            },
                            "call-helper",
                        ),
                    ]
                ),
            )
        if chat_calls == 2:
            assert "tools" not in payload
            return httpx.Response(
                200,
                json=_response_message_json(
                    json.dumps(
                        {
                            "final_answer": {
                                "citation_ids": ["C2"],
                                "notes": [],
                            }
                        }
                    )
                ),
            )

        assert "tools" not in payload
        assert "observed task-priority path" in payload["input"][-1]["content"]
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "citation_ids": ["C1"],
                            "notes": ["Used priority path."],
                        }
                    }
                )
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
        max_turns=3,
        transport=httpx.MockTransport(handler),
        priority_paths=["src/components/data-table.tsx"],
    )

    assert result.status == "completed"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-1"]
    assert any(
        "Final answer omitted observed task-priority path" in note
        for note in result.trajectory[1]["validation_notes"]
    )
    assert chat_calls == 3


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_uses_priority_observation_after_retry_omission(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    helper = root / "src" / "components" / "helper.tsx"
    helper.write_text("export function Helper() { return null }\n", encoding="utf-8")
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        chat_calls += 1
        if chat_calls == 1:
            return httpx.Response(
                200,
                json=_response_tool_calls_json(
                    [
                        (
                            "Read",
                            {
                                "path": "src/components/data-table.tsx",
                                "offset": 1,
                                "limit": 1,
                            },
                            "call-priority",
                        ),
                        (
                            "Read",
                            {
                                "path": "src/components/helper.tsx",
                                "offset": 1,
                                "limit": 1,
                            },
                            "call-helper",
                        ),
                    ]
                ),
            )
        return httpx.Response(
            200,
            json=_response_message_json(
                json.dumps(
                    {
                        "final_answer": {
                            "citation_ids": ["C2"],
                            "notes": [],
                        }
                    }
                )
            ),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
        max_turns=3,
        transport=httpx.MockTransport(handler),
        allow_observation_fallback=True,
        priority_paths=["src/components/data-table.tsx"],
    )

    assert result.status == "completed"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-1"]
    assert any("Accepted observed task-priority citations" in note for note in result.notes)
    assert chat_calls == 3


@pytest.mark.asyncio
async def test_fastcontext_tool_loop_completes_with_priority_observation_after_empty_retry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _write_snapshot(root)
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        chat_calls += 1
        if chat_calls == 1:
            return httpx.Response(
                200,
                json=_response_tool_call_json(
                    "Read",
                    {
                        "path": "src/components/data-table.tsx",
                        "offset": 1,
                        "limit": 1,
                    },
                    call_id="call-priority",
                ),
            )
        return httpx.Response(
            200,
            json=_response_message_json("I found the relevant file."),
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=deepseek.DEEPSEEK_MODEL,
        config=deepseek.get_config(),
        max_turns=3,
        transport=httpx.MockTransport(handler),
        allow_observation_fallback=True,
        priority_paths=["src/components/data-table.tsx"],
    )

    assert result.status == "completed"
    assert result.evidence_paths == ["src/components/data-table.tsx:1-1"]
    assert any("Accepted observed task-priority citations" in note for note in result.notes)
    assert chat_calls == 3


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
            json=_response_tool_call_json(
                "Read",
                {
                    "path": "src/components/data-table.tsx",
                    "offset": 1,
                    "limit": 4,
                },
                call_id="call-read",
            ),
        )

    with pytest.raises(fastcontext.FastContextLoopError):
        await fastcontext._run_tool_loop(
            root=root,
            messages=[{"role": "user", "content": "Find the data table"}],
            model_id=deepseek.DEEPSEEK_MODEL,
            config=deepseek.get_config(),
            max_turns=1,
            transport=httpx.MockTransport(handler),
            allow_observation_fallback=False,
        )
