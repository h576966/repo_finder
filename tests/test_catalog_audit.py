import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from source_scout import catalog, catalog_audit

FIXED_NOW = datetime(2026, 7, 3, tzinfo=UTC)


def _profile(
    *,
    usefulness: float = 0.85,
    extractability: float = 0.8,
    maintenance: float = 0.8,
    concerns: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "repository-profile-v3",
        "repository_type": "reference_application",
        "capabilities": [],
        "likely_usefulness": usefulness,
        "extractability": extractability,
        "maintenance_quality": maintenance,
        "needs_fastcontext": False,
        "concerns": concerns or [],
    }


def _store_repo(
    tmp_path: Path,
    owner: str,
    *,
    pushed_at: str = "2026-06-20T12:00:00Z",
    profile: dict[str, Any] | None = None,
    asset_score: float | None = 0.8,
    asset_capability: str = "data-table",
    asset_evidence_paths: list[str] | None = None,
    asset_synthesis: dict[str, Any] | None = None,
    failed_profile: bool = False,
    metadata: dict[str, Any] | None = None,
    snapshot_path: Path | None = None,
) -> str:
    repo_root = tmp_path / owner
    repo_root.mkdir()
    repo_metadata: dict[str, Any] = {
        "owner": {"login": owner},
        "name": "repo",
        "full_name": f"{owner}/repo",
        "html_url": f"https://github.com/{owner}/repo",
        "private": False,
        "archived": False,
        "mirror_url": None,
        "fork": False,
        "is_template": False,
        "language": "TypeScript",
        "size": 100,
        "created_at": "2026-01-01T00:00:00Z",
        "pushed_at": pushed_at,
        "topics": ["nextjs"],
        "stargazers_count": 10,
        "forks_count": 1,
    }
    if metadata:
        repo_metadata.update(metadata)
    repo_id = catalog.upsert_repository(repo_metadata, "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, f"{owner}sha", "main", snapshot_path or repo_root)
    card: dict[str, Any] = {
        "card_version": "repo-card-v1",
        "package_manifests": {},
        "tree_summary": {},
        "readme_excerpt": "",
        "stack_signals": {},
        "deterministic_features": {},
    }
    if profile is not None:
        card["repository_profile"] = profile
    card_id = catalog.upsert_repository_card(snapshot_id, card)
    if asset_score is not None:
        catalog.upsert_asset(
            snapshot_id,
            repo_id,
            asset_capability,
            {
                "entry_paths": ["components/data-table.tsx"],
                "dependency_paths": ["package.json"],
                "external_dependencies": ["@tanstack/react-table"],
                "evidence_paths": asset_evidence_paths
                if asset_evidence_paths is not None
                else ["components/data-table.tsx:1-4"],
                "synthesis": asset_synthesis if asset_synthesis is not None else {"ui_path_score": 0.8},
                "reuse_score": asset_score,
            },
        )
    if failed_profile:
        catalog.record_analysis_run(
            "profile",
            "failed",
            {"card_id": card_id, "error": "invalid profile"},
            repo_id=repo_id,
            snapshot_id=snapshot_id,
        )
    return repo_id


def _store_raw_repo(owner: str) -> str:
    return catalog.upsert_repository(
        {
            "owner": {"login": owner},
            "name": "repo",
            "full_name": f"{owner}/repo",
            "html_url": f"https://github.com/{owner}/repo",
            "private": False,
            "archived": False,
            "mirror_url": None,
            "fork": False,
            "is_template": False,
            "language": "TypeScript",
            "size": 100,
            "created_at": "2026-01-01T00:00:00Z",
            "pushed_at": "2026-06-20T12:00:00Z",
            "topics": ["nextjs"],
            "stargazers_count": 1,
            "forks_count": 0,
        },
        "test",
    )


def _snapshot_id(repo_id: str) -> str:
    row = (
        catalog.get_connection()
        .execute(
            "SELECT snapshot_id FROM snapshots WHERE repo_id = ?",
            [repo_id],
        )
        .fetchone()
    )
    assert row is not None
    return str(row[0])


def _add_asset(
    repo_id: str,
    capability: str,
    *,
    reuse_score: float,
    evidence_paths: list[str] | None = None,
    synthesis: dict[str, Any] | None = None,
) -> None:
    catalog.upsert_asset(
        _snapshot_id(repo_id),
        repo_id,
        capability,
        {
            "entry_paths": [f"components/{capability}.tsx"],
            "dependency_paths": ["package.json"],
            "external_dependencies": [],
            "evidence_paths": evidence_paths
            if evidence_paths is not None
            else [f"components/{capability}.tsx:1-4"],
            "synthesis": synthesis if synthesis is not None else {"noise_penalty": 0.0},
            "reuse_score": reuse_score,
        },
    )


def _repo_ids(items: list[dict[str, Any]]) -> set[str]:
    return {str(item["repo_id"]) for item in items}


def test_catalog_audit_reports_quality_buckets(tmp_path: Path) -> None:
    high = _store_repo(tmp_path, "high", profile=_profile(), asset_score=0.9)
    stale = _store_repo(
        tmp_path,
        "stale",
        pushed_at="2025-01-01T00:00:00Z",
        profile=_profile(),
        asset_score=0.9,
    )
    noisy = _store_repo(tmp_path, "forked", profile=_profile(), asset_score=0.9, metadata={"fork": True})
    unprofiled = _store_repo(tmp_path, "unprofiled", profile=None, asset_score=0.8)
    failed = _store_repo(tmp_path, "failed", profile=None, asset_score=0.8, failed_profile=True)
    low = _store_repo(tmp_path, "low", profile=_profile(), asset_score=None)

    report = catalog_audit.audit_catalog(limit_per_bucket=10, now=FIXED_NOW)

    assert report["summary"]["repositories"] == 6
    assert report["summary"]["total_repositories"] == 6
    assert report["summary"]["scope"] == "downloaded"
    assert report["summary"]["downloaded_repositories"] == 6
    assert report["summary"]["high_quality"] == 1
    assert report["summary"]["stale"] == 1
    assert report["summary"]["noisy"] == 1
    assert report["summary"]["unprofiled"] == 1
    assert report["summary"]["failed_profile"] == 1
    assert report["summary"]["low_signal"] == 1
    assert report["summary"]["discard_candidates"] == 2

    buckets = report["buckets"]
    assert _repo_ids(buckets["high_quality"]) == {high}
    assert _repo_ids(buckets["stale"]) == {stale}
    assert _repo_ids(buckets["noisy"]) == {noisy}
    assert _repo_ids(buckets["unprofiled"]) == {unprofiled}
    assert _repo_ids(buckets["failed_profile"]) == {failed}
    assert _repo_ids(buckets["low_signal"]) == {low}
    assert _repo_ids(buckets["discard_candidates"]) == {low, noisy}
    assert _repo_ids(report["recommendations"]["keep"]) == {high}
    assert _repo_ids(report["recommendations"]["reprofile"]) == {unprofiled, failed}
    assert _repo_ids(report["recommendations"]["discard"]) == {low, noisy}


def test_catalog_audit_bucket_filter_accepts_hyphen_alias(tmp_path: Path) -> None:
    failed = _store_repo(tmp_path, "failed", profile=None, asset_score=0.8, failed_profile=True)

    report = catalog_audit.audit_catalog(bucket="failed-profile", now=FIXED_NOW)

    assert set(report["buckets"]) == {"failed_profile"}
    assert _repo_ids(report["buckets"]["failed_profile"]) == {failed}


def test_catalog_audit_scope_filters_raw_and_missing_downloads(tmp_path: Path) -> None:
    downloaded = _store_repo(tmp_path, "downloaded", profile=_profile(), asset_score=0.9)
    cataloged = _store_repo(
        tmp_path,
        "cataloged",
        profile=_profile(),
        asset_score=0.9,
        snapshot_path=tmp_path / "missing-snapshot",
    )
    raw = _store_raw_repo("raw")

    default_report = catalog_audit.audit_catalog(now=FIXED_NOW)
    cataloged_report = catalog_audit.audit_catalog(scope="cataloged", now=FIXED_NOW)
    all_report = catalog_audit.audit_catalog(scope="all", now=FIXED_NOW)

    assert default_report["summary"]["repositories"] == 1
    assert default_report["summary"]["total_repositories"] == 3
    assert _repo_ids(default_report["recommendations"]["keep"]) == {downloaded}
    assert cataloged_report["summary"]["repositories"] == 2
    assert _repo_ids(cataloged_report["recommendations"]["keep"]) == {downloaded, cataloged}
    assert all_report["summary"]["repositories"] == 3
    assert raw in _repo_ids(all_report["recommendations"]["discard"])
    assert raw not in _repo_ids(default_report["recommendations"]["discard"])


def test_catalog_audit_downloaded_uses_current_snapshot_path_fallback(tmp_path: Path) -> None:
    fallback = catalog.snapshot_path("historic", "repo", "historicsha")
    fallback.mkdir(parents=True)
    repo_id = _store_repo(
        tmp_path,
        "historic",
        profile=_profile(),
        asset_score=0.9,
        snapshot_path=tmp_path / "old-missing-path",
    )

    report = catalog_audit.audit_catalog(bucket="high-quality", now=FIXED_NOW)

    [item] = report["buckets"]["high_quality"]
    assert item["repo_id"] == repo_id
    assert item["downloaded"] is True


def test_catalog_audit_capability_tiers_and_explosion_review_labels(tmp_path: Path) -> None:
    repo_id = _store_repo(
        tmp_path,
        "broad",
        profile=_profile(),
        asset_score=0.9,
        asset_synthesis={"noise_penalty": 0.0, "capability_signal_score": 0.8},
    )
    _add_asset(
        repo_id,
        "forms",
        reuse_score=0.55,
        synthesis={"noise_penalty": 0.1, "capability_signal_score": 0.3},
    )
    _add_asset(repo_id, "settings", reuse_score=0.95, evidence_paths=[], synthesis={"noise_penalty": 0.0})
    for index in range(12):
        _add_asset(
            repo_id,
            f"strong-{index}",
            reuse_score=0.9,
            synthesis={"noise_penalty": 0.0, "capability_signal_score": 0.8},
        )

    report = catalog_audit.audit_catalog(bucket="noisy", now=FIXED_NOW)
    [item] = report["buckets"]["noisy"]

    assert item["repo_id"] == repo_id
    assert item["recommended_action"] == "keep_review_labels"
    assert item["capability_counts"]["strong"] == 13
    assert item["capability_counts"]["possible"] == 1
    assert item["capability_counts"]["weak_noisy"] == 1
    assert "forms" in item["capability_tiers"]["possible"]
    assert "settings" in item["capability_tiers"]["weak_noisy"]
    assert any(reason.startswith("capability explosion:") for reason in item["reasons"])
    assert report["summary"]["discard_candidates"] == 0
    assert _repo_ids(report["recommendations"]["keep"]) == {repo_id}


def test_catalog_audit_weak_legacy_label_does_not_count_toward_explosion(tmp_path: Path) -> None:
    repo_id = _store_repo(tmp_path, "legacy", profile=_profile(), asset_score=None)
    for index in range(24):
        _add_asset(
            repo_id,
            f"legacy-{index}",
            reuse_score=0.95,
            synthesis={"noise_penalty": 0.0},
        )
    _add_asset(
        repo_id,
        "data-table",
        reuse_score=0.8,
        synthesis={"noise_penalty": 0.0, "capability_signal_score": 0.8},
    )

    report = catalog_audit.audit_catalog(now=FIXED_NOW)
    [item] = report["recommendations"]["keep"]

    assert item["repo_id"] == repo_id
    assert item["capability_counts"]["strong"] == 1
    assert item["capability_counts"]["weak_noisy"] == 24
    assert item["capability_counts"]["actionable"] == 1
    assert item["recommended_action"] == "keep"
    assert not any(reason.startswith("capability explosion:") for reason in item["reasons"])


def test_catalog_audit_cli_prints_json(tmp_path: Path, monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    repo_id = _store_repo(tmp_path, "high", profile=_profile(), asset_score=0.9)
    monkeypatch.setattr(
        sys,
        "argv",
        ["source_scout", "audit", "--bucket", "high_quality", "--scope", "downloaded", "--limit", "1"],
    )

    main_module.main()

    output = json.loads(capsys.readouterr().out)
    assert output["summary"]["repositories"] == 1
    assert _repo_ids(output["buckets"]["high_quality"]) == {repo_id}
