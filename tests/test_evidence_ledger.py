import hashlib
import json
from pathlib import Path

import pytest

from source_scout import catalog, evidence_ledger, pipeline


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(tmp_path / ".source_scout"))
    catalog.reset_connection()
    yield
    catalog.reset_connection()


def _write_lines(path: Path, count: int = 10, prefix: str = "line") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"{prefix} {index}" for index in range(1, count + 1)),
        encoding="utf-8",
    )


def _repo_metadata(owner: str = "owner", name: str = "repo") -> dict:
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


def test_ledger_materializes_deterministic_and_fastcontext_evidence_with_merged_origins(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source_scout"
    _write_lines(root / "src" / "a.ts", count=8, prefix="alpha")
    _write_lines(root / "src" / "b.ts", count=8, prefix="beta")

    result = evidence_ledger.build_evidence_ledger(
        snapshot_root=root,
        commit_sha="abc123",
        sources=[
            evidence_ledger.EvidenceSource("fastcontext", ["src/b.ts:1-1", "src/a.ts:2-4"]),
            evidence_ledger.EvidenceSource("deterministic", ["src/a.ts:2-4"]),
        ],
    )
    reversed_result = evidence_ledger.build_evidence_ledger(
        snapshot_root=root,
        commit_sha="abc123",
        sources=[
            evidence_ledger.EvidenceSource("deterministic", ["src/a.ts:2-4"]),
            evidence_ledger.EvidenceSource("fastcontext", ["src/a.ts:2-4", "src/b.ts:1-1"]),
        ],
    )

    assert [item["path"] for item in result.items] == ["src/a.ts", "src/b.ts"]
    assert result.items[0]["origins"] == ["deterministic", "fastcontext"]
    assert result.items[0]["start_line"] == 2
    assert result.items[0]["end_line"] == 4
    assert result.items[0]["commit_sha"] == "abc123"
    assert result.items[0]["snippet"].startswith("2|alpha 2\n3|alpha 3")
    selected = "alpha 2\nalpha 3\nalpha 4"
    assert result.items[0]["content_hash"] == f"sha256:{hashlib.sha256(selected.encode()).hexdigest()}"
    assert [item["evidence_id"] for item in result.items] == [
        item["evidence_id"] for item in reversed_result.items
    ]


def test_ledger_normalizes_workspace_prefixed_absolute_and_windows_paths(tmp_path: Path) -> None:
    root = tmp_path / "source_scout"
    _write_lines(root / "src" / "a.ts", count=3)

    result = evidence_ledger.build_evidence_ledger(
        snapshot_root=root,
        commit_sha="abc123",
        sources=[
            evidence_ledger.EvidenceSource(
                "deterministic",
                [
                    "/source_scout/src/a.ts:1-1",
                    "source_scout/src/a.ts:1-1",
                    "src\\a.ts:1-1",
                    f"{root / 'src' / 'a.ts'}:1-1",
                ],
            )
        ],
    )

    assert len(result.items) == 1
    assert result.items[0]["path"] == "src/a.ts"
    assert result.items[0]["origins"] == ["deterministic"]


def test_ledger_rejects_invalid_paths_ranges_and_missing_files(tmp_path: Path) -> None:
    root = tmp_path / "source_scout"
    outside = tmp_path / "outside.ts"
    _write_lines(root / "src" / "a.ts", count=5)
    _write_lines(outside, count=1)

    result = evidence_ledger.build_evidence_ledger(
        snapshot_root=root,
        commit_sha="abc123",
        sources=[
            evidence_ledger.EvidenceSource(
                "deterministic",
                [
                    f"{outside}:1-1",
                    "../outside.ts:1-1",
                    "src/missing.ts:1-1",
                    "src/a.ts:0-1",
                    "src/a.ts:4-2",
                    "src/a.ts:1-200",
                    "src/a.ts:7-8",
                    "src/a.ts",
                    "src/*.ts:1-1",
                    "generated/out.ts:1-1",
                    "vendor/lib.ts:1-1",
                ],
            )
        ],
    )

    assert result.items == []
    joined = "\n".join(result.validation_notes)
    assert "escapes snapshot root" in joined
    assert "missing evidence file" in joined
    assert "non-positive line range" in joined
    assert "reversed line range" in joined
    assert "overly broad evidence range" in joined
    assert "beyond EOF" in joined
    assert "without exact positive line range" in joined
    assert "wildcard or glob evidence path" in joined
    assert "skipped directory" in joined.lower()


def test_ledger_caps_snippets_item_count_and_total_prompt_characters(tmp_path: Path) -> None:
    root = tmp_path / "source_scout"
    for index in range(1, 5):
        _write_lines(root / "src" / f"{index}.ts", count=2, prefix="x" * 100)

    result = evidence_ledger.build_evidence_ledger(
        snapshot_root=root,
        commit_sha="abc123",
        sources=[
            evidence_ledger.EvidenceSource(
                "deterministic",
                [f"src/{index}.ts:1-1" for index in range(1, 5)],
            )
        ],
        max_items=3,
        max_chars_per_item=20,
        max_total_prompt_chars=45,
    )

    assert len(result.items) == 2
    assert all(len(item["snippet"]) <= 20 for item in result.items)
    assert result.total_prompt_characters <= 45
    assert result.truncated is True
    joined = "\n".join(result.validation_notes)
    assert "Capped evidence snippet characters" in joined
    assert "budget" in joined


def test_ledger_rejects_symlink_escape_where_supported(tmp_path: Path) -> None:
    root = tmp_path / "source_scout"
    root_src = root / "src"
    root_src.mkdir(parents=True)
    outside = tmp_path / "outside.ts"
    _write_lines(outside, count=1)
    link = root_src / "escape.ts"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlink creation is not supported in this environment: {exc}")

    result = evidence_ledger.build_evidence_ledger(
        snapshot_root=root,
        commit_sha="abc123",
        sources=[evidence_ledger.EvidenceSource("deterministic", ["src/escape.ts:1-1"])],
    )

    assert result.items == []
    assert any("escapes snapshot root" in note for note in result.validation_notes)


def test_candidate_ledger_reads_existing_matching_bundle_manifest(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    _write_lines(snapshot_root / "src" / "a.ts", count=3)
    (snapshot_root / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.0.0", "react": "19.0.0"}}),
        encoding="utf-8",
    )
    repo_id = catalog.upsert_repository(_repo_metadata(), source_channel="test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, pipeline.build_repository_card(snapshot_root))
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "route-handlers",
        {
            "entry_paths": ["src/a.ts"],
            "dependency_paths": ["package.json"],
            "external_dependencies": ["next", "react"],
            "evidence_paths": ["src/a.ts:1-1"],
            "synthesis": {},
            "reuse_score": 0.8,
        },
    )
    bundle_root = catalog.bundle_path(asset_id)
    bundle_root.mkdir(parents=True)
    (bundle_root / "bundle.json").write_text(
        json.dumps(
            {
                "candidate_id": asset_id,
                "task_signature": "task123",
                "commit_sha": "abc123",
                "source_snapshot": str(snapshot_root),
                "evidence_paths": ["src/a.ts:1-1"],
            }
        ),
        encoding="utf-8",
    )

    result = evidence_ledger.build_candidate_evidence_ledger(
        asset_id,
        task_signature="task123",
        fastcontext_evidence_paths=["src/a.ts:1-1"],
    )

    assert len(result.items) == 1
    assert result.items[0]["origins"] == ["deterministic", "fastcontext", "source_bundle"]
    assert result.items[0]["path"] == "src/a.ts"
    assert result.items[0]["commit_sha"] == "abc123"
