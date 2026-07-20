"""GitHub Actions event parsing (ported from hoverstare src/event.rs).

Reads the JSON payload at ``cfg.event_path`` and classifies it:
- pull_request (opened/reopened/synchronize) -> ReviewTarget
- issue_comment / pull_request_review_comment containing "@kimi-bot" -> MentionEvent
- anything else -> None

CLI explicit --repo/--pr runs construct ReviewTarget directly in cli.py and
do not go through this module.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .config import Config
from .types import MentionEvent, ReviewTarget

MENTION = "@kimi-bot"
REVIEW_ACTIONS = {"opened", "reopened", "synchronize"}


def parse_event(
    cfg: Config, env: dict | None = None
) -> ReviewTarget | MentionEvent | None:
    env = os.environ if env is None else env
    if cfg.event_path is None:
        return None
    try:
        payload = json.loads(cfg.event_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"failed to parse event payload ({cfg.event_path}): {e}") from e
    if not isinstance(payload, dict):
        return None

    repo = cfg.repo or env.get("GITHUB_REPOSITORY", "")

    comment = payload.get("comment")
    if isinstance(comment, dict):
        return _parse_mention(payload, comment, repo)

    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        if payload.get("action") not in REVIEW_ACTIONS:
            return None
        return ReviewTarget(repo=repo, pr_number=pr["number"])

    return None


def _parse_mention(
    payload: dict[str, Any], comment: dict[str, Any], repo: str
) -> MentionEvent | None:
    body = comment.get("body") or ""
    if MENTION not in body:
        return None
    # the command part is whatever follows the mention
    command = body.split(MENTION, 1)[1].strip()
    author = (comment.get("user") or {}).get("login", "")
    association = comment.get("author_association", "")

    # issue_comment: only comments on PRs are handled (pure issues ignored)
    issue = payload.get("issue")
    if isinstance(issue, dict):
        if issue.get("pull_request") is None:
            return None
        return MentionEvent(
            repo=repo,
            pr_number=issue["number"],
            comment_id=comment["id"],
            comment_body=command,
            comment_author=author,
            author_association=association,
        )

    # pull_request_review_comment: inline thread mention
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        return MentionEvent(
            repo=repo,
            pr_number=pr["number"],
            comment_id=comment["id"],
            comment_body=command,
            comment_author=author,
            author_association=association,
            path=comment.get("path"),
            line=comment.get("line"),
            in_reply_to_id=comment.get("in_reply_to_id"),
        )

    return None
