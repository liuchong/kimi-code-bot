"""GitHub API client (REST + GraphQL), ported from hoverstare src/github.rs.

Retry policy: 429 / 5xx / connection errors are retried with exponential
backoff (base * 4**attempt, ``Retry-After`` respected) up to MAX_RETRIES
times; other statuses are returned directly. GraphQL errors come back as
HTTP 200 + an ``errors`` field and are checked explicitly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .types import InlineComment, PRInfo

log = logging.getLogger(__name__)

DEFAULT_API = "https://api.github.com"
API_VERSION = "2022-11-28"
MAX_RETRIES = 4
PER_PAGE = 100

# permission levels for is_collaborator(permission_hint=...)
_PERM_ORDER = {"read": 1, "triage": 2, "write": 3, "maintain": 4, "admin": 5}


class GitHubError(Exception):
    """GitHub API error (non-2xx after retries, GraphQL errors, transport)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_url: str = DEFAULT_API,
        pat: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_base_delay: float = 0.5,
    ) -> None:
        self.api = api_url.rstrip("/")
        self._pat = pat
        self._retry_base_delay = retry_base_delay
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "kimi-code-bot/0.1.0",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ core

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = self._retry_base_delay * 4**attempt
        else:
            delay = self._retry_base_delay * 4**attempt
        await asyncio.sleep(delay)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        accept: str | None = None,
        json: Any = None,
        use_pat: bool = False,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if accept is not None:
            # per-request header overrides the client-level default
            headers["Accept"] = accept
        if use_pat:
            if not self._pat:
                raise GitHubError("operation requires a PAT (gh_pat not configured)")
            headers["Authorization"] = f"Bearer {self._pat}"

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await self._client.request(method, url, headers=headers, json=json)
            except httpx.TransportError as e:
                if attempt >= MAX_RETRIES:
                    raise GitHubError(f"connection error: {e}") from e
                log.warning(
                    "GitHub API connection error, retrying (%d/%d): %s",
                    attempt + 1,
                    MAX_RETRIES,
                    e,
                )
                await self._backoff(attempt)
                continue
            status = resp.status_code
            if status == 429 or 500 <= status < 600:
                if attempt >= MAX_RETRIES:
                    raise GitHubError(f"api error {status}: {resp.text}", status=status)
                log.warning(
                    "GitHub API %d, retrying (%d/%d)", status, attempt + 1, MAX_RETRIES
                )
                await self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            return resp
        raise GitHubError("unreachable: retry loop exited")  # pragma: no cover

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if not 200 <= resp.status_code < 300:
            raise GitHubError(
                f"api error {resp.status_code}: {resp.text}", status=resp.status_code
            )

    async def _get_paginated(self, url: str) -> list[dict]:
        """GET all pages of a list endpoint (per_page=100)."""
        out: list[dict] = []
        page = 1
        while True:
            sep = "&" if "?" in url else "?"
            resp = await self._send("GET", f"{url}{sep}per_page={PER_PAGE}&page={page}")
            self._raise_for_status(resp)
            batch = resp.json()
            out.extend(batch)
            if len(batch) < PER_PAGE:
                return out
            page += 1

    # ------------------------------------------------------------------ REST

    async def get_pr(self, repo: str, number: int) -> PRInfo:
        resp = await self._send("GET", f"{self.api}/repos/{repo}/pulls/{number}")
        self._raise_for_status(resp)
        d = resp.json()
        head_repo = d.get("head", {}).get("repo") or {}
        return PRInfo(
            repo=repo,
            number=d["number"],
            title=d.get("title", ""),
            author=(d.get("user") or {}).get("login", ""),
            author_association=d.get("author_association", ""),
            draft=d.get("draft", False),
            head_sha=d["head"]["sha"],
            base_sha=d["base"]["sha"],
            head_repo_full_name=head_repo.get("full_name"),
            html_url=d.get("html_url", ""),
        )

    async def get_pr_diff(self, repo: str, number: int) -> str:
        resp = await self._send(
            "GET",
            f"{self.api}/repos/{repo}/pulls/{number}",
            accept="application/vnd.github.v3.diff",
        )
        self._raise_for_status(resp)
        return resp.text

    async def get_compare_diff(self, repo: str, base_sha: str, head_sha: str) -> str:
        resp = await self._send(
            "GET",
            f"{self.api}/repos/{repo}/compare/{base_sha}...{head_sha}",
            accept="application/vnd.github.v3.diff",
        )
        self._raise_for_status(resp)
        return resp.text

    async def list_pr_files(self, repo: str, number: int) -> list[dict]:
        """All PR files (paginated); fallback for >300-file PRs."""
        return await self._get_paginated(f"{self.api}/repos/{repo}/pulls/{number}/files")

    async def list_reviews(self, repo: str, pr: int) -> list[dict]:
        resp = await self._send(
            "GET", f"{self.api}/repos/{repo}/pulls/{pr}/reviews?per_page={PER_PAGE}"
        )
        self._raise_for_status(resp)
        return resp.json()

    async def list_review_comments(self, repo: str, pr: int) -> list[dict]:
        return await self._get_paginated(f"{self.api}/repos/{repo}/pulls/{pr}/comments")

    async def create_review(
        self,
        repo: str,
        pr: int,
        commit_id: str,
        body: str,
        comments: list[InlineComment],
        event: str = "COMMENT",
    ) -> None:
        payload = {
            "commit_id": commit_id,
            "body": body,
            "event": event,
            "comments": [
                {"path": c.path, "line": c.line, "side": c.side, "body": c.body}
                for c in comments
            ],
        }
        resp = await self._send(
            "POST", f"{self.api}/repos/{repo}/pulls/{pr}/reviews", json=payload
        )
        self._raise_for_status(resp)

    async def create_issue_comment(self, repo: str, issue: int, body: str) -> int:
        resp = await self._send(
            "POST",
            f"{self.api}/repos/{repo}/issues/{issue}/comments",
            json={"body": body},
        )
        self._raise_for_status(resp)
        return int(resp.json().get("id", 0))

    async def add_reaction(self, repo: str, comment_id: int, content: str) -> None:
        # issue comments and review-thread comments use different endpoints;
        # try the issue endpoint first, fall back to the pulls endpoint on 404
        resp = await self._send(
            "POST",
            f"{self.api}/repos/{repo}/issues/comments/{comment_id}/reactions",
            json={"content": content},
        )
        if resp.status_code == 404:
            resp = await self._send(
                "POST",
                f"{self.api}/repos/{repo}/pulls/comments/{comment_id}/reactions",
                json={"content": content},
            )
        self._raise_for_status(resp)

    async def create_reply(self, repo: str, pr: int, comment_id: int, body: str) -> None:
        resp = await self._send(
            "POST",
            f"{self.api}/repos/{repo}/pulls/{pr}/comments/{comment_id}/replies",
            json={"body": body},
        )
        self._raise_for_status(resp)

    async def get_pull_comment(self, repo: str, comment_id: int) -> dict:
        resp = await self._send("GET", f"{self.api}/repos/{repo}/pulls/comments/{comment_id}")
        self._raise_for_status(resp)
        return resp.json()

    async def is_collaborator(
        self, repo: str, username: str, permission_hint: str = ""
    ) -> bool:
        resp = await self._send(
            "GET", f"{self.api}/repos/{repo}/collaborators/{username}"
        )
        if resp.status_code == 404:
            return False
        self._raise_for_status(resp)
        if not permission_hint:
            return True
        perm_resp = await self._send(
            "GET", f"{self.api}/repos/{repo}/collaborators/{username}/permission"
        )
        self._raise_for_status(perm_resp)
        actual = perm_resp.json().get("permission", "")
        return _PERM_ORDER.get(actual, 0) >= _PERM_ORDER.get(permission_hint, 0)

    async def create_status(
        self,
        repo: str,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str = "",
    ) -> None:
        payload: dict[str, str] = {
            "state": state,
            "context": context,
            "description": description,
        }
        if target_url:
            payload["target_url"] = target_url
        resp = await self._send(
            "POST", f"{self.api}/repos/{repo}/statuses/{sha}", json=payload
        )
        self._raise_for_status(resp)

    async def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
        """Raw file content at ``ref`` (base-branch instruction files); 404 -> None."""
        resp = await self._send(
            "GET",
            f"{self.api}/repos/{repo}/contents/{path}?ref={ref}",
            accept="application/vnd.github.raw",
        )
        if resp.status_code == 404:
            return None
        self._raise_for_status(resp)
        return resp.text

    # --------------------------------------------------------------- GraphQL

    async def graphql(
        self, query: str, variables: dict, use_pat: bool = False
    ) -> dict:
        resp = await self._send(
            "POST",
            f"{self.api}/graphql",
            json={"query": query, "variables": variables},
            use_pat=use_pat,
        )
        self._raise_for_status(resp)
        body = resp.json()
        # GraphQL errors come back as HTTP 200 + an errors field
        if body.get("errors"):
            raise GitHubError(f"graphql errors: {body['errors']}")
        return body

    async def fetch_review_threads(self, repo: str, pr: int) -> list[dict]:
        """All review threads: id, isResolved, comments(body, path, line, databaseId)."""
        query = """query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        nodes {
          id
          isResolved
          comments(first: 1) { nodes { databaseId body path line } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""
        owner, name = repo.split("/", 1)
        out: list[dict] = []
        cursor: str | None = None
        while True:
            data = await self.graphql(
                query,
                {"owner": owner, "repo": name, "pr": pr, "cursor": cursor},
            )
            threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            out.extend(threads.get("nodes") or [])
            page_info = threads.get("pageInfo") or {}
            if page_info.get("hasNextPage"):
                cursor = page_info.get("endCursor")
            else:
                return out

    async def resolve_review_thread(self, thread_id: str) -> bool:
        """Resolve a review thread via PAT; False if no PAT or on any failure."""
        if not self._pat:
            return False
        mutation = """mutation($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) {
    thread { isResolved }
  }
}"""
        try:
            data = await self.graphql(mutation, {"threadId": thread_id}, use_pat=True)
            thread = (data.get("data") or {}).get("resolveReviewThread", {}).get("thread") or {}
            return bool(thread.get("isResolved"))
        except GitHubError as e:
            log.warning("resolve_review_thread(%s) failed: %s", thread_id, e)
            return False

    async def add_thread_reply_graphql(self, thread_id: str, body: str) -> bool:
        """Reply inside a review thread (fallback for resolve: '✅ confirmed fixed')."""
        mutation = """mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
    input: { pullRequestReviewThreadId: $threadId, body: $body }
  ) {
    comment { id }
  }
}"""
        try:
            await self.graphql(mutation, {"threadId": thread_id, "body": body})
            return True
        except GitHubError as e:
            log.warning("add_thread_reply_graphql(%s) failed: %s", thread_id, e)
            return False
