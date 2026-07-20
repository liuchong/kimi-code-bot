"""Shared types for kimi-code-bot. All modules import from here — keep this file dependency-free."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal, Protocol

Severity = Literal["critical", "high", "medium", "low"]
SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}

LensName = Literal["correctness", "concurrency", "security"]
LENSES: list[tuple[LensName, float]] = [
    ("correctness", 0.2),
    ("concurrency", 0.4),
    ("security", 0.6),
]


class BotCommand(enum.Enum):
    REVIEW = "review"
    MENTION = "mention"


# ---------------------------------------------------------------- diff model


@dataclass
class DiffHunk:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    # raw text lines of the hunk body (each starts with ' ', '+', '-' or '\')
    lines: list[str] = field(default_factory=list)


@dataclass
class DiffFile:
    path: str
    old_path: str | None = None
    status: Literal["added", "modified", "deleted", "renamed"] = "modified"
    hunks: list[DiffHunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def commentable_lines(self) -> set[int]:
        """New-side line numbers that GitHub accepts as inline-comment anchors
        (context lines and added lines, on the RIGHT side)."""
        out: set[int] = set()
        for h in self.hunks:
            new_lineno = h.new_start
            for raw in h.lines:
                if raw.startswith("-"):
                    continue
                if raw.startswith("\\"):
                    continue
                out.add(new_lineno)
                new_lineno += 1
        return out

    @property
    def added_count(self) -> int:
        return sum(
            1 for h in self.hunks for raw in h.lines if raw.startswith("+") and not raw.startswith("+++")
        )


# ---------------------------------------------------------------- findings


@dataclass
class Finding:
    path: str
    line: int
    severity: Severity
    title: str
    description: str
    end_line: int | None = None
    suggestion: str | None = None
    confidence: float = 1.0
    votes: int = 1
    lenses: list[str] = field(default_factory=list)
    fingerprint: str = ""
    # line used after anchoring (may be snapped); None => could not anchor inline
    anchored_line: int | None = None


# ---------------------------------------------------------------- agent layer


@dataclass
class Budget:
    max_steps: int = 22  # max_tool_calls + 2 headroom
    timeout_secs: int = 900


@dataclass
class ReviewRequest:
    system_prompt: str
    user_prompt: str
    work_dir: str
    budget: Budget
    model: str | None = None  # kimi-cli model alias; None => kimi-cli default
    lens: str = "review"
    with_tools: bool = True  # False => reformat pass (no tools)


@dataclass
class ToolCallRecord:
    name: str
    arguments_summary: str
    ok: bool = True


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class ReviewRun:
    raw_output: str
    tool_trace: list[ToolCallRecord] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    steps: int = 0


class AgentError(Exception):
    """Recoverable analysis-zone error (network, rate limit, model misbehavior)."""


class ReviewBackend(Protocol):
    async def run(self, req: ReviewRequest) -> ReviewRun: ...


# ---------------------------------------------------------------- github model


@dataclass
class PRInfo:
    repo: str  # "owner/name"
    number: int
    title: str
    author: str
    author_association: str
    draft: bool
    head_sha: str
    base_sha: str
    head_repo_full_name: str | None
    html_url: str = ""


@dataclass
class InlineComment:
    path: str
    line: int
    body: str
    side: str = "RIGHT"


@dataclass
class ExistingFinding:
    """A finding marker recovered from a previously posted review comment."""

    fingerprint: str
    path: str
    line: int
    body: str
    comment_id: int
    thread_id: str | None = None  # GraphQL node id, for resolve
    thread_resolved: bool = False


@dataclass
class ReviewMeta:
    """Machine-readable state embedded in the review body meta comment."""

    head_sha: str
    mode: str = "full"  # "full" | "incremental"
    files_reviewed: int = 0
    fingerprints: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- events


@dataclass
class ReviewTarget:
    repo: str
    pr_number: int
    force_full: bool = False


@dataclass
class MentionEvent:
    repo: str
    pr_number: int
    comment_id: int
    comment_body: str
    comment_author: str
    author_association: str
    # set when the mention is on an inline review comment
    path: str | None = None
    line: int | None = None
    in_reply_to_id: int | None = None
