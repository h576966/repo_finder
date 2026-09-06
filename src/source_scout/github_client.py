"""Explicit bounded GitHub REST reads. Git fetch has separate authentication."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from .models import RateLimitError

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"  # Supported; intentionally retained, see docs/development-notes.md.
TIMEOUT = 30.0
MAX_HTTP_BYTES = 1_000_000
MAX_BLOB_BYTES = 240_000


class GitHubResponseError(ValueError):
    pass


class GitHubClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "source-scout",
        }
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if not url.startswith(API_BASE + "/"):
            raise GitHubResponseError("GitHub reads must remain on api.github.com.")
        async with self._client.stream("GET", url, params=params) as response:
            if response.status_code in {403, 429} or response.headers.get("x-ratelimit-remaining") == "0":
                if response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0":
                    raw_reset = response.headers.get("x-ratelimit-reset", "")
                    retry = max(int(raw_reset) - int(time.time()), 1) if raw_reset.isdigit() else None
                    raise RateLimitError("GitHub API rate limit exceeded.", retry_after=retry)
            # Never include response bodies, credentials or redirect URLs in errors.
            if response.status_code >= 300:
                raise httpx.HTTPStatusError(
                    f"GitHub HTTP {response.status_code}", request=response.request, response=response
                )
            raw = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16_384):
                if len(raw) + len(chunk) > MAX_HTTP_BYTES:
                    raise GitHubResponseError("GitHub response exceeded the read budget.")
                raw.extend(chunk)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise GitHubResponseError("Malformed GitHub JSON response.") from exc

    @staticmethod
    def _repo(owner: str, repo: str) -> str:
        if not all(
            re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in {".", ".."} for part in (owner, repo)
        ):
            raise GitHubResponseError("Invalid GitHub repository identity.")
        return f"{API_BASE}/repos/{owner}/{repo}"

    async def search_repos(
        self,
        query: str,
        per_page: int = 3,
        sort: str = "stars",
        order: str = "desc",
        page: int = 1,
    ) -> list[dict[str, Any]]:
        if not 1 <= per_page <= 3 or page != 1 or len(query) > 200:
            raise GitHubResponseError("Repository leads are limited to one page of at most three results.")
        data = await self._get(
            f"{API_BASE}/search/repositories",
            params={
                "q": query,
                "sort": sort,
                "order": order,
                "per_page": per_page,
                "page": page,
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise GitHubResponseError("Malformed GitHub search response.")
        if not all(isinstance(item, dict) for item in data["items"]):
            raise GitHubResponseError("Malformed GitHub repository lead.")
        return list(data["items"][:per_page])

    async def get_repo_metadata(self, owner: str, repo: str) -> dict[str, Any]:
        data = await self._get(self._repo(owner, repo))
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            raise GitHubResponseError("Malformed GitHub repository metadata.")
        return data

    async def get_default_branch_commit(self, owner: str, repo: str, branch: str) -> str:
        data = await self._get(f"{self._repo(owner, repo)}/commits/{quote(branch, safe='')}")
        if not isinstance(data, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(data.get("sha", ""))):
            raise GitHubResponseError("Could not resolve a full GitHub commit SHA.")
        return str(data["sha"])

    async def get_repo_contents(
        self,
        owner: str,
        repo: str,
        path: str = "",
        ref: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        data = await self._get(
            f"{self._repo(owner, repo)}/contents/{quote(path, safe='/')}",
            params={"ref": ref} if ref else None,
        )
        if not isinstance(data, (list, dict)):
            raise GitHubResponseError("Malformed GitHub contents response.")
        return data

    async def get_readme(self, owner: str, repo: str, ref: str | None = None) -> str | None:
        try:
            data = await self._get(f"{self._repo(owner, repo)}/readme", params={"ref": ref} if ref else None)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not isinstance(data, dict):
            raise GitHubResponseError("Malformed README response.")
        return self._decode_blob(data).decode("utf-8", errors="replace")[:1200]

    async def get_commit_tree(self, owner: str, repo: str, commit: str) -> dict[str, Any]:
        data = await self._get(f"{self._repo(owner, repo)}/git/commits/{commit}")
        if not isinstance(data, dict) or data.get("sha") != commit or not isinstance(data.get("tree"), dict):
            raise GitHubResponseError("GitHub commit identity mismatch.")
        tree_sha = str(data["tree"].get("sha", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", tree_sha):
            raise GitHubResponseError("Invalid GitHub tree SHA.")
        tree = await self._get(f"{self._repo(owner, repo)}/git/trees/{tree_sha}", params={"recursive": "1"})
        if (
            not isinstance(tree, dict)
            or tree.get("sha") != tree_sha
            or not isinstance(tree.get("tree"), list)
        ):
            raise GitHubResponseError("GitHub tree identity mismatch.")
        return tree

    async def get_blob(self, owner: str, repo: str, sha: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubResponseError("Invalid GitHub blob SHA.")
        data = await self._get(f"{self._repo(owner, repo)}/git/blobs/{sha}")
        if not isinstance(data, dict) or data.get("sha") != sha:
            raise GitHubResponseError("GitHub blob identity mismatch.")
        raw = self._decode_blob(data)
        actual = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        if actual != sha:
            raise GitHubResponseError("GitHub blob bytes do not match their Git object SHA.")
        return raw

    @staticmethod
    def _decode_blob(data: dict[str, Any]) -> bytes:
        content = data.get("content")
        size = data.get("size")
        if data.get("encoding") != "base64" or not isinstance(content, str):
            raise GitHubResponseError("GitHub did not return bounded base64 file content.")
        if isinstance(size, int) and size > MAX_BLOB_BYTES or len(content) > MAX_BLOB_BYTES * 2:
            raise GitHubResponseError("GitHub file exceeds the read budget.")
        try:
            raw = base64.b64decode("".join(content.split()), validate=True)
        except ValueError as exc:
            raise GitHubResponseError("Malformed GitHub base64 content.") from exc
        if len(raw) > MAX_BLOB_BYTES:
            raise GitHubResponseError("GitHub file exceeds the read budget.")
        return raw


def get_client() -> GitHubClient:
    """Caller owns close(); no cross-event-loop persistent HTTP clients."""
    return GitHubClient()
