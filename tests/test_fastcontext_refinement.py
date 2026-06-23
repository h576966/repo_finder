import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from source_scout import catalog, fastcontext, lmstudio
from tests.fastcontext_helpers import _create_candidate, _payload_message_text, isolated_catalog

pytestmark = pytest.mark.usefixtures(isolated_catalog.__name__)

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
