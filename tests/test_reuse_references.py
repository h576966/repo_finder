import hashlib
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import git
import httpx
import pytest
from fastmcp import Client

from source_scout import catalog, reuse_references, server, snapshotter


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

    monkeypatch.setattr(reuse_references, "get_client", forbidden)
    added = await reuse_references.add_reference_source(root, selection_kind="personal")
    assert added["commit_sha"] == commit
    assert added["source_kind"] == "local" and added["executed_source_code"] is False
    assert added["reference_count"] == 1

    first = reuse_references.find_reuse_references("bounded decorrelated jitter retry schedule")
    second = reuse_references.find_reuse_references("bounded decorrelated jitter retry schedule")
    assert asdict(first) == asdict(second)
    assert first.status == "matches" and len(first.results) == 1
    candidate = first.results[0]
    assert candidate.path == "src/retry_budget.py"
    assert candidate.permalink is None and candidate.origin == str(root.resolve())

    context = reuse_references.get_reuse_context(
        candidate.candidate_id, task="decorrelated jitter retry schedule"
    )
    assert context.commit_sha == commit and context.snapshot_id == added["snapshot_id"]
    assert context.content_sha256 == "sha256:" + hashlib.sha256(original.encode()).hexdigest()
    assert "decorrelated_jitter_delay" in context.snippets[0].content
    assert "uncommitted_secret" not in context.snippets[0].content
    assert context.snippets[0].start_line >= 1
    assert context.snippets[0].end_line >= context.snippets[0].start_line
    assert all(snippet.permalink is None for snippet in context.snippets)
    assert context.license["status"] == "files_present_spdx_unknown"


@pytest.mark.asyncio
async def test_readding_same_commit_keeps_stable_identity(tmp_path):
    root, _commit = _repository(tmp_path)
    first_add = await reuse_references.add_reference_source(root, selection_kind="curated")
    first = reuse_references.find_reuse_references("bounded decorrelated jitter retry schedule")
    second_add = await reuse_references.add_reference_source(root, selection_kind="curated")
    second = reuse_references.find_reuse_references("bounded decorrelated jitter retry schedule")
    assert first_add["snapshot_id"] == second_add["snapshot_id"]
    assert first.results[0].candidate_id == second.results[0].candidate_id
    assert first.results[0].content_sha256 == second.results[0].content_sha256


@pytest.mark.asyncio
async def test_weak_single_candidate_abstains_without_relative_score_approval(tmp_path):
    root, _commit = _repository(tmp_path, files={"src/utils.py": "def oauth_helper():\n    return None\n"})
    await reuse_references.add_reference_source(root)
    result = reuse_references.find_reuse_references("oauth refresh token rotation")
    assert result.status == "abstained" and result.results == []
    assert "absolute relevance gate" in str(result.abstention_reason)


@pytest.mark.asyncio
async def test_target_fit_reports_conflict_and_unknown_without_blocking_match(tmp_path):
    root, _commit = _repository(tmp_path)
    await reuse_references.add_reference_source(root)
    target = tmp_path / "target"
    target.mkdir()
    (target / "package.json").write_text(
        json.dumps({"dependencies": {"react": "19.0.0"}}), encoding="utf-8"
    )
    (target / "src.ts").write_text("export const value = 1\n", encoding="utf-8")
    match = reuse_references.find_reuse_references(
        "bounded decorrelated jitter retry schedule", project_path=target
    )
    assert match.status == "matches"
    assert match.results[0].target_fit["status"] == "conflict"
    assert "different known language ecosystems" in match.results[0].target_fit["conflicts"][0]

    unknown_target = tmp_path / "empty-target"
    unknown_target.mkdir()
    unknown = reuse_references.find_reuse_references(
        "bounded decorrelated jitter retry schedule", project_path=unknown_target
    )
    assert unknown.results[0].target_fit["status"] == "unknown"
    assert unknown.results[0].target_fit["unknown"]


@pytest.mark.asyncio
async def test_selected_archived_private_fork_template_is_not_discovery_filtered(monkeypatch, tmp_path):
    root, commit = _repository(tmp_path)

    class SelectedGitHub:
        async def get_repo_metadata(self, owner, repo):
            return {
                "owner": {"login": owner}, "name": repo, "full_name": f"{owner}/{repo}",
                "html_url": f"https://github.com/{owner}/{repo}", "default_branch": "main",
                "private": True, "archived": True, "fork": True, "is_template": True,
                "license": {"spdx_id": "MIT"}, "description": "Explicit selected retry patterns",
            }

        async def get_default_branch_commit(self, owner, repo, branch):
            return commit

    original_clone = snapshotter.clone_snapshot

    def local_clone(**kwargs):
        forwarded = {key: value for key, value in kwargs.items() if key != "repo_url"}
        return original_clone(repo_url=str(root), **forwarded)

    monkeypatch.setattr(reuse_references.snapshotter, "clone_snapshot", local_clone)
    added = await reuse_references.add_reference_source(
        "https://github.com/selected/retry-reference",
        selection_kind="curated",
        github_client=SelectedGitHub(),
    )
    found = reuse_references.find_reuse_references("bounded decorrelated jitter retry schedule")
    assert added["source_kind"] == "github" and found.status == "matches"
    facts = found.results[0].repository_facts
    assert facts["private"] and facts["archived"] and facts["fork"] and facts["is_template"]
    context = reuse_references.get_reuse_context(found.results[0].candidate_id, task="jitter retry")
    assert context.snippets[0].permalink == (
        f"https://github.com/selected/retry-reference/blob/{commit}/src/retry_budget.py"
        f"#L{context.snippets[0].start_line}-L{context.snippets[0].end_line}"
    )
    assert context.license == {"status": "observed", "spdx": "MIT", "files": ["LICENSE"]}


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
    await reuse_references.add_reference_source(root)
    found = reuse_references.find_reuse_references("bounded decorrelated jitter retry schedule")
    assert all(result.path != "src/external.py" for result in found.results)
    row = reuse_references._reference_row(found.results[0].candidate_id)
    assert row is not None
    snapshot_file = Path(row["snapshot_path"]) / row["path"]
    snapshot_file.write_text("changed bytes\n")
    with pytest.raises(reuse_references.ReuseReferenceError, match="changed files"):
        reuse_references.get_reuse_context(found.results[0].candidate_id)


@pytest.mark.asyncio
async def test_additive_schema_preserves_legacy_catalog_rows(tmp_path):
    legacy_repo = catalog.upsert_repository(
        {
            "owner": {"login": "legacy"}, "name": "catalog", "full_name": "legacy/catalog",
            "html_url": "https://github.com/legacy/catalog", "private": False, "archived": False,
        },
        "legacy",
    )
    root, _commit = _repository(tmp_path)
    await reuse_references.add_reference_source(root)
    assert catalog.get_repository(legacy_repo) is not None
    tables = {
        row[0] for row in catalog.get_connection().execute("SHOW TABLES").fetchall()
    }
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
            "full_name": f"owner/repo-{index}", "html_url": f"https://github.com/owner/repo-{index}",
            "default_branch": "main", "description": "retry pattern", "language": "Python",
            "license": None,
        }
        for index in range(4)
    ]
    client = _FallbackClient(matches=matches)
    result = await reuse_references.search_github_fallback(
        "decorrelated jitter retry", max_results=2, github_client=client
    )
    assert result["status"] == "matches" and len(result["results"]) == 2
    assert all(item["commit_sha"] == "a" * 40 and item["permanent"] is False for item in result["results"])
    assert all("--commit " + "a" * 40 in item["add_command"] for item in result["results"])
    assert catalog.get_connection().execute("SELECT COUNT(*) FROM reference_sources").fetchone()[0] == 0
    search_call = client.calls[0]
    assert search_call[2]["per_page"] == 2 and search_call[2]["page"] == 1


@pytest.mark.asyncio
async def test_github_fallback_distinguishes_no_matches_and_network_error():
    no_matches = await reuse_references.search_github_fallback(
        "rare implementation pattern", github_client=_FallbackClient()
    )
    request = httpx.Request("GET", "https://api.github.com/search/repositories")
    failure = await reuse_references.search_github_fallback(
        "rare implementation pattern",
        github_client=_FallbackClient(error=httpx.ConnectError("offline", request=request)),
    )
    assert no_matches["status"] == "no_matches"
    assert failure["status"] == "network_error" and failure["error_type"] == "network"


@pytest.mark.asyncio
async def test_reference_mcp_profile_has_only_find_and_context():
    assert server.DEFAULT_MCP_TOOL_NAMES == ("explore_local_code",)
    assert server.REFERENCE_MCP_TOOL_NAMES == ("find_reuse_references", "get_reuse_context")
    async with Client(server.create_server("references")) as client:
        assert {tool.name for tool in await client.list_tools()} == set(server.REFERENCE_MCP_TOOL_NAMES)
    async with Client(server.create_server("reuse")) as client:
        assert {tool.name for tool in await client.list_tools()} == set(server.REUSE_MCP_TOOL_NAMES)


@pytest.mark.asyncio
async def test_reference_find_cli_returns_json_without_model_access(monkeypatch, capsys, tmp_path):
    root, _commit = _repository(tmp_path)
    await reuse_references.add_reference_source(root)
    monkeypatch.setattr(
        sys,
        "argv",
        ["source-scout", "reference-find", "--task", "bounded decorrelated jitter retry schedule"],
    )
    from source_scout.__main__ import main

    main()
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "matches" and len(output["results"]) == 1
