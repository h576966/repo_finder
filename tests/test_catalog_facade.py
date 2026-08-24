from source_scout import (
    catalog,
    catalog_assessments,
    catalog_assets,
    catalog_core,
    catalog_repositories,
    catalog_search,
)
from source_scout.catalog_scoring import _has_backend_path


def test_catalog_facade_reexports_concern_apis() -> None:
    assert catalog.get_connection is catalog_core.get_connection
    assert catalog.record_analysis_run is catalog_core.record_analysis_run
    assert catalog.upsert_repository is catalog_repositories.upsert_repository
    assert catalog.get_latest_snapshot_identity is catalog_repositories.get_latest_snapshot_identity
    assert catalog.upsert_asset is catalog_assets.upsert_asset
    assert catalog.search_assets is catalog_search.search_assets
    assert catalog.store_reuse_assessment is catalog_assessments.store_reuse_assessment
    assert catalog.task_signature is catalog_assessments.task_signature
    assert catalog._has_backend_path is _has_backend_path


def test_catalog_facade_reopens_data_through_shared_connection() -> None:
    first_connection = catalog_core.get_connection()
    assert catalog.get_connection() is first_connection

    repo_id = catalog_repositories.upsert_repository(
        {
            "owner": {"login": "facade-owner"},
            "name": "facade-repo",
            "html_url": "https://github.com/facade-owner/facade-repo",
            "private": False,
            "archived": False,
        },
        "facade-test",
    )

    catalog.reset_connection()

    reopened = catalog.get_repository(repo_id)
    assert reopened is not None
    assert reopened["repo_id"] == repo_id
    assert catalog_core.get_connection() is catalog.get_connection()
