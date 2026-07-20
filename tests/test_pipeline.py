"""Pipeline tests with a scripted fake ReviewBackend (no network, no kimi-cli)."""

from __future__ import annotations

import json

import pytest

from kimibot import pipeline
from kimibot.config import Config
from kimibot.types import AgentError, DiffFile, DiffHunk, ReviewRun


class FakeBackend:
    """Maps (lens, attempt) -> canned behavior via a handler."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, bool]] = []  # (lens, with_tools)

    async def run(self, req) -> ReviewRun:
        self.calls.append((req.lens, req.with_tools))
        out = self.handler(req)
        if isinstance(out, Exception):
            raise out
        return ReviewRun(raw_output=out)


def _finding_json(path="a.py", line=10, title="Null deref in parser", severity="high"):
    return json.dumps(
        {
            "findings": [
                {
                    "path": path,
                    "line": line,
                    "severity": severity,
                    "title": title,
                    "description": "desc",
                }
            ],
            "cross_cutting": [],
        }
    )


def _files(added=100) -> DiffFile:
    return DiffFile(
        path="a.py",
        status="modified",
        hunks=[DiffHunk(old_start=1, old_lines=1, new_start=1, new_lines=added,
                        lines=["+x"] * added)],
    )


def _cfg(**kw) -> Config:
    return Config(workspace=__import__("pathlib").Path("."), **kw)


@pytest.mark.asyncio
async def test_two_votes_confirmed_without_verifier():
    backend = FakeBackend(lambda req: _finding_json())
    result = await pipeline.analyze(
        backend, cfg=_cfg(), pr_title="t", files=[_files()], diff_text="d", work_dir="."
    )
    assert len(result.confirmed) == 1
    assert result.confirmed[0].votes >= 2
    assert not any(lens == "verifier" for lens, _ in backend.calls)


@pytest.mark.asyncio
async def test_single_vote_goes_to_verifier_and_is_confirmed():
    def handler(req):
        if req.lens == "verifier":
            return '{"confirmed": true, "confidence": 0.9, "reason": "real bug"}'
        # only the correctness lens reports the finding
        return _finding_json() if req.lens == "correctness" else '{"findings": [], "cross_cutting": []}'

    backend = FakeBackend(handler)
    result = await pipeline.analyze(
        backend, cfg=_cfg(), pr_title="t", files=[_files()], diff_text="d", work_dir="."
    )
    assert len(result.confirmed) == 1
    assert result.confirmed[0].votes == 1


@pytest.mark.asyncio
async def test_single_vote_rejected_by_verifier():
    def handler(req):
        if req.lens == "verifier":
            return '{"confirmed": false, "confidence": 0.95, "reason": "not a bug"}'
        return _finding_json() if req.lens == "correctness" else '{"findings": [], "cross_cutting": []}'

    backend = FakeBackend(handler)
    result = await pipeline.analyze(
        backend, cfg=_cfg(), pr_title="t", files=[_files()], diff_text="d", work_dir="."
    )
    assert result.confirmed == []


@pytest.mark.asyncio
async def test_reformat_ladder_recovers_prose():
    def handler(req):
        if req.lens.endswith("-reformat"):
            assert req.with_tools is False
            return _finding_json()
        return "I think there is a bug somewhere in a.py around line 10."  # prose

    backend = FakeBackend(handler)
    result = await pipeline.analyze(
        backend, cfg=_cfg(passes=1), pr_title="t", files=[_files()], diff_text="d", work_dir="."
    )
    assert len(result.confirmed) >= 1
    assert any(lens.endswith("-reformat") for lens, _ in backend.calls)


@pytest.mark.asyncio
async def test_empty_output_skips_reformat_and_retries():
    attempts: dict[str, int] = {}

    def handler(req):
        if req.lens.endswith("-reformat"):
            raise AssertionError("reformat must be skipped for empty output")
        attempts[req.lens] = attempts.get(req.lens, 0) + 1
        return "" if attempts[req.lens] == 1 else _finding_json()

    backend = FakeBackend(handler)
    result = await pipeline.analyze(
        backend, cfg=_cfg(passes=1), pr_title="t", files=[_files()], diff_text="d", work_dir="."
    )
    assert result.passes_run == 1
    assert attempts["correctness"] == 2


@pytest.mark.asyncio
async def test_all_passes_failing_raises_agent_error():
    backend = FakeBackend(lambda req: AgentError("boom"))
    with pytest.raises(AgentError):
        await pipeline.analyze(
            backend, cfg=_cfg(passes=2), pr_title="t", files=[_files()], diff_text="d", work_dir="."
        )


@pytest.mark.asyncio
async def test_small_diff_degrades_to_single_pass():
    backend = FakeBackend(lambda req: _finding_json())
    result = await pipeline.analyze(
        backend, cfg=_cfg(passes=3, verify=False), pr_title="t",
        files=[_files(added=10)], diff_text="d", work_dir=".",
    )
    lenses = {lens for lens, _ in backend.calls}
    assert lenses == {"correctness"}
    assert result.stats["small_diff"] is True
    # passes=1 + verify=false => direct passthrough
    assert len(result.confirmed) == 1


def test_cluster_merges_same_issue_across_lenses():
    from kimibot.types import Finding

    a = Finding(path="a.py", line=10, severity="high", title="空指针解引用", description="x")
    b = Finding(path="a.py", line=12, severity="high", title="空指针解引用问题", description="y")
    c = Finding(path="b.py", line=10, severity="high", title="空指针解引用", description="z")
    clusters = pipeline.cluster_findings([a, b, c])
    sizes = sorted(len(c_) for c_ in clusters)
    assert sizes == [1, 2]
