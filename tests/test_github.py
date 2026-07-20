"""Tests for kimibot.github (httpx.MockTransport, no real requests)."""

from __future__ import annotations

import json

import httpx
import pytest

from kimibot.github import GitHubClient, GitHubError
from kimibot.types import InlineComment

REPO = "octo/hello"
API = "https://api.github.com"


def make_client(
    handler, pat: str | None = None
) -> GitHubClient:
    return GitHubClient(
        "test-token",
        pat=pat,
        transport=httpx.MockTransport(handler),
        retry_base_delay=0,
    )


def json_response(data, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


# --------------------------------------------------------------------- PR info


@pytest.mark.asyncio
async def test_get_pr():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{REPO}/pulls/42"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        return json_response(
            {
                "number": 42,
                "title": "Add feature",
                "user": {"login": "alice"},
                "author_association": "MEMBER",
                "draft": False,
                "head": {"sha": "headsha", "repo": {"full_name": "octo/hello"}},
                "base": {"sha": "basesha"},
                "html_url": "https://github.com/octo/hello/pull/42",
            }
        )

    async with make_client(handler) as client:
        pr = await client.get_pr(REPO, 42)
    assert pr.number == 42
    assert pr.author == "alice"
    assert pr.author_association == "MEMBER"
    assert pr.head_sha == "headsha"
    assert pr.base_sha == "basesha"
    assert pr.head_repo_full_name == "octo/hello"
    assert pr.repo == REPO


@pytest.mark.asyncio
async def test_get_pr_diff_accept_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "application/vnd.github.v3.diff"
        return httpx.Response(200, text="diff --git a/x b/x\n")

    async with make_client(handler) as client:
        diff = await client.get_pr_diff(REPO, 1)
    assert diff.startswith("diff --git")


@pytest.mark.asyncio
async def test_get_compare_diff():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{REPO}/compare/aaa...bbb"
        assert request.headers["Accept"] == "application/vnd.github.v3.diff"
        return httpx.Response(200, text="diff text")

    async with make_client(handler) as client:
        assert await client.get_compare_diff(REPO, "aaa", "bbb") == "diff text"


# ------------------------------------------------------------------ pagination


@pytest.mark.asyncio
async def test_list_pr_files_paginates():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        calls.append(page)
        if page == "1":
            return json_response([{"filename": f"f{i}.py", "patch": "@@"} for i in range(100)])
        return json_response([{"filename": "last.py", "patch": "@@"}])

    async with make_client(handler) as client:
        files = await client.list_pr_files(REPO, 7)
    assert calls == ["1", "2"]
    assert len(files) == 101
    assert files[-1]["filename"] == "last.py"


@pytest.mark.asyncio
async def test_list_reviews_and_review_comments():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reviews"):
            return json_response([{"id": 1, "body": "review body", "commit_id": "sha"}])
        return json_response([{"id": 9, "body": "c", "path": "a.py", "line": 3}])

    async with make_client(handler) as client:
        reviews = await client.list_reviews(REPO, 7)
        comments = await client.list_review_comments(REPO, 7)
    assert reviews[0]["id"] == 1
    assert comments[0]["path"] == "a.py"


# ------------------------------------------------------------------ mutations


@pytest.mark.asyncio
async def test_create_review_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return json_response({"id": 555}, status=201)

    comments = [InlineComment(path="a.py", line=10, body="issue here")]
    async with make_client(handler) as client:
        await client.create_review(REPO, 3, "commitsha", "summary", comments)
    assert captured["commit_id"] == "commitsha"
    assert captured["body"] == "summary"
    assert captured["event"] == "COMMENT"
    assert captured["comments"] == [
        {"path": "a.py", "line": 10, "side": "RIGHT", "body": "issue here"}
    ]


@pytest.mark.asyncio
async def test_create_issue_comment_returns_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{REPO}/issues/5/comments"
        return json_response({"id": 777}, status=201)

    async with make_client(handler) as client:
        assert await client.create_issue_comment(REPO, 5, "hello") == 777


@pytest.mark.asyncio
async def test_add_reaction_falls_back_to_pulls_endpoint():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "/issues/comments/" in request.url.path:
            return json_response({"message": "Not Found"}, status=404)
        return json_response({"id": 1}, status=201)

    async with make_client(handler) as client:
        await client.add_reaction(REPO, 123, "eyes")
    assert paths == [
        f"/repos/{REPO}/issues/comments/123/reactions",
        f"/repos/{REPO}/pulls/comments/123/reactions",
    ]


@pytest.mark.asyncio
async def test_create_reply():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{REPO}/pulls/9/comments/44/replies"
        assert json.loads(request.content) == {"body": "ack"}
        return json_response({"id": 1}, status=201)

    async with make_client(handler) as client:
        await client.create_reply(REPO, 9, 44, "ack")


@pytest.mark.asyncio
async def test_is_collaborator():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alice"):
            return httpx.Response(204)
        return httpx.Response(404)

    async with make_client(handler) as client:
        assert await client.is_collaborator(REPO, "alice") is True
        assert await client.is_collaborator(REPO, "mallory") is False


@pytest.mark.asyncio
async def test_is_collaborator_permission_hint():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/permission"):
            return json_response({"permission": "read"})
        return httpx.Response(204)

    async with make_client(handler) as client:
        assert await client.is_collaborator(REPO, "alice", "write") is False
        assert await client.is_collaborator(REPO, "alice", "read") is True


@pytest.mark.asyncio
async def test_create_status():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{REPO}/statuses/sha123"
        captured.update(json.loads(request.content))
        return json_response({}, status=201)

    async with make_client(handler) as client:
        await client.create_status(REPO, "sha123", "kimi-bot/review", "success", "ok")
    assert captured == {
        "state": "success",
        "context": "kimi-bot/review",
        "description": "ok",
    }


@pytest.mark.asyncio
async def test_get_file_content_and_404():
    def handler(request: httpx.Request) -> httpx.Response:
        if "AGENTS.md" in request.url.path:
            assert request.url.params["ref"] == "main"
            assert request.headers["Accept"] == "application/vnd.github.raw"
            return httpx.Response(200, text="# instructions")
        return json_response({"message": "Not Found"}, status=404)

    async with make_client(handler) as client:
        assert await client.get_file_content(REPO, "AGENTS.md", "main") == "# instructions"
        assert await client.get_file_content(REPO, "MISSING.md", "main") is None


# -------------------------------------------------------------------- GraphQL


@pytest.mark.asyncio
async def test_graphql_errors_field_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        return json_response({"errors": [{"message": "boom"}]})

    async with make_client(handler) as client:
        with pytest.raises(GitHubError, match="graphql errors"):
            await client.graphql("query { viewer { login } }", {})


@pytest.mark.asyncio
async def test_fetch_review_threads_paginates():
    pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = json.loads(request.content)["variables"]["cursor"]
        pages.append(cursor)
        if cursor is None:
            return json_response(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "T1",
                                            "isResolved": False,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "databaseId": 100,
                                                        "body": "issue",
                                                        "path": "a.py",
                                                        "line": 5,
                                                    }
                                                ]
                                            },
                                        }
                                    ],
                                    "pageInfo": {"hasNextPage": True, "endCursor": "CUR"},
                                }
                            }
                        }
                    }
                }
            )
        return json_response(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {"id": "T2", "isResolved": True, "comments": {"nodes": []}}
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            }
        )

    async with make_client(handler) as client:
        threads = await client.fetch_review_threads(REPO, 2)
    assert pages == [None, "CUR"]
    assert [t["id"] for t in threads] == ["T1", "T2"]
    assert threads[0]["comments"]["nodes"][0]["databaseId"] == 100


@pytest.mark.asyncio
async def test_resolve_review_thread_requires_pat():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made without a PAT")

    async with make_client(handler) as client:
        assert await client.resolve_review_thread("T1") is False


@pytest.mark.asyncio
async def test_resolve_review_thread_with_pat():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer pat-token"
        return json_response(
            {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
        )

    async with make_client(handler, pat="pat-token") as client:
        assert await client.resolve_review_thread("T1") is True


@pytest.mark.asyncio
async def test_resolve_review_thread_failure_returns_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"errors": [{"message": "FORBIDDEN"}]})

    async with make_client(handler, pat="pat-token") as client:
        assert await client.resolve_review_thread("T1") is False


@pytest.mark.asyncio
async def test_add_thread_reply_graphql():
    def handler(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        assert variables == {"threadId": "T1", "body": "✅ confirmed fixed"}
        return json_response({"data": {"addPullRequestReviewThreadReply": {"comment": {"id": 1}}}})

    async with make_client(handler) as client:
        assert await client.add_thread_reply_graphql("T1", "✅ confirmed fixed") is True


@pytest.mark.asyncio
async def test_add_thread_reply_graphql_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"errors": [{"message": "nope"}]})

    async with make_client(handler) as client:
        assert await client.add_thread_reply_graphql("T1", "x") is False


# ---------------------------------------------------------------------- retry


@pytest.mark.asyncio
async def test_retry_on_429_then_success():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json_response({"message": "rate limited"}, status=429,
                                 headers={"Retry-After": "0"})
        return json_response([{"id": 1}])

    async with make_client(handler) as client:
        reviews = await client.list_reviews(REPO, 1)
    assert calls == 2
    assert reviews == [{"id": 1}]


@pytest.mark.asyncio
async def test_retry_exhausted_raises():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response({"message": "server error"}, status=500)

    async with make_client(handler) as client:
        with pytest.raises(GitHubError, match="api error 500"):
            await client.list_reviews(REPO, 1)
    assert calls == 5  # initial attempt + 4 retries


@pytest.mark.asyncio
async def test_non_retryable_error_raises_immediately():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response({"message": "Not Found"}, status=404)

    async with make_client(handler) as client:
        with pytest.raises(GitHubError, match="api error 404"):
            await client.list_reviews(REPO, 1)
    assert calls == 1
