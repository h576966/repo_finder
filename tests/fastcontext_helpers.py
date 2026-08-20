import json
from pathlib import Path
from typing import Any

from source_scout import catalog


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
    messages = payload.get("input") or payload.get("messages", [])
    return "\n".join(str(message.get("content") or "") for message in messages if isinstance(message, dict))


def _response_message_json(content: str, *, response_id: str = "resp-1") -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 0,
        "model": "deepseek-v4-flash",
        "output": [
            {
                "id": f"{response_id}-message",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "status": "completed",
        "text": {"format": {"type": "text"}},
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _response_tool_call_json(
    tool: str,
    args: dict[str, Any],
    *,
    call_id: str = "call-1",
    response_id: str = "resp-1",
) -> dict[str, Any]:
    return _response_tool_calls_json([(tool, args, call_id)], response_id=response_id)


def _response_tool_calls_json(
    calls: list[tuple[str, dict[str, Any], str]],
    *,
    response_id: str = "resp-1",
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 0,
        "model": "deepseek-v4-flash",
        "output": [
            {
                "id": f"{response_id}-function-call-{index}",
                "type": "function_call",
                "call_id": call_id,
                "name": tool,
                "arguments": json.dumps(args),
                "status": "completed",
            }
            for index, (tool, args, call_id) in enumerate(calls, start=1)
        ],
        "parallel_tool_calls": True,
        "status": "completed",
        "text": {"format": {"type": "text"}},
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


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
