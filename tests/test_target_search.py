import json
from pathlib import Path

from source_scout import catalog, pipeline
from source_scout.catalog_scoring import _capability_intent_scores
from source_scout.target_profile import build_target_profile


def _repo_metadata(name: str) -> dict[str, object]:
    return {
        "owner": {"login": "owner"},
        "name": name,
        "full_name": f"owner/{name}",
        "html_url": f"https://github.com/owner/{name}",
        "private": False,
        "archived": False,
        "mirror_url": None,
        "fork": False,
        "is_template": False,
        "language": "TypeScript",
        "size": 10,
        "created_at": "2026-01-15T00:00:00Z",
        "pushed_at": "2026-06-20T12:00:00Z",
        "topics": ["nextjs", "table"],
    }


def _candidate(
    tmp_path: Path,
    name: str,
    *,
    next_major: int,
    react_major: int,
    next_constraint: str | None = None,
    react_constraint: str | None = None,
) -> str:
    root = tmp_path / name
    (root / "components").mkdir(parents=True)
    (root / "components" / "data-table.tsx").write_text(
        "export function DataTable() { return null }\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "next": next_constraint or f"^{next_major}.0.0",
                    "react": react_constraint or f"^{react_major}.0.0",
                }
            }
        ),
        encoding="utf-8",
    )
    repo_id = catalog.upsert_repository(_repo_metadata(name), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, f"{name}-sha", "main", root)
    catalog.upsert_repository_card(snapshot_id, pipeline.build_repository_card(root))
    return catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "data-table",
        {
            "entry_paths": ["components/data-table.tsx"],
            "dependency_paths": ["package.json"],
            "external_dependencies": ["next", "react"],
            "evidence_paths": ["components/data-table.tsx:1-1"],
            "reuse_score": 0.9,
            "synthesis": {
                "capability_signal_score": 0.9,
                "capability_path_score": 1.0,
                "ui_path_score": 1.0,
            },
        },
    )


def _target(tmp_path: Path, name: str, *, next_major: int, react_major: int) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "app.tsx").write_text("export const App = () => null\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "next": f"^{next_major}.0.0",
                    "react": f"^{react_major}.0.0",
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def _role_candidate(
    tmp_path: Path,
    name: str,
    *,
    capability: str,
    entry_path: str,
    dependencies: list[str],
) -> str:
    root = tmp_path / name
    source = root / entry_path
    source.parent.mkdir(parents=True)
    source.write_text("export const reusable = true\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"dependencies": {dependency: "1.0.0" for dependency in dependencies}}),
        encoding="utf-8",
    )
    repo_id = catalog.upsert_repository(_repo_metadata(name), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, f"{name}-sha", "main", root)
    catalog.upsert_repository_card(snapshot_id, pipeline.build_repository_card(root))
    return catalog.upsert_asset(
        snapshot_id,
        repo_id,
        capability,
        {
            "entry_paths": [entry_path],
            "dependency_paths": ["package.json"],
            "external_dependencies": dependencies,
            "evidence_paths": [f"{entry_path}:1-1"],
            "reuse_score": 0.9,
            "synthesis": {
                "capability_signal_score": 0.9,
                "capability_path_score": 1.0,
                "ui_path_score": 1.0,
            },
        },
    )


def test_target_profile_reorders_equally_relevant_candidates(tmp_path: Path) -> None:
    next_14 = _candidate(tmp_path, "next14", next_major=14, react_major=18)
    next_15 = _candidate(tmp_path, "next15", next_major=15, react_major=19)
    target_14 = build_target_profile(
        _target(tmp_path, "target14", next_major=14, react_major=18)
    )
    target_15 = build_target_profile(
        _target(tmp_path, "target15", next_major=15, react_major=19)
    )

    results_14 = catalog.search_assets(
        "Find a reusable data table",
        max_repos=2,
        target_profile=target_14,
    )
    results_15 = catalog.search_assets(
        "Find a reusable data table",
        max_repos=2,
        target_profile=target_15,
    )

    assert results_14[0].candidate_id == next_14
    assert results_15[0].candidate_id == next_15
    assert results_14[0].target_fit_score > results_14[1].target_fit_score
    assert results_15[0].target_fit_score > results_15[1].target_fit_score
    assert "major-version conflict" in " ".join(results_15[1].target_fit_notes)


def test_search_abstains_without_task_relevance_signal(tmp_path: Path) -> None:
    _candidate(tmp_path, "table", next_major=15, react_major=19)

    results = catalog.search_assets(
        "Implement an OAuth device authorization grant poller",
        max_repos=3,
    )

    assert results == []


def test_search_abstains_on_generic_single_word_overlap(tmp_path: Path) -> None:
    _candidate(tmp_path, "table", next_major=15, react_major=19)

    results = catalog.search_assets(
        "Implement a Kubernetes operator for rotating SPIFFE workload certificates",
        max_repos=3,
    )

    assert results == []


def test_bm25_role_cards_recover_keyword_ablated_tasks(tmp_path: Path) -> None:
    table_id = _candidate(tmp_path, "table", next_major=15, react_major=19)
    palette_id = _role_candidate(
        tmp_path,
        "palette",
        capability="command-palette",
        entry_path="components/command-menu.tsx",
        dependencies=["cmdk"],
    )
    _role_candidate(
        tmp_path,
        "decoy",
        capability="file-storage",
        entry_path="src/storage/upload.ts",
        dependencies=["@aws-sdk/client-s3"],
    )

    table_results = catalog.search_assets(
        "A tabular record browser with sorting filters and pagination",
        max_repos=3,
    )
    palette_results = catalog.search_assets(
        "A keyboard-driven launcher overlay for navigating application actions",
        max_repos=3,
    )

    assert table_results[0].candidate_id == table_id
    assert all(result.capability == "data-table" for result in table_results)
    assert palette_results[0].candidate_id == palette_id
    assert all(result.capability == "command-palette" for result in palette_results)


def test_capability_intent_hints_do_not_match_word_substrings() -> None:
    scores = _capability_intent_scores(
        "Find a no_std Rust I2C sensor driver for an STM32 microcontroller"
    )

    assert scores["file-storage"] == 0.0


def test_target_fit_keeps_unknown_npm_major_neutral(tmp_path: Path) -> None:
    unknown_id = _candidate(
        tmp_path,
        "unknown",
        next_major=15,
        react_major=19,
        next_constraint="workspace:^15.0.0",
    )
    profile = build_target_profile(
        _target(tmp_path, "target", next_major=15, react_major=19)
    )

    result = catalog.search_assets(
        "Find a reusable data table",
        max_repos=1,
        target_profile=profile,
    )[0]

    assert result.candidate_id == unknown_id
    assert result.target_fit_score == 0.09
    assert "compatibility is unknown" in " ".join(result.target_fit_notes)
