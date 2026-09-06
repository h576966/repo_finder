"""Separate processes and historical data contracts for the shared collection."""

import asyncio
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
import pytest

from source_scout import catalog, implementation_references
from source_scout.file_lock import file_lock
from tests.test_implementation_references import _repository

pytestmark = pytest.mark.usefixtures("isolated_catalog")


def _cli(*args):
    result = subprocess.run(
        [sys.executable, "-m", "source_scout", *args], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_idle_process_releases_database_for_other_sessions(tmp_path):
    script = """
from source_scout.catalog_core import get_connection
with get_connection() as conn:
    assert conn.execute('SELECT 1').fetchone()[0] == 1
print('ready', flush=True)
input()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        found = _cli("references", "find", "--task", "jitter retry")
        assert found["status"] == "abstained"
        assert process.poll() is None
    finally:
        process.communicate("exit\n", timeout=10)
    assert process.returncode == 0


def test_simultaneous_cli_add_find_and_shared_immutable_snapshot(tmp_path):
    root, _ = _repository(tmp_path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        adds = [pool.submit(_cli, "references", "add", "--source", str(root)) for _ in range(2)]
        find = pool.submit(_cli, "references", "find", "--task", "bounded jitter retry")
        results = [future.result() for future in adds]
        assert find.result()["status"] in {"abstained", "matches"}
    assert results[0]["snapshot_id"] == results[1]["snapshot_id"]
    match = _cli("references", "find", "--task", "bounded decorrelated jitter retry schedule")
    assert len(match["results"]) == 1
    context = _cli("references", "context", "--reference-id", match["results"][0]["reference_id"])
    assert context["commit_sha"] == results[0]["commit_sha"]


def test_failed_add_rolls_back_all_catalog_tables(monkeypatch, tmp_path):
    root, _ = _repository(tmp_path)

    def fail(*args, **kwargs):
        raise ValueError("injected mid-transaction failure")

    monkeypatch.setattr(implementation_references.catalog_repositories, "upsert_snapshot", fail)
    with pytest.raises(ValueError, match="mid-transaction"):
        asyncio.run(implementation_references.add_reference_source(root))
    with catalog.get_connection() as conn:
        for table in ("repositories", "snapshots", "reference_sources", "implementation_references"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_lock_wait_is_bounded_and_failed_transaction_releases_it(isolated_catalog):
    lock = isolated_catalog / "probe.lock"
    with file_lock(lock):
        with ThreadPoolExecutor(max_workers=1) as pool:

            def wait():
                started = time.monotonic()
                with pytest.raises(TimeoutError, match="busy"):
                    with file_lock(lock, timeout=0.1):
                        pytest.fail("lock was not held")
                return time.monotonic() - started

            assert pool.submit(wait).result() < 2
    with file_lock(lock, timeout=0.1):
        pass


def test_historical_schema_columns_ids_and_records_survive(isolated_catalog):
    db = catalog.catalog_db_path()
    with duckdb.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE repository_cards (card_id TEXT, gemma_profile TEXT, repository_profile TEXT)"
        )
        conn.execute("INSERT INTO repository_cards VALUES ('old-id', 'historical', NULL)")
    with catalog.get_connection() as conn:
        row = conn.execute("SELECT * FROM repository_cards").fetchone()
        assert row == ("old-id", "historical", "historical")
        assert {c[0] for c in conn.description} == {"card_id", "gemma_profile", "repository_profile"}
    assert catalog.read_records("repository_cards")["rows"][0]["card_id"] == "old-id"


def test_gc_migration_does_not_remove_references_or_shared_snapshots(tmp_path):
    root, _ = _repository(tmp_path)
    added = asyncio.run(implementation_references.add_reference_source(root))
    snapshot = catalog.get_snapshot(added["snapshot_id"])
    shared = catalog.upsert_snapshot(
        added["repo_id"],
        added["commit_sha"],
        "main",
        Path(snapshot["snapshot_path"]),
        analyzer_version="historical-other",
    )
    result = subprocess.run(
        [sys.executable, "-m", "source_scout", "gc", "--keep-per-repo", "1"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and "retired" in result.stderr
    assert Path(snapshot["snapshot_path"]).is_dir()
    assert catalog.get_snapshot(shared)
    assert (
        implementation_references.find_implementation_references("bounded jitter retry").status == "matches"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows long path contract")
def test_long_windows_snapshot_paths_need_no_global_git_setting(tmp_path, monkeypatch):
    root, _ = _repository(tmp_path)
    collection = tmp_path / ("long-" * 20) / ("nested-" * 15) / "collection"
    monkeypatch.setenv("SOURCE_SCOUT_HOME", str(collection))
    added = asyncio.run(implementation_references.add_reference_source(root))
    snapshot = catalog.get_snapshot(added["snapshot_id"])
    assert len(snapshot["snapshot_path"]) > 260
    found = implementation_references.find_implementation_references("bounded jitter retry")
    assert (
        implementation_references.get_implementation_reference(found.results[0].reference_id).commit_sha
        == added["commit_sha"]
    )
