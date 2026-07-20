"""Tests for kimi_code_bot.event.parse_event."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kimi_code_bot.config import Config
from kimi_code_bot.event import parse_event
from kimi_code_bot.types import MentionEvent, ReviewTarget

REPO = "octo/hello"


def write_event(tmp_path: Path, payload: dict) -> Config:
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(payload), encoding="utf-8")
    return Config(repo=REPO, event_path=event_file)


# --------------------------------------------------------------- pull_request


def test_pull_request_opened(tmp_path):
    cfg = write_event(
        tmp_path, {"action": "opened", "pull_request": {"number": 42}}
    )
    target = parse_event(cfg, env={})
    assert target == ReviewTarget(repo=REPO, pr_number=42)


@pytest.mark.parametrize("action", ["reopened", "synchronize"])
def test_pull_request_review_actions(tmp_path, action):
    cfg = write_event(tmp_path, {"action": action, "pull_request": {"number": 7}})
    target = parse_event(cfg, env={})
    assert isinstance(target, ReviewTarget)
    assert target.pr_number == 7


@pytest.mark.parametrize("action", ["closed", "edited", "labeled"])
def test_pull_request_ignored_actions(tmp_path, action):
    cfg = write_event(tmp_path, {"action": action, "pull_request": {"number": 7}})
    assert parse_event(cfg, env={}) is None


def test_repo_from_env_when_cfg_repo_empty(tmp_path):
    cfg = write_event(tmp_path, {"action": "opened", "pull_request": {"number": 1}})
    cfg.repo = ""
    target = parse_event(cfg, env={"GITHUB_REPOSITORY": "env/repo"})
    assert target == ReviewTarget(repo="env/repo", pr_number=1)


# --------------------------------------------------------------- issue_comment


def test_issue_comment_mention_on_pr(tmp_path):
    cfg = write_event(
        tmp_path,
        {
            "action": "created",
            "issue": {"number": 12, "pull_request": {"url": "..."}},
            "comment": {
                "id": 900,
                "body": "please @kimi-code-bot review this",
                "author_association": "MEMBER",
                "user": {"login": "alice"},
            },
        },
    )
    ev = parse_event(cfg, env={})
    assert isinstance(ev, MentionEvent)
    assert ev.repo == REPO
    assert ev.pr_number == 12
    assert ev.comment_id == 900
    assert ev.comment_body == "review this"  # command part after the mention
    assert ev.comment_author == "alice"
    assert ev.author_association == "MEMBER"
    assert ev.path is None
    assert ev.in_reply_to_id is None


def test_issue_comment_on_pure_issue_ignored(tmp_path):
    cfg = write_event(
        tmp_path,
        {
            "action": "created",
            "issue": {"number": 12},  # no pull_request key => pure issue
            "comment": {
                "id": 900,
                "body": "@kimi-code-bot review",
                "author_association": "MEMBER",
                "user": {"login": "alice"},
            },
        },
    )
    assert parse_event(cfg, env={}) is None


def test_issue_comment_without_mention_ignored(tmp_path):
    cfg = write_event(
        tmp_path,
        {
            "action": "created",
            "issue": {"number": 12, "pull_request": {"url": "..."}},
            "comment": {
                "id": 900,
                "body": "lgtm",
                "author_association": "MEMBER",
                "user": {"login": "alice"},
            },
        },
    )
    assert parse_event(cfg, env={}) is None


# -------------------------------------------------- pull_request_review_comment


def test_review_comment_mention(tmp_path):
    cfg = write_event(
        tmp_path,
        {
            "action": "created",
            "pull_request": {"number": 33},
            "comment": {
                "id": 901,
                "body": "@kimi-code-bot explain",
                "author_association": "OWNER",
                "user": {"login": "bob"},
                "path": "src/main.py",
                "line": 88,
                "in_reply_to_id": 555,
            },
        },
    )
    ev = parse_event(cfg, env={})
    assert isinstance(ev, MentionEvent)
    assert ev.pr_number == 33
    assert ev.comment_id == 901
    assert ev.comment_body == "explain"
    assert ev.path == "src/main.py"
    assert ev.line == 88
    assert ev.in_reply_to_id == 555


def test_review_comment_without_mention_ignored(tmp_path):
    cfg = write_event(
        tmp_path,
        {
            "action": "created",
            "pull_request": {"number": 33},
            "comment": {
                "id": 901,
                "body": "nice catch",
                "author_association": "OWNER",
                "user": {"login": "bob"},
            },
        },
    )
    assert parse_event(cfg, env={}) is None


# ---------------------------------------------------------------------- misc


def test_no_event_path_returns_none():
    assert parse_event(Config(repo=REPO), env={}) is None


def test_unrelated_event_returns_none(tmp_path):
    cfg = write_event(tmp_path, {"action": "created", "release": {"tag_name": "v1"}})
    assert parse_event(cfg, env={}) is None


def test_invalid_json_raises(tmp_path):
    event_file = tmp_path / "event.json"
    event_file.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="failed to parse event payload"):
        parse_event(Config(repo=REPO, event_path=event_file), env={})
