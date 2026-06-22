import asyncio
import base64
import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

from .models import RateLimitError

LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

API_BASE = "https://api.github.com"
TIMEOUT = 30.0


class GitHubClient:
    def __init__(self) -> None:
        token = os.environ.get("GITHUB_TOKEN", "")
        self._headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "source-scout-mcp",
        }
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=TIMEOUT,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.get(url, params=params)

        remaining = int(response.headers.get("x-ratelimit-remaining", "1"))
        if remaining == 0:
            reset_epoch = int(response.headers.get("x-ratelimit-reset", "0"))
            if reset_epoch:
                retry_after = max(reset_epoch - int(time.time()), 1)
            else:
                retry_after = None
            msg = (
                f"GitHub API rate limit exceeded. Retry in {retry_after}s"
                if retry_after
                else "GitHub API rate limit exceeded."
            )
            raise RateLimitError(msg, retry_after=retry_after)

        if response.status_code == 403 and "rate limit" in response.text.lower():
            raise RateLimitError("GitHub API rate limit exceeded.", retry_after=60)

        if response.status_code == 404:
            raise httpx.HTTPStatusError(
                "Not found",
                request=response.request,
                response=response,
            )

        response.raise_for_status()
        return response.json()

    async def search_repos(
        self,
        query: str,
        per_page: int = 30,
        sort: str = "stars",
        order: str = "desc",
        page: int = 1,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "q": query,
            "sort": sort,
            "order": order,
            "per_page": per_page,
            "page": page,
        }
        url = f"{API_BASE}/search/repositories"
        data = await self._get(url, params=params)
        return data.get("items", [])  # type: ignore[no-any-return]

    async def get_repo_metadata(self, owner: str, repo: str) -> dict[str, Any]:
        url = f"{API_BASE}/repos/{owner}/{repo}"
        return await self._get(url)  # type: ignore[no-any-return]

    async def get_readme(self, owner: str, repo: str) -> str | None:
        url = f"{API_BASE}/repos/{owner}/{repo}/readme"
        try:
            data: dict[str, Any] = await self._get(url)
        except httpx.HTTPStatusError:
            return None

        content = data.get("content", "")
        encoding = data.get("encoding", "base64")

        if encoding == "base64" and content:
            try:
                decoded = base64.b64decode(content).decode("utf-8", errors="replace")
                if len(decoded) > 10000:
                    decoded = decoded[:10000] + "\n\n[README truncated to 10KB]"
                return decoded
            except Exception:
                return None
        return str(content) if content else None

    async def get_commits(
        self, owner: str, repo: str, per_page: int = 20
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": per_page}
        url = f"{API_BASE}/repos/{owner}/{repo}/commits"
        return await self._get(url, params=params)  # type: ignore[no-any-return]

    async def get_default_branch_commit(
        self, owner: str, repo: str, branch: str
    ) -> str:
        enc_branch = quote(branch, safe="")
        url = f"{API_BASE}/repos/{owner}/{repo}/branches/{enc_branch}"
        data = await self._get(url)
        commit = data.get("commit", {})
        sha = commit.get("sha")
        if not sha:
            commits = await self.get_commits(owner, repo, per_page=1)
            if commits:
                sha = commits[0].get("sha")
        if not sha:
            raise ValueError(f"Could not resolve default branch commit for {owner}/{repo}")
        return str(sha)

    async def get_repo_contents(
        self, owner: str, repo: str, path: str = ""
    ) -> list[dict[str, Any]] | dict[str, Any]:
        enc_path = quote(path, safe="") if path else ""
        url = f"{API_BASE}/repos/{owner}/{repo}/contents"
        if enc_path:
            url = f"{url}/{enc_path}"
        return await self._get(url)  # type: ignore[no-any-return]

    async def get_file_content(
        self, owner: str, repo: str, path: str, max_lines: int = 30
    ) -> str | None:
        enc_path = quote(path, safe="")
        url = f"{API_BASE}/repos/{owner}/{repo}/contents/{enc_path}"
        try:
            data = await self._get(url)
        except httpx.HTTPStatusError:
            return None

        if isinstance(data, list):
            return None

        content = data.get("content", "")
        encoding = data.get("encoding", "base64")
        if encoding == "base64" and content:
            try:
                decoded = base64.b64decode(content).decode("utf-8", errors="replace")
                lines = decoded.split("\n")
                return "\n".join(lines[:max_lines])
            except Exception:
                return None
        return content if isinstance(content, str) else None


_client: GitHubClient | None = None
_loop_id: int | None = None


def get_client() -> GitHubClient:
    global _client, _loop_id
    try:
        current_loop = asyncio.get_running_loop()
        current_id = id(current_loop)
    except RuntimeError:
        current_id = None

    if _client is not None and current_id != _loop_id:
        _client = None

    if _client is None:
        _client = GitHubClient()
        _loop_id = current_id

    return _client
