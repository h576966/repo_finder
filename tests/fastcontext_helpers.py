import json
from pathlib import Path
from typing import Any

import pytest

from source_scout import catalog


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
