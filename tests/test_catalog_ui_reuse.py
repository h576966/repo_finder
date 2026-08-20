import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from source_scout import assessor, bundles, capabilities, catalog, catalog_scoring, evidence, pipeline
from source_scout.models import AdaptationStep, AssessmentDimensions, ReuseAssessmentResult


def _repo_metadata(owner: str, name: str, **overrides) -> dict:
    metadata = {
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
    metadata.update(overrides)
    return metadata


def _store_bundle_assessment(
    asset_id: str,
    task_signature: str,
    *,
    source_paths: list[str] | None = None,
    verdict: str = "select",
    schema_version: str | None = None,
    commit_sha: str | None = None,
) -> str:
    asset = catalog.get_asset_detail(asset_id)
    assert asset is not None
    selected_paths = (
        source_paths if source_paths is not None else [str(path) for path in asset["entry_paths"]]
    )
    assessment = ReuseAssessmentResult(
        candidate_id=asset_id,
        repo_id=str(asset["repo_id"]),
        snapshot_id=str(asset["snapshot_id"]),
        commit_sha=commit_sha or str(asset["commit_sha"]),
        task="Test bundle",
        task_signature=task_signature,
        model_id="test-model",
        prompt_version=assessor.PROMPT_VERSION,
        schema_version=schema_version or assessor.SCHEMA_VERSION,
        analyzer_version=assessor.ANALYZER_VERSION,
        input_fingerprint=f"input-{task_signature}",
        fastcontext_policy="never",
        fastcontext_status="not_requested",
        license_status="unknown",
        recommended_verdict=verdict,
        final_verdict=verdict,
        reuse_score=0.9,
        model_confidence=0.9,
        confidence=0.9,
        evidence_coverage=1.0,
        requirement_count=1,
        satisfied_requirement_count=1,
        evidence_requirement_count=1,
        dimensions=AssessmentDimensions(0.9, 0.9, 0.9, 0.1, 0.1),
        adaptation_steps=[AdaptationStep("Reuse the selected source.", selected_paths)],
        evidence_ledger=[
            {
                "path": path,
                "start_line": 1,
                "end_line": 1,
                "commit_sha": commit_sha or str(asset["commit_sha"]),
                "content_hash": (
                    "sha256:"
                    + hashlib.sha256(
                        (Path(str(asset["snapshot_path"])) / path)
                        .read_text(encoding="utf-8", errors="replace")
                        .splitlines()[0]
                        .encode()
                    ).hexdigest()
                ),
                "validated": True,
            }
            for path in selected_paths
        ],
    )
    return catalog.store_reuse_assessment(assessment)


def _create_simple_bundle_asset(tmp_path: Path) -> str:
    snapshot_root = tmp_path / "bundle-snapshot"
    (snapshot_root / "src").mkdir(parents=True)
    (snapshot_root / "src" / "entry.ts").write_text(
        "export const ok = true\n",
        encoding="utf-8",
    )
    repo_id = catalog.upsert_repository(_repo_metadata("owner", "bundle-repo"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "bundle-sha", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
    return catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "route-handlers",
        {
            "entry_paths": ["src/entry.ts"],
            "dependency_paths": [],
            "external_dependencies": [],
            "evidence_paths": ["src/entry.ts:1-1"],
            "synthesis": {},
            "reuse_score": 0.8,
        },
    )


def _write_nextjs_fixture(root: Path) -> None:
    (root / "components" / "data-table").mkdir(parents=True)
    (root / "components" / "data-table" / "data-table.tsx").write_text(
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
        json.dumps(
            {
                "dependencies": {
                    "next": "15.0.0",
                    "react": "19.0.0",
                    "@tanstack/react-table": "8.0.0",
                    "tailwindcss": "4.0.0",
                },
                "devDependencies": {"typescript": "5.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (root / "tsconfig.json").write_text("{}", encoding="utf-8")


def _write_python_ai_fixture(root: Path) -> None:
    (root / "src" / "assistant").mkdir(parents=True)
    (root / "src" / "assistant" / "rag.py").write_text(
        "\n".join(
            [
                "from fastapi import FastAPI",
                "from openai import OpenAI",
                "import duckdb",
                "",
                "app = FastAPI()",
                "client = OpenAI(base_url='http://127.0.0.1:1234/v1')",
                "",
                "def semantic_search(query: str):",
                "    embedding = client.embeddings.create(model='local', input=query)",
                "    return duckdb.sql('select * from chunks').fetchall()",
            ]
        ),
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "assistant-tools"',
                'dependencies = ["fastapi", "openai", "duckdb", "pydantic", "typer"]',
            ]
        ),
        encoding="utf-8",
    )


def _write_trpc_fixture(root: Path) -> None:
    (root / "src" / "server" / "trpc").mkdir(parents=True)
    (root / "src" / "server" / "trpc" / "router.ts").write_text(
        "\n".join(
            [
                "import { initTRPC } from '@trpc/server'",
                "const t = initTRPC.create()",
                "export const appRouter = t.router({",
                "  health: t.procedure.query(() => 'ok'),",
                "})",
            ]
        ),
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"@trpc/server": "11.0.0", "next": "15.0.0"}}),
        encoding="utf-8",
    )


def _write_command_palette_fixture(root: Path) -> None:
    (root / "components" / "ui").mkdir(parents=True)
    (root / "components" / "ui" / "command.tsx").write_text(
        "\n".join(
            [
                "import { CommandDialog, CommandInput } from 'cmdk'",
                "export function CommandPalette() {",
                "  return <CommandDialog><CommandInput /></CommandDialog>",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"cmdk": "1.0.0", "next": "15.0.0", "react": "19.0.0"}}),
        encoding="utf-8",
    )


def _write_admin_export_fixture(root: Path) -> None:
    (root / "src" / "lib" / "export").mkdir(parents=True)
    (root / "src" / "lib" / "export" / "pdf.ts").write_text(
        "\n".join(
            [
                "import jsPDF from 'jspdf'",
                "export function exportInvoicePdf(rows: string[]) {",
                "  const doc = new jsPDF()",
                "  doc.save('invoice.pdf')",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"jspdf": "2.5.0", "next": "15.0.0"}}),
        encoding="utf-8",
    )


def _write_background_job_fixture(root: Path) -> None:
    (root / "worker" / "src").mkdir(parents=True)
    (root / "worker" / "src" / "queue.ts").write_text(
        "\n".join(
            [
                "export async function runScheduledQueue() {",
                "  await processQueue()",
                "}",
                "async function processQueue() { return 'done' }",
            ]
        ),
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.0.0", "react": "19.0.0"}}),
        encoding="utf-8",
    )


def _write_generic_admin_fixture(root: Path) -> None:
    (root / "src" / "app" / "admin").mkdir(parents=True)
    (root / "src" / "app" / "admin" / "page.tsx").write_text(
        "\n".join(
            [
                "export default function AdminPage() {",
                "  return <main><h1>Dashboard</h1><p>Manage staff and reports</p></main>",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "@trpc/server": "11.0.0",
                    "jspdf": "2.5.0",
                    "next": "15.0.0",
                    "react": "19.0.0",
                }
            }
        ),
        encoding="utf-8",
    )


def _profile(capability_name: str, evidence_paths: list[str], confidence: float = 0.9) -> dict:
    return {
        "schema_version": "repository-profile-v3",
        "repository_type": "reference_application",
        "capabilities": [{"name": capability_name, "confidence": confidence, "evidence": evidence_paths}],
        "likely_usefulness": 0.8,
        "extractability": 0.8,
        "maintenance_quality": 0.7,
        "needs_fastcontext": True,
        "concerns": [],
    }


def _all_zero_profile() -> dict:
    return {
        "schema_version": "repository-profile-v3",
        "repository_type": "examples",
        "capabilities": [],
        "likely_usefulness": 0,
        "extractability": 0,
        "maintenance_quality": 0,
        "needs_fastcontext": True,
        "concerns": [],
    }


def test_capability_constants_cover_catalog_scoring_representatives() -> None:
    assert {"data-table", "command-palette"} <= capabilities.UI_CAPABILITIES
    assert {"route-handlers", "data-access", "background-jobs"} <= capabilities.BACKEND_CAPABILITIES
    assert {"llm-harness", "rag-retrieval", "python-api"} <= capabilities.AI_DATA_CAPABILITIES
    assert "personal-code" in capabilities.DOMAIN_CAPABILITIES
    assert "tanstack" in capabilities.CAPABILITY_INTENT_HINTS["data-table"]
    assert "api route" in capabilities.CAPABILITY_INTENT_HINTS["route-handlers"]
    assert {"api", "route"} <= capabilities.BACKEND_CAPABILITY_PATH_TERMS["route-handlers"]
    assert {"db", "prisma"} <= capabilities.BACKEND_CAPABILITY_PATH_TERMS["data-access"]
    assert {"rag", "retrieval"} <= capabilities.CAPABILITY_PATH_TERMS["rag-retrieval"]
    assert {"cron", "worker"} <= capabilities.BACKGROUND_JOB_STRONG_TERMS
    assert "service-worker" in capabilities.BACKGROUND_JOB_FALSE_POSITIVE_TERMS


def test_profile_match_score_ignores_free_form_capability_labels() -> None:
    profile = _profile(
        "Perfect TanStack data table with command palette",
        ["components/data-table.tsx"],
        confidence=1.0,
    )
    labeled_score = catalog_scoring._profile_match_score(profile)
    profile["capabilities"] = []
    quality_only_score = catalog_scoring._profile_match_score(profile)

    assert labeled_score == quality_only_score
    assert 0 < quality_only_score < 0.35


def test_search_assets_treats_all_zero_empty_profile_as_absent(tmp_path: Path) -> None:
    def store_asset(owner: str, profile: dict | None) -> str:
        root = tmp_path / owner
        root.mkdir()
        _write_nextjs_fixture(root)
        repo_id = catalog.upsert_repository(
            _repo_metadata(owner, "repo", topics=["nextjs", "table"]),
            "test",
        )
        snapshot_id = catalog.upsert_snapshot(repo_id, f"{owner}sha", "main", root)
        card = pipeline.build_repository_card(root)
        if profile is not None:
            card["repository_profile"] = profile
        catalog.upsert_repository_card(snapshot_id, card)
        return catalog.upsert_asset(
            snapshot_id,
            repo_id,
            "data-table",
            evidence.scan_snapshot(root, "data-table"),
        )

    no_profile_asset_id = store_asset("without-profile", None)
    empty_profile_asset_id = store_asset("empty-profile", _all_zero_profile())

    results = catalog.search_assets("Find a reusable data table", max_repos=5)
    scores = {result.candidate_id: result.score for result in results}

    assert scores[empty_profile_asset_id] == scores[no_profile_asset_id]


def test_catalog_schema_and_paths() -> None:
    conn = catalog.get_connection()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert {
        "repositories",
        "snapshots",
        "repository_cards",
        "assets",
        "reuse_outcomes",
        "analysis_runs",
        "evidence_refinements",
    }.issubset(tables)

    path = catalog.snapshot_path("owner", "repo", "abc123")
    assert path.name == "abc123"
    assert "owner__repo" in str(path)

    columns = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'repositories'
            """
        ).fetchall()
    }
    assert {"repo_created_at", "is_fork", "is_template"}.issubset(columns)


def test_historic_snapshot_path_resolves_to_source_scout_home(tmp_path: Path) -> None:
    current_snapshot = catalog.snapshot_path("owner", "repo", "abc123")
    current_snapshot.mkdir(parents=True)
    _write_nextjs_fixture(current_snapshot)
    historic_snapshot = tmp_path / ("." + "repo" + "_finder") / "repos" / "owner__repo" / "abc123"
    repo_id = catalog.upsert_repository(_repo_metadata("owner", "repo"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", historic_snapshot)
    catalog.upsert_repository_card(snapshot_id, pipeline.build_repository_card(current_snapshot))
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "data-table",
        {
            "entry_paths": ["components/data-table/data-table.tsx"],
            "dependency_paths": ["package.json"],
            "external_dependencies": ["@tanstack/react-table"],
            "evidence_paths": ["components/data-table/data-table.tsx:1-5"],
            "synthesis": {},
            "reuse_score": 1.0,
        },
    )

    detail = catalog.get_asset_detail(asset_id)
    snapshots = catalog.list_snapshots_for_evidence(limit=1)

    assert detail is not None
    assert Path(str(detail["snapshot_path"])) == current_snapshot.resolve()
    assert Path(str(snapshots[0]["snapshot_path"])) == current_snapshot.resolve()


def test_repository_card_and_ui_gates(tmp_path: Path) -> None:
    _write_nextjs_fixture(tmp_path)
    card = pipeline.build_repository_card(tmp_path)
    assert card["stack_signals"]["has_react_dependency"] is True
    assert card["stack_signals"]["has_next_dependency"] is True
    assert card["stack_signals"]["has_typescript_files"] is True

    metadata = _repo_metadata("owner", "repo")
    ok, reason = pipeline._passes_ui_gates(metadata, card)
    assert ok is True
    assert reason == "qualified"


def test_python_repository_card_and_reuse_gates(tmp_path: Path) -> None:
    _write_python_ai_fixture(tmp_path)
    card = pipeline.build_repository_card(tmp_path)

    assert card["stack_signals"]["has_python_files"] is True
    assert card["stack_signals"]["has_python_manifest"] is True
    assert card["stack_signals"]["has_python_ai_data_dependency"] is True
    assert card["deterministic_features"]["python_source_file_count"] == 1
    assert card["deterministic_features"]["python_manifest_count"] == 1

    metadata = _repo_metadata("owner", "python-ai", language="Python", topics=["ai"])
    ok, reason = pipeline._passes_reuse_gates(metadata, card)

    assert ok is True
    assert reason == "qualified"


def test_modern_python_dependency_formats_are_indexed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "retrieval.py").write_text(
        "def retrieve_context(query):\n    return embed(query)\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "modern-python"',
                "dependencies = []",
                "",
                "[dependency-groups]",
                'ai = ["openai>=2"]',
                'lint = [{ include-group = "ai" }, "ruff>=0.12"]',
                "",
                "[tool.poetry.group.analytics.dependencies]",
                'duckdb = ">=1"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements-test.txt").write_text("pydantic>=2\n", encoding="utf-8")

    card = pipeline.build_repository_card(tmp_path)
    package_manifests = card["package_manifests"]
    declared_dependencies = {
        dependency
        for manifest in package_manifests.values()
        for section in ("dependencies", "devDependencies")
        for dependency in manifest[section]
    }
    result = evidence.scan_snapshot(tmp_path, "rag-retrieval")

    assert {"openai", "ruff", "duckdb", "pydantic"} <= declared_dependencies
    assert card["deterministic_features"]["python_manifest_count"] == 2
    assert {"openai", "duckdb", "pydantic"} <= set(result["external_dependencies"])
    assert "requirements-test.txt" in result["dependency_paths"]


def test_modern_javascript_and_typescript_extensions_are_scanned(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    for filename in ("index.mjs", "worker.cjs", "types.mts", "config.cts"):
        (source_root / filename).write_text(
            f"export const {filename.split('.', 1)[0]} = true\n",
            encoding="utf-8",
        )
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    card = pipeline.build_repository_card(tmp_path)
    scanned_paths = {path.relative_to(tmp_path).as_posix() for path in evidence.collect_scan_files(tmp_path)}

    assert card["stack_signals"]["has_typescript_files"] is True
    assert card["stack_signals"]["has_javascript_or_typescript_files"] is True
    assert card["deterministic_features"]["source_file_count"] == 4
    assert card["deterministic_features"]["js_ts_source_file_count"] == 4
    assert {
        "src/index.mjs",
        "src/worker.cjs",
        "src/types.mts",
        "src/config.cts",
    } <= scanned_paths


def test_scout_queries_filter_archived_stale_large_and_old_repos() -> None:
    query = pipeline.build_nextjs_ui_queries()[0][1]

    assert "archived:false" in query
    assert "mirror:false" in query
    assert "template:false" in query
    assert "is:public" in query
    assert "created:>=" in query
    assert "pushed:>=" in query
    assert f"size:<={pipeline.MAX_REPOSITORY_SIZE_KB}" in query


def test_personal_code_queries_filter_archived_stale_large_and_old_repos() -> None:
    queries = pipeline.build_domain_queries("personal-code")
    joined = "\n".join(query for _, query in queries)

    assert any(capability == "rag-retrieval" for capability, _ in queries)
    assert any(capability == "node-ai-sdk" for capability, _ in queries)
    assert "language:Python" in joined
    assert "language:TypeScript" in joined
    assert "archived:false" in joined
    assert "mirror:false" in joined
    assert "template:false" in joined
    assert "is:public" in joined
    assert "created:>=" in joined
    assert "pushed:>=" in joined
    assert f"size:<={pipeline.MAX_REPOSITORY_SIZE_KB}" in joined


def test_nextjs_ui_domain_still_builds_compatibility_queries() -> None:
    assert pipeline.build_domain_queries("nextjs-ui") == pipeline.build_nextjs_ui_queries()


def test_metadata_gates_reject_stale_old_forks_templates_mirrors_and_large_repos() -> None:
    base = _repo_metadata("owner", "repo")

    assert pipeline._passes_metadata_gates({**base, "archived": True}) == (False, "archived")
    assert pipeline._passes_metadata_gates({**base, "private": True}) == (False, "not public")
    assert pipeline._passes_metadata_gates({**base, "mirror_url": "https://mirror"}) == (
        False,
        "mirror",
    )
    assert pipeline._passes_metadata_gates({**base, "fork": True}) == (False, "fork")
    assert pipeline._passes_metadata_gates({**base, "is_template": True}) == (False, "template")
    assert pipeline._passes_metadata_gates({**base, "size": pipeline.MAX_REPOSITORY_SIZE_KB + 1}) == (
        False,
        "too large",
    )
    assert pipeline._passes_metadata_gates({**base, "created_at": None}) == (
        False,
        "missing created_at",
    )
    assert pipeline._passes_metadata_gates({**base, "created_at": "2023-01-01T00:00:00Z"}) == (
        False,
        "too old",
    )
    assert pipeline._passes_metadata_gates({**base, "pushed_at": None}) == (
        False,
        "missing pushed_at",
    )
    assert pipeline._passes_metadata_gates({**base, "pushed_at": "2025-01-01T00:00:00Z"}) == (
        False,
        "stale",
    )


def test_repository_upsert_stores_freshness_and_repo_kind_metadata() -> None:
    repo_id = catalog.upsert_repository(
        _repo_metadata("owner", "repo", fork=True, is_template=True),
        "test",
    )

    stored = catalog.get_repository(repo_id)

    assert stored is not None
    assert stored["repo_created_at"] == "2026-01-15T00:00:00Z"
    assert stored["is_fork"] is True
    assert stored["is_template"] is True


def test_list_repositories_for_qualification_filters_archived_large_forks_templates_and_mirrors() -> None:
    def store(name: str, archived: bool = False, size: int = 10, **overrides) -> None:
        catalog.upsert_repository(
            _repo_metadata("owner", name, archived=archived, size=size, **overrides),
            "test",
        )

    store("good")
    store("archived", archived=True)
    store("huge", size=pipeline.MAX_REPOSITORY_SIZE_KB + 1)
    store("fork", fork=True)
    store("template", is_template=True)
    store("mirror", mirror_url="https://mirror")

    candidates = catalog.list_repositories_for_qualification(10)

    assert [candidate["repo_id"] for candidate in candidates] == ["owner/good"]


def test_snapshot_content_gates_reject_docs_only_and_lockfile_only(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    (docs_root / "README.md").write_text("# Docs only", encoding="utf-8")
    docs_card = pipeline.build_repository_card(docs_root)

    assert pipeline._passes_snapshot_content_gates(docs_card) == (False, "docs-only or empty")

    lock_root = tmp_path / "lock"
    lock_root.mkdir()
    (lock_root / "package-lock.json").write_text("{}", encoding="utf-8")
    lock_card = pipeline.build_repository_card(lock_root)

    assert pipeline._passes_snapshot_content_gates(lock_card) == (False, "lockfile-only")


def test_snapshot_content_gates_reject_generated_vendor_heavy_repo(tmp_path: Path) -> None:
    _write_nextjs_fixture(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    for index in range(25):
        (dist / f"generated-{index}.js").write_text("export const value = 1", encoding="utf-8")

    card = pipeline.build_repository_card(tmp_path)

    assert card["deterministic_features"]["generated_vendor_file_count"] == 25
    assert pipeline._passes_snapshot_content_gates(card) == (False, "generated/vendor-heavy")


def test_evidence_scan_returns_line_citations_and_dependencies(tmp_path: Path) -> None:
    _write_nextjs_fixture(tmp_path)
    result = evidence.scan_snapshot(tmp_path, "data-table")
    assert "components/data-table/data-table.tsx" in result["entry_paths"]
    assert "@tanstack/react-table" in result["external_dependencies"]
    assert any(path.startswith("components/data-table/data-table.tsx:") for path in result["evidence_paths"])
    assert result["reuse_score"] > 0


def test_evidence_scan_returns_python_ai_assets(tmp_path: Path) -> None:
    _write_python_ai_fixture(tmp_path)

    result = evidence.scan_snapshot(tmp_path, "rag-retrieval")

    assert "src/assistant/rag.py" in result["entry_paths"]
    assert "openai" in result["external_dependencies"]
    assert "duckdb" in result["external_dependencies"]
    assert "pyproject.toml" in result["dependency_paths"]
    assert any(path.startswith("src/assistant/rag.py:") for path in result["evidence_paths"])
    assert result["reuse_score"] > 0


def test_evidence_scan_rejects_generic_admin_for_noisy_backend_labels(tmp_path: Path) -> None:
    _write_generic_admin_fixture(tmp_path)

    for capability in ("trpc-router", "email-webhooks", "admin-export"):
        result = evidence.scan_snapshot(tmp_path, capability)
        assert result["entry_paths"] == []
        assert result["evidence_paths"] == []
        assert result["reuse_score"] == 0


def test_manifest_only_dependency_evidence_cannot_be_strong(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"@tanstack/react-table": "8.0.0", "next": "15.0.0"}}),
        encoding="utf-8",
    )

    result = evidence.scan_snapshot(tmp_path, "data-table")

    assert result["evidence_paths"]
    assert result["reuse_score"] <= catalog_scoring.MANIFEST_ONLY_SCORE_CAP
    assert result["synthesis"]["manifest_only"] is True
    assert result["synthesis"]["capability_signal_score"] < catalog_scoring.STRONG_LABEL_SIGNAL


@pytest.mark.parametrize(
    ("capability", "writer"),
    [
        ("data-table", _write_nextjs_fixture),
        ("trpc-router", _write_trpc_fixture),
        ("command-palette", _write_command_palette_fixture),
        ("admin-export", _write_admin_export_fixture),
        ("background-jobs", _write_background_job_fixture),
    ],
)
def test_evidence_scan_keeps_real_capability_specific_assets(
    tmp_path: Path,
    capability: str,
    writer,
) -> None:
    writer(tmp_path)

    result = evidence.scan_snapshot(tmp_path, capability)

    assert result["entry_paths"]
    assert result["evidence_paths"]
    assert result["reuse_score"] >= 0.75
    assert result["synthesis"]["capability_signal_score"] >= catalog_scoring.STRONG_LABEL_SIGNAL


def test_ai_data_evidence_requires_specific_signal(tmp_path: Path) -> None:
    _write_nextjs_fixture(tmp_path)

    result = evidence.scan_snapshot(tmp_path, "data-pipeline")

    assert result["entry_paths"] == []
    assert result["evidence_paths"] == []
    assert result["reuse_score"] == 0


def test_run_evidence_domain_runs_every_mapped_capability(monkeypatch) -> None:
    called: list[str] = []

    def fake_run_evidence(capability: str, limit: int) -> dict[str, int]:
        called.append(f"{capability}:{limit}")
        return {"stored_assets": 1, "skipped_snapshots": 2}

    monkeypatch.setattr(evidence, "run_evidence", fake_run_evidence)

    result = evidence.run_evidence_domain("personal-code", 7)

    expected = list(capabilities.DOMAIN_CAPABILITIES["personal-code"])
    assert called == [f"{capability}:7" for capability in expected]
    assert result["stored_assets"] == len(expected)
    assert result["skipped_snapshots"] == len(expected) * 2


def test_evidence_cli_runs_domain(monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module

    def fake_run_evidence_domain(domain: str, limit: int) -> dict[str, object]:
        return {"domain": domain, "stored_assets": limit, "skipped_snapshots": 0}

    monkeypatch.setattr(evidence, "run_evidence_domain", fake_run_evidence_domain)
    monkeypatch.setattr(
        sys,
        "argv",
        ["source_scout", "evidence", "--domain", "personal-code", "--limit", "9"],
    )

    main_module.main()

    captured = capsys.readouterr()
    assert "'domain': 'personal-code'" in captured.out
    assert "'stored_assets': 9" in captured.out


def test_scout_cli_defaults_to_personal_code(monkeypatch, capsys) -> None:
    import source_scout.__main__ as main_module
    from source_scout import pipeline as pipeline_module

    async def fake_scout(domain: str, limit: int) -> dict[str, int]:
        assert domain == "personal-code"
        assert limit == 5
        return {"stored_repositories": 5}

    monkeypatch.setattr(main_module, "_require_github_token", lambda: None)
    monkeypatch.setattr(pipeline_module, "scout", fake_scout)
    monkeypatch.setattr(sys, "argv", ["source_scout", "scout", "--limit", "5"])

    main_module.main()

    captured = capsys.readouterr()
    assert "'stored_repositories': 5" in captured.out


def test_evidence_scan_prefers_ui_component_over_schema_noise(tmp_path: Path) -> None:
    _write_nextjs_fixture(tmp_path)
    (tmp_path / "drizzle").mkdir()
    (tmp_path / "drizzle" / "schema.ts").write_text(
        "\n".join(
            [
                "export const auditTable = table('audit', {",
                "  data: text('data'),",
                "  columns: text('columns'),",
                "  tableName: text('table_name'),",
                "})",
            ]
        ),
        encoding="utf-8",
    )

    result = evidence.scan_snapshot(tmp_path, "data-table")

    assert result["entry_paths"][0] == "components/data-table/data-table.tsx"
    assert "drizzle/schema.ts" not in result["entry_paths"]
    assert result["synthesis"]["ui_path_score"] > result["synthesis"]["noise_penalty"]


def test_server_action_scan_ignores_ui_action_components(tmp_path: Path) -> None:
    (tmp_path / "src" / "components" / "actions").mkdir(parents=True)
    (tmp_path / "src" / "components" / "actions" / "QuickActions.tsx").write_text(
        "export function QuickActions() { return <button>Run action</button> }",
        encoding="utf-8",
    )
    (tmp_path / "app" / "actions").mkdir(parents=True)
    (tmp_path / "app" / "actions" / "admin.ts").write_text(
        "\n".join(
            [
                '"use server"',
                "import { revalidatePath } from 'next/cache'",
                "export async function updateAdminSettings() {",
                "  revalidatePath('/admin')",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.0.0", "react": "19.0.0"}}),
        encoding="utf-8",
    )

    result = evidence.scan_snapshot(tmp_path, "server-actions")

    assert "app/actions/admin.ts" in result["entry_paths"]
    assert "src/components/actions/QuickActions.tsx" not in result["entry_paths"]


def test_delete_assets_for_snapshots_preserves_unscanned_assets(tmp_path: Path) -> None:
    asset_ids = []
    snapshot_ids = []
    for name in ("scanned", "unscanned"):
        root = tmp_path / name
        root.mkdir()
        repo_id = catalog.upsert_repository(_repo_metadata("owner", name), "test")
        snapshot_id = catalog.upsert_snapshot(repo_id, f"{name}sha", "main", root)
        snapshot_ids.append(snapshot_id)
        catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
        asset_ids.append(
            catalog.upsert_asset(
                snapshot_id,
                repo_id,
                "server-actions",
                {
                    "entry_paths": [f"app/actions/{name}.ts"],
                    "dependency_paths": ["package.json"],
                    "external_dependencies": ["next"],
                    "evidence_paths": [f"app/actions/{name}.ts:1-3"],
                    "reuse_score": 1.0,
                    "synthesis": {
                        "adaptation_notes": [],
                        "ui_path_score": 1.0,
                        "noise_penalty": 0.0,
                        "capability_path_score": 1.0,
                    },
                },
            )
        )

    catalog.delete_assets_for_snapshots("server-actions", [snapshot_ids[0]])

    assert catalog.get_asset_detail(asset_ids[0]) is None
    assert catalog.get_asset_detail(asset_ids[1]) is not None


def test_bundle_manifest_copies_evidence_files(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    _write_nextjs_fixture(snapshot_root)

    repo_id = catalog.upsert_repository(
        _repo_metadata("owner", "repo"),
        "test",
    )
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, pipeline.build_repository_card(snapshot_root))
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "data-table",
        evidence.scan_snapshot(snapshot_root, "data-table"),
    )

    assessment_id = _store_bundle_assessment(
        asset_id,
        "task123",
        source_paths=["components/data-table/data-table.tsx"],
    )
    result = bundles.create_source_bundle(assessment_id)
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["candidate_id"] == asset_id
    assert manifest["task_signature"] == "task123"
    assert "components/data-table/data-table.tsx" in manifest["copied_files"]
    assert manifest["missing_files"] == []
    assert (Path(result.bundle_path) / "source" / "components/data-table/data-table.tsx").exists()


def test_bundle_path_is_task_specific_and_includes_hashes_and_read_order(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    _write_nextjs_fixture(snapshot_root)

    repo_id = catalog.upsert_repository(_repo_metadata("owner", "repo"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, pipeline.build_repository_card(snapshot_root))
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "data-table",
        evidence.scan_snapshot(snapshot_root, "data-table"),
    )

    first_assessment_id = _store_bundle_assessment(asset_id, "task-one")
    second_assessment_id = _store_bundle_assessment(asset_id, "task-two")
    first = bundles.create_source_bundle(first_assessment_id)
    second = bundles.create_source_bundle(second_assessment_id)
    first_manifest = json.loads(Path(first.manifest_path).read_text(encoding="utf-8"))

    copied_file = "components/data-table/data-table.tsx"
    expected_hash = hashlib.sha256((snapshot_root / copied_file).read_bytes()).hexdigest()
    assert Path(first.bundle_path).parts[-2:] == (asset_id, first_assessment_id)
    assert second.bundle_path != first.bundle_path
    assert first.files == first_manifest["copied_files"]
    assert copied_file in first.recommended_read_order
    assert first_manifest["file_hashes"][copied_file] == expected_hash
    assert first.file_hashes[copied_file] == expected_hash


def test_bundle_ignores_unrelated_missing_asset_dependency_paths(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "src").mkdir()
    (snapshot_root / "src" / "entry.ts").write_text("export const ok = true\n", encoding="utf-8")

    repo_id = catalog.upsert_repository(_repo_metadata("owner", "repo"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "route-handlers",
        {
            "entry_paths": ["src/entry.ts"],
            "dependency_paths": ["missing.json"],
            "external_dependencies": [],
            "evidence_paths": ["src/entry.ts:1-1"],
            "synthesis": {"adaptation_notes": []},
            "reuse_score": 0.8,
        },
    )

    assessment_id = _store_bundle_assessment(asset_id, "task123")
    result = bundles.create_source_bundle(assessment_id)
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

    assert result.files == ["src/entry.ts"]
    assert result.missing_files == []
    assert manifest["missing_files"] == []
    assert "missing.json" not in manifest["file_hashes"]


def test_bundle_copies_evidence_only_files(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "src").mkdir()
    (snapshot_root / "src" / "entry.ts").write_text("export const ok = true\n", encoding="utf-8")
    (snapshot_root / "src" / "evidence.ts").write_text(
        "export const evidence = true\n",
        encoding="utf-8",
    )

    repo_id = catalog.upsert_repository(_repo_metadata("owner", "repo"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "route-handlers",
        {
            "entry_paths": ["src/entry.ts"],
            "dependency_paths": [],
            "external_dependencies": [],
            "evidence_paths": ["src/evidence.ts:1-1"],
            "synthesis": {},
            "reuse_score": 0.8,
        },
    )

    assessment_id = _store_bundle_assessment(
        asset_id,
        "task123",
        source_paths=["src/evidence.ts"],
    )
    result = bundles.create_source_bundle(assessment_id)
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

    assert "src/evidence.ts" in result.files
    assert result.evidence_paths == ["src/evidence.ts:1-1"]
    assert result.evidence_file_paths == ["src/evidence.ts"]
    assert manifest["evidence_paths"] == ["src/evidence.ts:1-1"]
    assert manifest["evidence_file_paths"] == ["src/evidence.ts"]
    assert manifest["recommended_read_order"][0] == "src/evidence.ts"
    assert (Path(result.bundle_path) / "source" / "src" / "evidence.ts").exists()


def test_bundle_rerun_removes_stale_source_files(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "src").mkdir()
    (snapshot_root / "src" / "entry.ts").write_text("export const ok = true\n", encoding="utf-8")

    repo_id = catalog.upsert_repository(_repo_metadata("owner", "repo"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "route-handlers",
        {
            "entry_paths": ["src/entry.ts"],
            "dependency_paths": [],
            "external_dependencies": [],
            "evidence_paths": ["src/entry.ts:1-1"],
            "synthesis": {},
            "reuse_score": 0.8,
        },
    )

    assessment_id = _store_bundle_assessment(asset_id, "task123")
    first = bundles.create_source_bundle(assessment_id)
    stale = Path(first.bundle_path) / "source" / "stale.ts"
    stale.write_text("export const stale = true\n", encoding="utf-8")

    second = bundles.create_source_bundle(assessment_id)

    assert second.bundle_path == first.bundle_path
    assert not stale.exists()
    assert (Path(second.bundle_path) / "source" / "src" / "entry.ts").exists()
    assert Path(second.manifest_path).exists()


def test_failed_bundle_rerun_preserves_previous_bundle(tmp_path: Path, monkeypatch) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "src").mkdir()
    (snapshot_root / "src" / "entry.ts").write_text(
        "export const ok = true\n",
        encoding="utf-8",
    )

    repo_id = catalog.upsert_repository(_repo_metadata("owner", "repo"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "route-handlers",
        {
            "entry_paths": ["src/entry.ts"],
            "dependency_paths": [],
            "external_dependencies": [],
            "evidence_paths": ["src/entry.ts:1-1"],
            "synthesis": {},
            "reuse_score": 0.8,
        },
    )
    assessment_id = _store_bundle_assessment(asset_id, "task123")
    first = bundles.create_source_bundle(assessment_id)
    manifest_path = Path(first.manifest_path)
    source_path = Path(first.bundle_path) / "source" / "src" / "entry.ts"
    original_manifest = manifest_path.read_text(encoding="utf-8")

    def fail_copy(*_args, **_kwargs) -> None:
        raise OSError("simulated copy failure")

    monkeypatch.setattr(bundles.shutil, "copy2", fail_copy)
    with pytest.raises(OSError, match="simulated copy failure"):
        bundles.create_source_bundle(assessment_id)

    assert manifest_path.read_text(encoding="utf-8") == original_manifest
    assert source_path.read_text(encoding="utf-8") == "export const ok = true\n"
    assert not list(Path(first.bundle_path).parent.glob(".*.staging-*"))


def test_bundle_rejects_unsafe_paths(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "src").mkdir()
    (snapshot_root / "src" / "entry.ts").write_text("export const ok = true\n", encoding="utf-8")
    (tmp_path / "escape.ts").write_text("export const escape = true\n", encoding="utf-8")

    repo_id = catalog.upsert_repository(_repo_metadata("owner", "repo"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "route-handlers",
        {
            "entry_paths": ["../escape.ts"],
            "dependency_paths": [],
            "external_dependencies": [],
            "evidence_paths": [],
            "synthesis": {},
            "reuse_score": 0.8,
        },
    )

    assessment_id = _store_bundle_assessment(
        asset_id,
        "task123",
        source_paths=["../escape.ts"],
    )
    with pytest.raises(ToolError, match="Unsafe source path|escapes snapshot root"):
        bundles.create_source_bundle(assessment_id)


def test_bundle_rejects_unsafe_assessment_id_path_segment(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "src").mkdir()
    (snapshot_root / "src" / "entry.ts").write_text("export const ok = true\n", encoding="utf-8")

    repo_id = catalog.upsert_repository(_repo_metadata("owner", "repo"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "abc123", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "route-handlers",
        {
            "entry_paths": ["src/entry.ts"],
            "dependency_paths": [],
            "external_dependencies": [],
            "evidence_paths": ["src/entry.ts:1-1"],
            "synthesis": {},
            "reuse_score": 0.8,
        },
    )

    with pytest.raises(ValueError, match="Unsafe bundle assessment_id"):
        catalog.assessment_bundle_path(asset_id, "../other-assessment")


@pytest.mark.parametrize("verdict", ["reject", "insufficient_evidence"])
def test_bundle_blocks_non_reusable_assessment_verdicts(tmp_path: Path, verdict: str) -> None:
    asset_id = _create_simple_bundle_asset(tmp_path)
    assessment_id = _store_bundle_assessment(asset_id, "task123", verdict=verdict)

    with pytest.raises(ToolError, match="cannot create a bundle"):
        bundles.create_source_bundle(assessment_id)


def test_bundle_marks_inspection_verdict(tmp_path: Path) -> None:
    asset_id = _create_simple_bundle_asset(tmp_path)
    assessment_id = _store_bundle_assessment(asset_id, "task123", verdict="inspect")

    result = bundles.create_source_bundle(assessment_id)
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

    assert result.bundle_mode == "inspection"
    assert result.warnings[0].startswith("Inspection bundle")
    assert manifest["bundle_schema_version"] == "source-bundle-v2"
    assert manifest["assessment_id"] == assessment_id


def test_bundle_requires_current_assessment_and_validated_source_paths(tmp_path: Path) -> None:
    asset_id = _create_simple_bundle_asset(tmp_path)
    stale_id = _store_bundle_assessment(
        asset_id,
        "stale-task",
        schema_version="reuse-assessment-v2",
    )
    no_source_id = _store_bundle_assessment(asset_id, "empty-task", source_paths=[])
    stale_commit_id = _store_bundle_assessment(
        asset_id,
        "commit-task",
        commit_sha="old-sha",
    )

    with pytest.raises(ToolError, match="schema is stale"):
        bundles.create_source_bundle(stale_id)
    with pytest.raises(ToolError, match="no validated adaptation source paths"):
        bundles.create_source_bundle(no_source_id)
    with pytest.raises(ToolError, match="no longer matches"):
        bundles.create_source_bundle(stale_commit_id)
    with pytest.raises(ToolError, match="Unknown assessment_id"):
        bundles.create_source_bundle("missing-assessment")


def test_bundle_rejects_superseded_snapshot_and_changed_evidence(tmp_path: Path) -> None:
    asset_id = _create_simple_bundle_asset(tmp_path)
    assessment_id = _store_bundle_assessment(asset_id, "task123")
    asset = catalog.get_asset_detail(asset_id)
    assert asset is not None
    snapshot_root = Path(str(asset["snapshot_path"]))

    (snapshot_root / "src" / "entry.ts").write_text(
        "export const changed = true\n",
        encoding="utf-8",
    )
    with pytest.raises(ToolError, match="evidence hash is stale"):
        bundles.create_source_bundle(assessment_id)

    (snapshot_root / "src" / "entry.ts").write_text(
        "export const ok = true\n",
        encoding="utf-8",
    )
    repo_id = str(asset["repo_id"])
    newer_root = tmp_path / "newer-snapshot"
    newer_root.mkdir()
    newer_snapshot_id = catalog.upsert_snapshot(repo_id, "newer-sha", "main", newer_root)
    catalog.get_connection().execute(
        "UPDATE snapshots SET indexed_at = ? WHERE snapshot_id = ?",
        ["9999-12-31T23:59:59+00:00", newer_snapshot_id],
    )
    with pytest.raises(ToolError, match="superseded catalog snapshot"):
        bundles.create_source_bundle(assessment_id)


def test_select_bundle_rejects_unparseable_required_source(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "python-bundle"
    (snapshot_root / "src").mkdir(parents=True)
    (snapshot_root / "src" / "entry.py").write_text("def broken(:\n", encoding="utf-8")
    repo_id = catalog.upsert_repository(_repo_metadata("owner", "python-bundle"), "test")
    snapshot_id = catalog.upsert_snapshot(repo_id, "python-sha", "main", snapshot_root)
    catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
    asset_id = catalog.upsert_asset(
        snapshot_id,
        repo_id,
        "python-api",
        {
            "entry_paths": ["src/entry.py"],
            "dependency_paths": [],
            "external_dependencies": [],
            "evidence_paths": ["src/entry.py:1-1"],
            "synthesis": {},
            "reuse_score": 0.8,
        },
    )
    assessment_id = _store_bundle_assessment(asset_id, "python-task")

    with pytest.raises(ToolError, match="dependency closure is incomplete"):
        bundles.create_source_bundle(assessment_id)


def test_select_bundle_reports_path_alias_without_guessing(tmp_path: Path) -> None:
    asset_id = _create_simple_bundle_asset(tmp_path)
    asset = catalog.get_asset_detail(asset_id)
    assert asset is not None
    entry = Path(str(asset["snapshot_path"])) / "src" / "entry.ts"
    entry.write_text(
        'import { helper } from "@/lib/helper"\nexport const ok = helper\n',
        encoding="utf-8",
    )
    assessment_id = _store_bundle_assessment(asset_id, "alias-task")

    result = bundles.create_source_bundle(assessment_id)

    assert result.bundle_mode == "assessment"
    assert result.unresolved_local_imports == ["src/entry.ts -> @/lib/helper (path_alias_not_supported)"]
    assert "without guessing" in result.warnings[0]


def test_search_assets_uses_repository_profile_and_ui_scores(tmp_path: Path) -> None:
    good_root = tmp_path / "good"
    good_root.mkdir()
    _write_nextjs_fixture(good_root)
    good_repo_id = catalog.upsert_repository(
        _repo_metadata("good", "repo", topics=["nextjs", "table"]),
        "test",
    )
    good_snapshot_id = catalog.upsert_snapshot(good_repo_id, "goodsha", "main", good_root)
    good_card = pipeline.build_repository_card(good_root)
    good_card["repository_profile"] = _profile(
        "Complex data tables",
        ["components/data-table/data-table.tsx"],
    )
    catalog.upsert_repository_card(good_snapshot_id, good_card)
    good_asset_id = catalog.upsert_asset(
        good_snapshot_id,
        good_repo_id,
        "data-table",
        evidence.scan_snapshot(good_root, "data-table"),
    )

    noisy_root = tmp_path / "noisy"
    noisy_root.mkdir()
    (noisy_root / "app" / "dashboard").mkdir(parents=True)
    (noisy_root / "app" / "dashboard" / "page.tsx").write_text(
        "export default function Dashboard() { return <main>Dashboard data</main> }",
        encoding="utf-8",
    )
    (noisy_root / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15", "react": "19", "tailwindcss": "4"}}),
        encoding="utf-8",
    )
    noisy_repo_id = catalog.upsert_repository(
        _repo_metadata("noisy", "repo", topics=["nextjs", "dashboard"]),
        "test",
    )
    noisy_snapshot_id = catalog.upsert_snapshot(noisy_repo_id, "noisysha", "main", noisy_root)
    noisy_card = pipeline.build_repository_card(noisy_root)
    noisy_card["repository_profile"] = _profile("Dashboard shell", ["app/dashboard/page.tsx"])
    catalog.upsert_repository_card(noisy_snapshot_id, noisy_card)
    catalog.upsert_asset(
        noisy_snapshot_id,
        noisy_repo_id,
        "data-table",
        {
            "entry_paths": ["app/dashboard/page.tsx"],
            "dependency_paths": ["package.json"],
            "external_dependencies": ["next", "react", "tailwindcss"],
            "evidence_paths": ["app/dashboard/page.tsx:1-1", "package.json:1-1"],
            "reuse_score": 0.82,
            "synthesis": {
                "adaptation_notes": ["Dashboard-only evidence should not satisfy data-table reuse."],
                "ui_path_score": 0.45,
                "noise_penalty": 0.2,
            },
        },
    )

    results = catalog.search_assets(
        "Find a reusable data table for a Next.js Tailwind dashboard",
        max_repos=2,
    )

    assert [result.candidate_id for result in results] == [good_asset_id]
    assert results[0].repo_id == "good/repo"
    assert results[0].task_signature == catalog.task_signature(
        "Find a reusable data table for a Next.js Tailwind dashboard"
    )


def test_search_assets_skips_weak_label_even_with_good_profile(tmp_path: Path) -> None:
    weak_root = tmp_path / "weak"
    weak_root.mkdir()
    _write_generic_admin_fixture(weak_root)
    weak_repo_id = catalog.upsert_repository(
        _repo_metadata("weak", "repo", topics=["nextjs", "admin"]),
        "test",
    )
    weak_snapshot_id = catalog.upsert_snapshot(weak_repo_id, "weaksha", "main", weak_root)
    weak_card = pipeline.build_repository_card(weak_root)
    weak_card["repository_profile"] = _profile("High quality app", ["src/app/admin/page.tsx"])
    catalog.upsert_repository_card(weak_snapshot_id, weak_card)
    catalog.upsert_asset(
        weak_snapshot_id,
        weak_repo_id,
        "trpc-router",
        {
            "entry_paths": ["src/app/admin/page.tsx"],
            "dependency_paths": ["package.json"],
            "external_dependencies": ["next", "react"],
            "evidence_paths": ["src/app/admin/page.tsx:1-3", "package.json:1-1"],
            "reuse_score": 1.0,
            "synthesis": {"ui_path_score": 0.9, "noise_penalty": 0.0},
        },
    )

    strong_root = tmp_path / "strong"
    strong_root.mkdir()
    _write_trpc_fixture(strong_root)
    strong_repo_id = catalog.upsert_repository(
        _repo_metadata("strong", "repo", topics=["nextjs", "trpc"]),
        "test",
    )
    strong_snapshot_id = catalog.upsert_snapshot(strong_repo_id, "strongsha", "main", strong_root)
    catalog.upsert_repository_card(strong_snapshot_id, pipeline.build_repository_card(strong_root))
    strong_asset_id = catalog.upsert_asset(
        strong_snapshot_id,
        strong_repo_id,
        "trpc-router",
        evidence.scan_snapshot(strong_root, "trpc-router"),
    )

    results = catalog.search_assets("Find a reusable tRPC router", max_repos=5)

    assert [result.candidate_id for result in results] == [strong_asset_id]


def test_search_assets_fail_closes_repos_without_freshness_metadata(tmp_path: Path) -> None:
    def store_asset(owner: str, metadata: dict) -> str:
        root = tmp_path / owner
        root.mkdir()
        stored_repo_id = catalog.upsert_repository(metadata, "test")
        snapshot_id = catalog.upsert_snapshot(stored_repo_id, f"{owner}sha", "main", root)
        catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
        return catalog.upsert_asset(
            snapshot_id,
            stored_repo_id,
            "data-table",
            {
                "entry_paths": ["components/data-table.tsx"],
                "dependency_paths": ["package.json"],
                "external_dependencies": ["@tanstack/react-table"],
                "evidence_paths": ["components/data-table.tsx:1-3"],
                "reuse_score": 1.0,
                "synthesis": {
                    "adaptation_notes": [],
                    "ui_path_score": 1.0,
                    "noise_penalty": 0.0,
                    "capability_path_score": 1.0,
                },
            },
        )

    stale_metadata = _repo_metadata("unknown", "repo")
    stale_metadata.pop("created_at")
    store_asset("unknown", stale_metadata)
    fresh_asset_id = store_asset("fresh", _repo_metadata("fresh", "repo"))

    results = catalog.search_assets("Find a reusable data table", max_repos=5)

    assert [result.candidate_id for result in results] == [fresh_asset_id]


def test_search_assets_only_uses_latest_snapshot_per_repository(tmp_path: Path) -> None:
    repo_id = catalog.upsert_repository(_repo_metadata("owner", "repo"), "test")
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    old_snapshot_id = catalog.upsert_snapshot(repo_id, "oldsha", "main", old_root)
    new_snapshot_id = catalog.upsert_snapshot(repo_id, "newsha", "main", new_root)
    catalog.get_connection().execute(
        "UPDATE snapshots SET indexed_at = ? WHERE snapshot_id = ?",
        ["2026-07-01T00:00:00+00:00", old_snapshot_id],
    )
    catalog.get_connection().execute(
        "UPDATE snapshots SET indexed_at = ? WHERE snapshot_id = ?",
        ["2026-07-02T00:00:00+00:00", new_snapshot_id],
    )

    common = {
        "entry_paths": ["components/data-table.tsx"],
        "dependency_paths": ["package.json"],
        "external_dependencies": ["@tanstack/react-table"],
        "evidence_paths": ["components/data-table.tsx:1-3"],
        "synthesis": {
            "adaptation_notes": [],
            "ui_path_score": 1.0,
            "noise_penalty": 0.0,
            "capability_path_score": 1.0,
        },
    }
    catalog.upsert_asset(
        old_snapshot_id,
        repo_id,
        "data-table",
        {**common, "reuse_score": 1.0},
    )
    latest_asset_id = catalog.upsert_asset(
        new_snapshot_id,
        repo_id,
        "data-table",
        {**common, "reuse_score": 0.1},
    )

    results = catalog.search_assets("Find a reusable data table", max_repos=1)

    assert [result.candidate_id for result in results] == [latest_asset_id]
    assert results[0].commit_sha == "newsha"


def test_search_assets_sorts_by_raw_score_before_display_clamp(tmp_path: Path) -> None:
    def store_asset(repo_id: str, entry_paths: list[str], dependencies: list[str], cap_path: float) -> str:
        owner, name = repo_id.split("/", 1)
        root = tmp_path / owner / name
        root.mkdir(parents=True)
        stored_repo_id = catalog.upsert_repository(
            _repo_metadata(owner, name, topics=["nextjs", "table"]),
            "test",
        )
        snapshot_id = catalog.upsert_snapshot(stored_repo_id, f"{owner}sha", "main", root)
        catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
        return catalog.upsert_asset(
            snapshot_id,
            stored_repo_id,
            "data-table",
            {
                "entry_paths": entry_paths,
                "dependency_paths": ["package.json"],
                "external_dependencies": dependencies,
                "evidence_paths": [f"{entry_paths[0]}:1-3"],
                "reuse_score": 1.0,
                "synthesis": {
                    "adaptation_notes": [],
                    "ui_path_score": 1.0,
                    "noise_penalty": 0.0,
                    "capability_path_score": cap_path,
                },
            },
        )

    store_asset(
        "lower/repo",
        ["components/misc/page.tsx"],
        ["next", "react", "tailwindcss"],
        0.8,
    )
    better_asset_id = store_asset(
        "better/repo",
        ["components/data-table/data-table.tsx"],
        ["next", "react", "tailwindcss", "@tanstack/react-table"],
        1.0,
    )

    results = catalog.search_assets(
        "Find a reusable data table for a Next.js Tailwind dashboard",
        max_repos=2,
    )

    assert results[0].candidate_id == better_asset_id
    assert results[0].score == 1.0
    assert results[1].score < results[0].score


def test_search_assets_prefers_command_combobox_over_generic_ui_noise(tmp_path: Path) -> None:
    def store_asset(
        repo_id: str,
        entry_paths: list[str],
        dependencies: list[str],
        cap_path: float,
        reuse_score: float,
    ) -> str:
        owner, name = repo_id.split("/", 1)
        root = tmp_path / owner / name
        root.mkdir(parents=True)
        stored_repo_id = catalog.upsert_repository(
            _repo_metadata(owner, name),
            "test",
        )
        snapshot_id = catalog.upsert_snapshot(stored_repo_id, f"{owner}sha", "main", root)
        catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
        return catalog.upsert_asset(
            snapshot_id,
            stored_repo_id,
            "command-palette",
            {
                "entry_paths": entry_paths,
                "dependency_paths": ["package.json"],
                "external_dependencies": dependencies,
                "evidence_paths": [f"{entry_paths[0]}:1-3"],
                "reuse_score": reuse_score,
                "synthesis": {
                    "adaptation_notes": [],
                    "ui_path_score": 1.0,
                    "noise_penalty": 0.0,
                    "capability_path_score": cap_path,
                },
            },
        )

    store_asset(
        "generic/repo",
        ["frontend/src/components/Sidebar.tsx"],
        ["react", "react-dom"],
        0.0,
        1.0,
    )
    combobox_asset_id = store_asset(
        "combobox/repo",
        ["components/ui/combobox.tsx"],
        ["@base-ui/react", "react", "react-dom"],
        1.0,
        0.9,
    )

    results = catalog.search_assets(
        "Find reusable command palette, combobox, or quick-search navigation UI",
        max_repos=2,
    )

    assert results[0].candidate_id == combobox_asset_id
    assert results[0].repo_id == "combobox/repo"


def test_backend_path_detection_includes_action_filenames() -> None:
    assert catalog._has_backend_path(["src/app/(auth)/register/actions.ts"]) is True


def test_search_assets_prefers_real_background_job_paths_over_service_worker_noise(
    tmp_path: Path,
) -> None:
    def store_asset(repo_id: str, entry_paths: list[str], reuse_score: float) -> str:
        owner, name = repo_id.split("/", 1)
        root = tmp_path / owner / name
        root.mkdir(parents=True)
        stored_repo_id = catalog.upsert_repository(
            _repo_metadata(owner, name),
            "test",
        )
        snapshot_id = catalog.upsert_snapshot(stored_repo_id, f"{owner}sha", "main", root)
        catalog.upsert_repository_card(snapshot_id, {"card_version": "repo-card-v1"})
        return catalog.upsert_asset(
            snapshot_id,
            stored_repo_id,
            "background-jobs",
            {
                "entry_paths": entry_paths,
                "dependency_paths": ["package.json"],
                "external_dependencies": ["next", "react"],
                "evidence_paths": [f"{path}:1-3" for path in entry_paths],
                "reuse_score": reuse_score,
                "synthesis": {
                    "adaptation_notes": [],
                    "ui_path_score": 1.0,
                    "noise_penalty": 0.0,
                    "capability_path_score": 1.0,
                },
            },
        )

    store_asset(
        "pwa/repo",
        [
            "src/lib/register-service-worker.ts",
            "src/app/admin/feeding/components/FeedSchedule.tsx",
        ],
        1.0,
    )
    worker_asset_id = store_asset(
        "worker/repo",
        ["worker/src/lib/sequence-processor.ts", "worker/src/index.ts"],
        0.8,
    )

    results = catalog.search_assets(
        "Find background job, worker, sync, or scheduled processing code",
        max_repos=2,
    )

    assert results[0].candidate_id == worker_asset_id
    assert results[0].repo_id == "worker/repo"


async def _tool_names(module) -> set[str]:
    tools = await module.mcp.list_tools()
    return {tool.name for tool in tools}


@pytest.mark.asyncio
async def test_default_mcp_tool_surface_is_exact() -> None:
    import source_scout.server as server

    names = await _tool_names(server)
    assert names == set(server.DEFAULT_MCP_TOOL_NAMES)
