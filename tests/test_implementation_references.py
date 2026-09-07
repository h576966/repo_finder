import hashlib
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import git
import httpx
import pytest
from fastmcp import Client

from source_scout import catalog, implementation_references, server, snapshotter

pytestmark = pytest.mark.usefixtures("isolated_catalog")


def test_git_blob_reader_closes_underlying_stream() -> None:
    class TrackingStream:
        closed = False

        def read(self, limit: int) -> bytes:
            return b"payload"[:limit]

        def close(self) -> None:
            self.closed = True

    stream = TrackingStream()
    object_stream = SimpleNamespace(read=stream.read, stream=stream)
    blob = SimpleNamespace(data_stream=object_stream)

    assert snapshotter._read_blob_bytes(blob, 4) == b"payl"
    assert stream.closed is True


@pytest.mark.asyncio
async def test_historical_reference_id_and_hash_keep_original_meaning(tmp_path):
    root, commit = _repository(tmp_path)
    await implementation_references.add_reference_source(root)
    match = implementation_references.find_implementation_references("bounded jitter retry").results[0]
    row = implementation_references._reference_row(match.reference_id)
    path = Path(row["snapshot_path"]) / match.path
    canonical = path.read_bytes()
    historical_bytes = canonical.replace(b"\n", b"\r\n")
    historical_hash = hashlib.sha256(historical_bytes).hexdigest()
    path.write_bytes(historical_bytes)
    with catalog.get_connection() as conn:
        conn.execute(
            "UPDATE snapshots SET analyzer_version = 'reuse-reference-v1' WHERE snapshot_id = ?",
            [match.snapshot_id],
        )
        conn.execute(
            "UPDATE implementation_references SET content_sha256 = ? WHERE reference_id = ?",
            [historical_hash, match.reference_id],
        )
    context = implementation_references.get_implementation_reference(match.reference_id)
    assert context.reference_id == match.reference_id and context.commit_sha == commit
    assert context.historical_content_sha256 == "sha256:" + historical_hash
    assert context.content_sha256 == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert implementation_references._reference_row(match.reference_id)["content_sha256"] == historical_hash
    assert (
        implementation_references.find_implementation_references("bounded jitter retry").status == "abstained"
    )
    path.write_bytes(b"changed source")
    with pytest.raises(ValueError, match="changed"):
        implementation_references.get_implementation_reference(match.reference_id)


def _repository(
    tmp_path: Path, name: str = "reference", files: dict[str, str] | None = None
) -> tuple[Path, str]:
    root = tmp_path / name
    if files is None:
        shutil.copytree(Path(__file__).parent / "fixtures/reference_repo", root)
    else:
        for rel_path, content in files.items():
            path = root / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    repo = git.Repo.init(root)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Source Scout Test")
        writer.set_value("user", "email", "source-scout@example.invalid")
    repo.git.add(".")
    commit = repo.index.commit("fixture").hexsha
    repo.close()
    return root, str(commit)


@pytest.mark.asyncio
async def test_local_add_find_context_is_commit_pinned_and_model_free(monkeypatch, tmp_path):
    root, commit = _repository(tmp_path)
    original = (root / "src/retry_budget.py").read_text()
    (root / "src/retry_budget.py").write_text("uncommitted_secret = True\n")

    def forbidden(*args, **kwargs):
        pytest.fail("local reference flow must not access GitHub or model configuration")

    monkeypatch.setattr(implementation_references, "get_client", forbidden)
    added = await implementation_references.add_reference_source(root, selection_kind="personal")
    assert added["commit_sha"] == commit
    assert added["source_kind"] == "local" and added["executed_source_code"] is False
    assert added["reference_count"] == 1

    first = implementation_references.find_implementation_references(
        "bounded decorrelated jitter retry schedule"
    )
    second = implementation_references.find_implementation_references(
        "bounded decorrelated jitter retry schedule"
    )
    first_payload, second_payload = asdict(first), asdict(second)
    assert first_payload.pop("usage") != second_payload.pop("usage")
    assert first_payload == second_payload
    journal = json.loads(Path(first.usage["report_path"]).read_text(encoding="utf-8"))
    assert journal["result"]["results"][0]["reference_id"] == first.results[0].reference_id
    assert first.status == "matches" and len(first.results) == 1
    candidate = first.results[0]
    assert candidate.path == "src/retry_budget.py"
    assert candidate.permalink is None and candidate.origin == str(root.resolve())

    context = implementation_references.get_implementation_reference(
        candidate.reference_id, task="decorrelated jitter retry schedule"
    )
    assert context.commit_sha == commit and context.snapshot_id == added["snapshot_id"]
    assert context.content_sha256 == "sha256:" + hashlib.sha256(original.encode()).hexdigest()
    assert "decorrelated_jitter_delay" in context.snippets[0].content
    assert "uncommitted_secret" not in context.snippets[0].content
    assert context.snippets[0].start_line >= 1
    assert context.snippets[0].end_line >= context.snippets[0].start_line
    assert all(snippet.permalink is None for snippet in context.snippets)
    assert context.license["status"] == "files_present_spdx_unknown"
    recorded_context = json.loads(Path(context.usage["report_path"]).read_text(encoding="utf-8"))
    assert recorded_context["result"]["snippets"][0]["content_sha256"] == context.snippets[0].content_sha256
    assert "content" not in recorded_context["result"]["snippets"][0]
    assert "manifests" not in recorded_context["result"]


@pytest.mark.asyncio
async def test_readding_same_commit_keeps_stable_identity(tmp_path):
    root, _commit = _repository(tmp_path)
    first_add = await implementation_references.add_reference_source(root, selection_kind="curated")
    first = implementation_references.find_implementation_references(
        "bounded decorrelated jitter retry schedule"
    )
    second_add = await implementation_references.add_reference_source(root, selection_kind="curated")
    second = implementation_references.find_implementation_references(
        "bounded decorrelated jitter retry schedule"
    )
    assert first_add["snapshot_id"] == second_add["snapshot_id"]
    assert first.results[0].reference_id == second.results[0].reference_id
    assert first.results[0].content_sha256 == second.results[0].content_sha256


@pytest.mark.asyncio
async def test_weak_single_candidate_abstains_without_relative_score_approval(tmp_path):
    root, _commit = _repository(tmp_path, files={"src/utils.py": "def oauth_helper():\n    return None\n"})
    await implementation_references.add_reference_source(root)
    result = implementation_references.find_implementation_references("oauth refresh token rotation")
    assert result.status == "abstained" and result.results == []
    assert "absolute relevance gate" in str(result.abstention_reason)


@pytest.mark.asyncio
async def test_metadata_cannot_supply_implementation_evidence(tmp_path):
    root, _ = _repository(
        tmp_path,
        name="oauth-refresh-token-rotation",
        files={
            "package.json": json.dumps(
                {
                    "name": "oauth-refresh-token-rotation",
                    "dependencies": {"oauth-refresh-token-rotation": "1.0.0"},
                }
            ),
            "src/arithmetic.py": "def add(left, right):\n    return left + right\n",
        },
    )
    await implementation_references.add_reference_source(root)
    assert (
        implementation_references.find_implementation_references("oauth refresh token rotation").status
        == "abstained"
    )
    real, _ = _repository(
        tmp_path,
        name="real",
        files={
            "src/tokens.py": (
                "def rotate_oauth_refresh_token(old_token):\n    return refresh_token(old_token)\n"
            ),
        },
    )
    await implementation_references.add_reference_source(real, selection_kind="curated")
    result = implementation_references.find_implementation_references("oauth refresh token rotation")
    assert result.status == "matches"
    assert [item.path for item in result.results] == ["src/tokens.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".cjs", ".cts"])
async def test_commonjs_and_cts_are_source_evidence(tmp_path, suffix):
    root, _ = _repository(
        tmp_path,
        files={f"src/retry{suffix}": "export function boundedJitterRetry() { return jitterDelay(); }\n"},
    )
    added = await implementation_references.add_reference_source(root)
    assert added["reference_count"] == 1
    assert (
        implementation_references.find_implementation_references("bounded jitter retry").status == "matches"
    )


@pytest.mark.asyncio
async def test_windows_cmd_is_source_evidence(tmp_path):
    root, _ = _repository(
        tmp_path,
        files={
            "source-scout.cmd": (
                "@echo off\n"
                "set PROJECT_PYTHON=%~dp0.venv\\Scripts\\python.exe\n"
                "if not exist \"%PROJECT_PYTHON%\" echo virtual environment missing\n"
                "\"%PROJECT_PYTHON%\" -m source_scout %*\n"
            ),
        },
    )
    added = await implementation_references.add_reference_source(root)
    assert added["reference_count"] == 1
    result = implementation_references.find_implementation_references(
        "Windows command wrapper repository virtual environment missing Python"
    )
    assert result.status == "matches"
    assert result.results[0].path == "source-scout.cmd"


@pytest.mark.asyncio
async def test_attributes_do_not_change_git_blob_hashes_or_execute_filters(tmp_path, monkeypatch):
    root, commit = _repository(
        tmp_path,
        files={
            ".gitattributes": "*.py text eol=crlf filter=forbidden\n",
            "src/retry.py": "def bounded_jitter_retry():\n    return 1\n",
        },
    )
    marker = tmp_path / "filter-ran"
    # A global inherited filter would run during checkout if we used one.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.forbidden.smudge")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"echo forbidden > {marker.as_posix()}")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "filter.forbidden.required")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "true")
    with git.Repo(root) as repo:
        canonical = (repo.commit(commit).tree / "src/retry.py").data_stream.read()
    await implementation_references.add_reference_source(root)
    match = implementation_references.find_implementation_references("bounded jitter retry").results[0]
    context = implementation_references.get_implementation_reference(match.reference_id)
    assert context.content_sha256 == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert not marker.exists()
    row = implementation_references._reference_row(match.reference_id)
    assert (Path(row["snapshot_path"]) / "src/retry.py").read_bytes() == canonical


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["x" * 180_000, "界" * 60_000], ids=["ascii", "unicode"])
async def test_large_single_line_manifest_and_source_have_bounded_honest_context(tmp_path, payload):
    root, _ = _repository(
        tmp_path,
        files={
            "package.json": json.dumps({"name": payload}, ensure_ascii=False),
            "src/retry.py": 'bounded_jitter_retry = "' + payload + '"\n',
        },
    )
    await implementation_references.add_reference_source(root)
    match = implementation_references.find_implementation_references("bounded jitter retry").results[0]
    context = implementation_references.get_implementation_reference(match.reference_id)
    output = implementation_references.reference_to_jsonable(context)
    assert len(json.dumps(output).encode()) <= 64_000
    assert output["truncated"]
    assert output["snippets"] == []
    assert output["manifests"][0]["truncated"]
    assert output["manifests"][0]["start_line"] is None
    assert output["missing_evidence"]


@pytest.mark.asyncio
async def test_unrecognized_language_does_not_claim_general_target_compatibility(tmp_path):
    root, _ = _repository(tmp_path, files={"src/retry.rs": "fn bounded_jitter_retry() -> u32 { 1 }\n"})
    target = tmp_path / "target"
    target.mkdir()
    (target / "main.go").write_text("package main\n")
    await implementation_references.add_reference_source(root)
    result = implementation_references.find_implementation_references(
        "bounded jitter retry", target_project_path=target
    )
    assert result.results[0].target_fit["status"] == "unknown"


@pytest.mark.asyncio
async def test_target_fit_reports_conflict_and_unknown_without_blocking_match(tmp_path):
    root, _commit = _repository(tmp_path)
    await implementation_references.add_reference_source(root)
    target = tmp_path / "target"
    target.mkdir()
    (target / "package.json").write_text(json.dumps({"dependencies": {"react": "19.0.0"}}), encoding="utf-8")
    (target / "src.ts").write_text("export const value = 1\n", encoding="utf-8")
    match = implementation_references.find_implementation_references(
        "bounded decorrelated jitter retry schedule", target_project_path=target
    )
    assert match.status == "matches"
    assert match.results[0].target_fit["status"] == "conflict"
    assert "different known language ecosystems" in match.results[0].target_fit["conflicts"][0]

    unknown_target = tmp_path / "empty-target"
    unknown_target.mkdir()
    unknown = implementation_references.find_implementation_references(
        "bounded decorrelated jitter retry schedule", target_project_path=unknown_target
    )
    assert unknown.results[0].target_fit["status"] == "unknown"
    assert unknown.results[0].target_fit["unknown"]


@pytest.mark.asyncio
async def test_selected_archived_private_fork_template_is_not_discovery_filtered(monkeypatch, tmp_path):
    root, commit = _repository(tmp_path)

    class SelectedGitHub:
        async def get_repo_metadata(self, owner, repo):
            return {
                "owner": {"login": owner},
                "name": repo,
                "full_name": f"{owner}/{repo}",
                "html_url": f"https://github.com/{owner}/{repo}",
                "default_branch": "main",
                "private": True,
                "archived": True,
                "fork": True,
                "is_template": True,
                "license": {"spdx_id": "MIT"},
                "description": "Explicit selected retry patterns",
            }

        async def get_default_branch_commit(self, owner, repo, branch):
            return commit

    original_clone = snapshotter.clone_snapshot

    def local_clone(**kwargs):
        forwarded = {key: value for key, value in kwargs.items() if key != "repo_url"}
        return original_clone(repo_url=str(root), **forwarded)

    monkeypatch.setattr(implementation_references.snapshotter, "clone_snapshot", local_clone)
    added = await implementation_references.add_reference_source(
        "https://github.com/selected/retry-reference",
        selection_kind="curated",
        github_client=SelectedGitHub(),
    )
    found = implementation_references.find_implementation_references(
        "bounded decorrelated jitter retry schedule"
    )
    assert added["source_kind"] == "github" and found.status == "matches"
    facts = found.results[0].repository_facts
    assert facts["private"] and facts["archived"] and facts["fork"] and facts["is_template"]
    context = implementation_references.get_implementation_reference(
        found.results[0].reference_id, task="jitter retry"
    )
    assert context.snippets[0].permalink == (
        f"https://github.com/selected/retry-reference/blob/{commit}/src/retry_budget.py"
        f"#L{context.snippets[0].start_line}-L{context.snippets[0].end_line}"
    )
    assert context.license["spdx"] is None
    assert context.license["current_repository_observation"]["spdx"] == "MIT"
    assert context.license["pinned_commit"]["files"] == ["LICENSE"]


@pytest.mark.asyncio
async def test_changed_snapshot_and_symlink_escape_are_rejected(tmp_path):
    root, _commit = _repository(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("def secret_outside(): pass\n")
    try:
        (root / "src/external.py").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    repo = git.Repo(root)
    repo.git.add("src/external.py")
    repo.index.commit("symlink")
    repo.close()
    await implementation_references.add_reference_source(root)
    found = implementation_references.find_implementation_references(
        "bounded decorrelated jitter retry schedule"
    )
    assert all(result.path != "src/external.py" for result in found.results)
    row = implementation_references._reference_row(found.results[0].reference_id)
    assert row is not None
    snapshot_file = Path(row["snapshot_path"]) / row["path"]
    snapshot_file.write_text("changed bytes\n")
    with pytest.raises(implementation_references.ImplementationReferenceError, match="changed files"):
        implementation_references.get_implementation_reference(found.results[0].reference_id)


@pytest.mark.asyncio
async def test_additive_schema_preserves_legacy_catalog_rows(tmp_path):
    legacy_repo = catalog.upsert_repository(
        {
            "owner": {"login": "legacy"},
            "name": "catalog",
            "full_name": "legacy/catalog",
            "html_url": "https://github.com/legacy/catalog",
            "private": False,
            "archived": False,
        },
        "legacy",
    )
    root, _commit = _repository(tmp_path)
    await implementation_references.add_reference_source(root)
    assert catalog.get_repository(legacy_repo) is not None
    with catalog.get_connection() as conn:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert {"assets", "reuse_assessments", "reference_sources", "implementation_references"} <= tables


class _FallbackClient:
    def __init__(self, *, matches=None, error=None):
        self.matches = matches if matches is not None else []
        self.error = error
        self.calls = []

    async def search_repos(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        if self.error:
            raise self.error
        return self.matches

    async def get_default_branch_commit(self, owner, repo, branch):
        self.calls.append(("commit", owner, repo, branch))
        return "a" * 40

    async def get_repo_contents(self, owner, repo, path="", ref=None):
        self.calls.append(("contents", owner, repo, ref))
        assert ref == "a" * 40
        return [{"path": "src/pattern.py"}, {"path": "README.md"}]

    async def get_readme(self, owner, repo, ref=None):
        self.calls.append(("readme", owner, repo, ref))
        assert ref == "a" * 40
        return "Pinned implementation inspection"


@pytest.mark.asyncio
async def test_explicit_github_fallback_is_bounded_pinned_and_not_persisted():
    matches = [
        {
            "full_name": f"owner/repo-{index}",
            "html_url": f"https://github.com/owner/repo-{index}",
            "default_branch": "main",
            "description": "retry pattern",
            "language": "Python",
            "license": None,
        }
        for index in range(4)
    ]
    client = _FallbackClient(matches=matches)
    result = await implementation_references.search_github_fallback(
        "decorrelated jitter retry", max_results=2, github_client=client
    )
    assert result["status"] == "leads" and len(result["results"]) == 2
    assert all(item["commit_sha"] == "a" * 40 and item["permanent"] is False for item in result["results"])
    assert all("--commit " + "a" * 40 in item["add_command"] for item in result["results"])
    with catalog.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reference_sources").fetchone()[0] == 0
    search_call = client.calls[0]
    assert search_call[2]["per_page"] == 2 and search_call[2]["page"] == 1


@pytest.mark.asyncio
async def test_github_fallback_distinguishes_no_matches_and_network_error():
    no_matches = await implementation_references.search_github_fallback(
        "rare implementation pattern", github_client=_FallbackClient()
    )
    request = httpx.Request("GET", "https://api.github.com/search/repositories")
    failure = await implementation_references.search_github_fallback(
        "rare implementation pattern",
        github_client=_FallbackClient(error=httpx.ConnectError("offline", request=request)),
    )
    assert no_matches["status"] == "no_matches"
    assert failure["status"] == "network_error" and failure["error_type"] == "network"


@pytest.mark.asyncio
async def test_reference_mcp_profile_has_only_find_and_context():
    assert server.REFERENCE_MCP_TOOL_NAMES == (
        "find_implementation_references",
        "get_implementation_reference",
    )
    async with Client(server.create_server("references")) as client:
        assert {tool.name for tool in await client.list_tools()} == set(server.REFERENCE_MCP_TOOL_NAMES)
    with pytest.raises(ValueError, match="retired"):
        server.create_server("reuse")


@pytest.mark.asyncio
async def test_reference_find_cli_returns_json_without_model_access(monkeypatch, capsys, tmp_path):
    root, _commit = _repository(tmp_path)
    await implementation_references.add_reference_source(root)
    monkeypatch.setattr(
        sys,
        "argv",
        ["source-scout", "references", "find", "--task", "bounded decorrelated jitter retry schedule"],
    )
    from source_scout.__main__ import main

    main()
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "matches" and len(output["results"]) == 1
