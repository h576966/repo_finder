import base64
import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from source_scout import implementation_references, snapshotter
from source_scout.github_client import API_VERSION, GitHubClient, GitHubResponseError
from source_scout.models import RateLimitError


@pytest.mark.asyncio
async def test_auth_version_and_sha_transport_contract(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
    calls = []
    raw = b"def bounded_jitter_retry():\n    return 1\n"
    blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    commit, tree = "a" * 40, "b" * 40

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer fixture-token"
        assert request.headers["x-github-api-version"] == API_VERSION
        path = request.url.path
        if path.endswith("/git/commits/" + commit):
            body = {"sha": commit, "tree": {"sha": tree}}
        elif path.endswith("/git/trees/" + tree):
            assert parse_qs(request.url.query.decode()) == {"recursive": ["1"]}
            body = {
                "sha": tree,
                "truncated": False,
                "tree": [{"path": "src/retry.py", "sha": blob, "type": "blob", "mode": "100644"}],
            }
        elif path.endswith("/git/blobs/" + blob):
            body = {
                "sha": blob,
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            }
        else:
            pytest.fail(str(request.url))
        return httpx.Response(200, json=body)

    client = GitHubClient(transport=httpx.MockTransport(handler))
    try:
        result = await implementation_references.inspect_github_source(
            "https://github.com/owner/repo", commit, paths=["src/retry.py"], github_client=client
        )
    finally:
        await client.close()
    assert len(calls) == 3 and result["persisted"] is False
    assert result["status"] == "inspected"
    assert result["results"][0]["kind"] == "verified_implementation_source"
    assert result["results"][0]["content_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert "fixture-token" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 302])
async def test_http_errors_are_bounded_and_do_not_leak_bodies(status):
    client = GitHubClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, text="secret-provider-body", headers={"Location": "https://untrusted.invalid/token"}
            )
        )
    )
    try:
        with pytest.raises((RateLimitError, httpx.HTTPStatusError)) as exc:
            await client.get_repo_metadata("owner", "repo")
        assert "secret-provider-body" not in str(exc.value)
        assert "untrusted.invalid" not in str(exc.value)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"items": ["invalid"]}'])
async def test_malformed_search_response_is_not_an_abstention(body):
    client = GitHubClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    try:
        result = await implementation_references.search_github_fallback("jitter retry", github_client=client)
    finally:
        await client.close()
    assert result["status"] == "network_error" and result["error_type"] == "invalid_response"


@pytest.mark.asyncio
async def test_http_read_budget_stops_stream():
    observed = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(200):
                observed.append(1)
                yield b"x" * 16_384

    client = GitHubClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks())))
    try:
        with pytest.raises(GitHubResponseError, match="read budget"):
            await client.get_repo_metadata("owner", "repo")
    finally:
        await client.close()
    assert len(observed) <= 63


@pytest.mark.asyncio
async def test_timeout_is_reported_without_retry():
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive transport text", request=request)

    client = GitHubClient(transport=httpx.MockTransport(fail))
    try:
        result = await implementation_references.search_github_fallback("jitter retry", github_client=client)
    finally:
        await client.close()
    assert len(calls) == 1 and result["error_type"] == "network"
    assert "sensitive" not in json.dumps(result)


@pytest.mark.asyncio
async def test_blob_hash_mismatch_is_rejected():
    client = GitHubClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "sha": "a" * 40,
                    "size": 5,
                    "encoding": "base64",
                    "content": base64.b64encode(b"wrong").decode(),
                },
            )
        )
    )
    try:
        with pytest.raises(GitHubResponseError, match="object SHA"):
            await client.get_blob("owner", "repo", "a" * 40)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_partial_inspection_preserves_valid_sources():
    from tests.test_implementation_references import _FallbackClient

    class Partial(_FallbackClient):
        async def get_readme(self, owner, repo, ref=None):
            if repo == "broken":
                raise httpx.ReadTimeout("offline")
            return "lead"

    client = Partial(matches=[{"full_name": "o/good"}, {"full_name": "o/broken"}])
    result = await implementation_references.search_github_fallback("jitter retry", github_client=client)
    assert result["status"] == "partial"
    assert [item["repo_id"] for item in result["results"]] == ["o/good"]
    assert result["results"][0]["implementation_verified"] is False
    assert result["errors"]


def test_git_fetch_failure_has_separate_auth_contract_and_no_token(monkeypatch, isolated_catalog):
    import subprocess

    monkeypatch.setenv("GITHUB_TOKEN", "private-token-fixture")
    actual_run = subprocess.run
    seen = []

    def run(argv, **kwargs):
        if "fetch" in argv:
            seen.append((argv, kwargs))
            assert "private-token-fixture" not in str(argv)
            raise subprocess.CalledProcessError(128, argv, stderr=b"private-token-fixture")
        return actual_run(argv, **kwargs)

    monkeypatch.setattr(snapshotter.subprocess, "run", run)
    with pytest.raises(snapshotter.SnapshotError, match="REST-only") as exc:
        snapshotter.clone_snapshot("https://github.com/private/repo", "private", "repo", "a" * 40, "main")
    assert len(seen) == 1 and "private-token-fixture" not in str(exc.value)
    assert not list(isolated_catalog.rglob("config"))
