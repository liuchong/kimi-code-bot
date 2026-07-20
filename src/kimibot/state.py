"""Cross-commit state (ported from hoverstare src/state.rs).

All state lives on the GitHub side (hidden markers in comments + a metadata
comment in review bodies); kimi-bot itself persists nothing and is naturally
stateless.
"""

from __future__ import annotations

import hashlib
import re

from .types import ExistingFinding, ReviewMeta

# Hidden marker inside inline comments: "<!-- kimi-bot-finding:{fp} -->"
FINDING_MARK = "<!-- kimi-bot-finding:{fp} -->"
_MARKER_RE = re.compile(r"<!--\s*kimi-bot-finding:([0-9a-fA-F]+)\s*-->")

# Metadata comment in review bodies:
# <!-- kimi-bot-meta mode=... head_sha=... files_reviewed=... finding: fp1 fp2 ... -->
META_MARK = "<!-- kimi-bot-meta"
META_MARK_RE = re.compile(
    r"<!--\s*kimi-bot-meta"
    r"\s+mode=(?P<mode>\S+)"
    r"\s+head_sha=(?P<head_sha>\S+)"
    r"\s+files_reviewed=(?P<files_reviewed>\d+)"
    r"(?:\s+finding:(?P<fingerprints>[^>]*?))?"
    r"\s*-->"
)


def _normalize(s: str) -> str:
    """Trim, collapse consecutive whitespace, ignore case."""
    return " ".join(s.split()).lower()


def fingerprint(path: str, line_content: str, title: str) -> str:
    """Stable identity for "which problem in which code of which file".

    Uses the line content rather than the line number — line drift (new lines
    inserted above) does not affect fingerprint stability. Returns the first
    16 hex chars of sha1(path + normalized line content + normalized title).
    """
    h = hashlib.sha1()
    h.update(path.encode())
    h.update(b"\n")
    h.update(_normalize(line_content).encode())
    h.update(b"\n")
    h.update(_normalize(title).encode())
    return h.hexdigest()[:16]


def render_finding_mark(fp: str) -> str:
    """Render one fingerprint marker for embedding in an inline comment."""
    return FINDING_MARK.format(fp=fp)


def extract_fingerprints(body: str) -> list[str]:
    """Extract all fingerprint markers from a comment body (a merged comment
    may contain several)."""
    return _MARKER_RE.findall(body or "")


def parse_finding_marks(comments: list[dict]) -> list[ExistingFinding]:
    """Recover finding markers from previously posted review comments (REST
    API dicts with id/path/line/body keys). One ExistingFinding per marker."""
    out: list[ExistingFinding] = []
    for c in comments:
        body = c.get("body") or ""
        line = c.get("line") or c.get("original_line") or 0
        for fp in extract_fingerprints(body):
            out.append(
                ExistingFinding(
                    fingerprint=fp,
                    path=c.get("path") or "",
                    line=line,
                    body=body,
                    comment_id=c.get("id") or 0,
                    thread_id=c.get("thread_id"),
                    thread_resolved=bool(c.get("thread_resolved", False)),
                )
            )
    return out


def render_meta(meta: ReviewMeta) -> str:
    """Render the machine-readable metadata comment for a review body."""
    fps = " ".join(meta.fingerprints)
    return (
        f"<!-- kimi-bot-meta mode={meta.mode} head_sha={meta.head_sha}"
        f" files_reviewed={meta.files_reviewed} finding: {fps} -->"
    )


def parse_meta(body: str) -> ReviewMeta | None:
    """Parse the metadata comment back out of a review body."""
    m = META_MARK_RE.search(body or "")
    if not m:
        return None
    fps = (m.group("fingerprints") or "").split()
    return ReviewMeta(
        head_sha=m.group("head_sha"),
        mode=m.group("mode"),
        files_reviewed=int(m.group("files_reviewed")),
        fingerprints=fps,
    )


def unresolved_fingerprints(
    threads: list[dict], marks: list[ExistingFinding]
) -> set[str]:
    """Fingerprints of historical findings that are still open.

    Combines GraphQL reviewThread nodes ({"id", "isResolved", "comments":
    {"nodes": [{"body": ...}]}}) with marks recovered from comments:
    - every fingerprint found in an unresolved thread's comments is open;
    - a mark whose thread_id belongs to an unresolved thread is open;
    - a mark without thread info is conservatively treated as open unless it
      is explicitly flagged thread_resolved.
    """
    out: set[str] = set()
    unresolved_thread_ids: set[str] = set()
    for t in threads:
        if t.get("isResolved"):
            continue
        if tid := t.get("id"):
            unresolved_thread_ids.add(tid)
        for c in (t.get("comments") or {}).get("nodes") or []:
            out.update(extract_fingerprints(c.get("body") or ""))
    for mark in marks:
        if mark.thread_id is not None:
            if mark.thread_id in unresolved_thread_ids:
                out.add(mark.fingerprint)
        elif not mark.thread_resolved:
            out.add(mark.fingerprint)
    return out
