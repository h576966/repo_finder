import json
from pathlib import Path

import httpx
import pytest

from source_scout import fastcontext, lmstudio
from tests.fastcontext_helpers import _write_budget_snapshot, _write_snapshot, isolated_catalog

pytestmark = pytest.mark.usefixtures(isolated_catalog.__name__)

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
                                                    "path": "tests/test_fastcontext_loop.py",
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
                                        "notes": ["Too many on final turn."],
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
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-priority",
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
                                        "id": "call-helper",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/helper.tsx",
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
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "final_answer": {
                                            "citation_ids": ["C2"],
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
        assert "observed task-priority path" in payload["messages"][-1]["content"]
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
                                        "notes": ["Used priority path."],
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
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-priority",
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
                                        "id": "call-helper",
                                        "type": "function",
                                        "function": {
                                            "name": "Read",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "src/components/helper.tsx",
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
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "final_answer": {
                                        "citation_ids": ["C2"],
                                        "notes": [],
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
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-priority",
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
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "I found the relevant file."}}]},
        )

    result = await fastcontext._run_tool_loop(
        root=root,
        messages=[{"role": "user", "content": "Find the data table"}],
        model_id=lmstudio.DEFAULT_FASTCONTEXT_MODEL,
        config=lmstudio.get_config(),
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
