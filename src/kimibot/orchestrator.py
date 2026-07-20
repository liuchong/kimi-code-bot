"""Review orchestration — ported from hoverstare's orchestrator.rs.

Flow:
  resolve PR -> skip checks (draft / bot author / empty diff)
  -> incremental check (last meta head_sha -> compare API delta as review scope,
     full diff for anchoring only)
  -> fetch unresolved findings (GraphQL threads + fingerprint marks)
  -> load repo instructions from the BASE branch (head injection defense)
  -> pipeline.analyze (multi-pass + vote + verifier)
  -> fingerprint + anchor + render
  -> publish (one review POST; fallback summary issue comment; double failure => exit 1)
  -> resolve fixed threads (GraphQL; PAT-less fallback: "confirmed fixed" reply)
  -> status checks

Exit-code contract (fail-open): analysis-zone failures exit 0 (never redden CI);
config errors and publish double-failures exit 1.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from . import diff as diff_mod
from . import pipeline, report, state
from .agent import KimiBackend
from .config import Config
from .github import GitHubClient
from .types import Finding, ReviewMeta, ReviewTarget

logger = logging.getLogger(__name__)


async def run_review(
    cfg: Config,
    gh: GitHubClient,
    target: ReviewTarget,
    backend: KimiBackend | None = None,
    dry_run: bool = False,
) -> int:
    backend = backend or KimiBackend()
    repo, pr_number = target.repo, target.pr_number

    pr = await gh.get_pr(repo, pr_number)

    # --- skip checks
    if pr.draft and not cfg.review_drafts:
        logger.info("PR #%d is draft, skipping", pr_number)
        return 0
    if pr.author.endswith("[bot]"):
        logger.info("PR #%d authored by bot (%s), skipping", pr_number, pr.author)
        return 0

    full_diff_text = await gh.get_pr_diff(repo, pr_number)
    if not full_diff_text.strip():
        logger.info("PR #%d has empty diff, skipping", pr_number)
        return 0
    full_files, _ignored = diff_mod.filter_ignored(diff_mod.parse_diff(full_diff_text), cfg.ignore)
    if not full_files:
        logger.info("PR #%d: all files ignored, skipping", pr_number)
        return 0

    # --- incremental check: last review meta head_sha -> delta as scope
    review_files = full_files
    incremental_note: str | None = None
    if not target.force_full:
        last_sha = await _last_reviewed_sha(gh, repo, pr_number)
        if last_sha and last_sha != pr.head_sha:
            try:
                delta_text = await gh.get_compare_diff(repo, last_sha, pr.head_sha)
                delta_files, _ = diff_mod.filter_ignored(diff_mod.parse_diff(delta_text), cfg.ignore)
                if delta_files:
                    review_files = delta_files
                    incremental_note = (
                        f"Incremental review: scope is the delta since {last_sha[:8]} "
                        f"({len(delta_files)} files). The full diff was used for anchoring only."
                    )
            except Exception as e:  # noqa: BLE001 — incremental is best-effort
                logger.warning("incremental compare failed, falling back to full review: %s", e)

    review_files, truncated = diff_mod.truncate_files(review_files, cfg.max_diff_kb)

    # --- unresolved findings from previous reviews (prompt context + resolve later)
    marks, unresolved_fps = await _existing_state(gh, repo, pr_number, cfg)

    # --- repo instructions from BASE branch (never trust head)
    instructions = await _load_instructions(gh, cfg, repo, pr.base_sha)

    # --- analysis
    result = await pipeline.analyze(
        backend,
        cfg=cfg,
        pr_title=pr.title,
        files=review_files,
        diff_text=diff_mod.render_diff(review_files),
        work_dir=str(cfg.workspace),
        incremental_note=incremental_note,
        unresolved=marks,
        instructions=instructions,
    )

    # --- anchor on the FULL diff (delta-only lines may not be commentable in scope,
    # but anchoring uses the review-scope files; GitHub accepts lines in the PR diff)
    inline, cross_cutting, nitpicks = report.anchor_findings(
        result.confirmed, full_files, cfg.severity_threshold
    )
    _assign_fingerprints(inline + cross_cutting + nitpicks, cfg.workspace)
    # Deterministic dedupe: never re-post a finding that already has an open
    # thread (the model is told not to re-report, but don't rely on that).
    inline = _filter_already_open(inline, marks)
    cross_cutting = _filter_already_open(cross_cutting, marks)
    nitpicks = _filter_already_open(nitpicks, marks)

    meta = ReviewMeta(
        head_sha=pr.head_sha,
        mode="incremental" if incremental_note else "full",
        files_reviewed=len(review_files),
        fingerprints=[f.fingerprint for f in inline + cross_cutting],
    )
    body = report.render_body(
        inline=inline,
        cross_cutting=cross_cutting,
        nitpicks=nitpicks,
        meta=meta,
        language=cfg.language,
        truncated=truncated,
        stats=result.stats,
    )
    comments = report.build_inline_comments(inline, cfg.language)

    if dry_run:
        print(body)
        for c in comments:
            print(f"\n--- inline {c.path}:{c.line} ---\n{c.body}")
        return 0

    # --- publish (publish zone: double failure => exit 1)
    try:
        await gh.create_review(repo, pr_number, pr.head_sha, body, comments)
    except Exception as e:  # noqa: BLE001
        logger.error("create_review failed (%s), falling back to summary comment", e)
        try:
            await gh.create_issue_comment(repo, pr_number, body)
        except Exception as e2:  # noqa: BLE001
            logger.error("fallback comment also failed: %s", e2)
            return 1

    # --- resolve fixed threads (model-verified only)
    # The review contract asks the model to verify each previously reported open
    # finding against the current code and return resolved_finding_ids. Only
    # those are resolved — never "not re-reported" ones (they may simply be out
    # of the review scope). Dedupe by comment_id: same-anchor merged comments
    # carry multiple markers but share one thread.
    fixed = [
        m for m in marks
        if m.fingerprint in result.resolved_finding_ids and not m.thread_resolved
    ]
    seen_comments: set[int] = set()
    for m in fixed:
        if m.comment_id in seen_comments:
            continue
        seen_comments.add(m.comment_id)
        ok = False
        if m.thread_id:
            ok = await gh.resolve_review_thread(m.thread_id)
        if not ok:
            try:
                await gh.create_reply(repo, pr_number, m.comment_id, "✅ confirmed fixed")
            except Exception:  # noqa: BLE001
                logger.warning("failed to mark finding %s as fixed", m.fingerprint)

    # --- status checks
    if cfg.status_checks:
        from .types import SEVERITY_ORDER

        threshold = SEVERITY_ORDER.get("high", 1)
        high_open = [
            f for f in inline + cross_cutting
            if SEVERITY_ORDER.get(f.severity, 9) <= threshold
        ]
        state_name = "failure" if high_open else "success"
        desc = f"{len(high_open)} high-severity findings" if high_open else "no blocking findings"
        try:
            await gh.create_status(repo, pr.head_sha, "kimi-bot", "success", "review completed", pr.html_url)
            await gh.create_status(repo, pr.head_sha, "kimi-bot-findings", state_name, desc, pr.html_url)
        except Exception as e:  # noqa: BLE001
            logger.warning("status checks failed (non-fatal): %s", e)

    logger.info(
        "review done: %d inline, %d cross-cutting, %d nitpicks, %d resolved",
        len(inline), len(cross_cutting), len(nitpicks), len(fixed),
    )
    return 0


async def _last_reviewed_sha(gh: GitHubClient, repo: str, pr: int) -> str | None:
    try:
        reviews = await gh.list_reviews(repo, pr)
    except Exception:  # noqa: BLE001
        return None
    for r in reversed(reviews):
        meta = state.parse_meta(r.get("body") or "")
        if meta:
            return meta.head_sha
    return None


async def _existing_state(
    gh: GitHubClient, repo: str, pr: int, cfg: Config
) -> tuple[list, set[str]]:
    """(finding marks with thread info, unresolved fingerprint set)."""

    try:
        comments = await gh.list_review_comments(repo, pr)
        marks = state.parse_finding_marks(comments)
    except Exception:  # noqa: BLE001
        marks = []
    unresolved: set[str] = set()
    try:
        threads = await gh.fetch_review_threads(repo, pr)
        unresolved = state.unresolved_fingerprints(threads, marks)
        # attach thread ids for later resolving
        by_comment = {}
        for t in threads:
            for c in t.get("comments", []):
                by_comment[c.get("databaseId")] = (t.get("id"), t.get("isResolved", False))
        for m in marks:
            if m.comment_id in by_comment:
                m.thread_id, m.thread_resolved = by_comment[m.comment_id]
    except Exception:  # noqa: BLE001
        unresolved = {m.fingerprint for m in marks}
    return marks, unresolved


async def _load_instructions(gh: GitHubClient, cfg: Config, repo: str, base_sha: str) -> str | None:
    parts: list[str] = []
    for path in cfg.instructions:
        try:
            content = await gh.get_file_content(repo, path, base_sha)
        except Exception:  # noqa: BLE001
            content = None
        if content:
            parts.append(f"--- {path} ---\n{content}")
    return "\n\n".join(parts) if parts else None


def _assign_fingerprints(findings: list[Finding], workspace: Path) -> None:
    for f in findings:
        line_content = ""
        try:
            lines = (workspace / f.path).read_text(encoding="utf-8", errors="replace").splitlines()
            idx = (f.anchored_line or f.line) - 1
            if 0 <= idx < len(lines):
                line_content = lines[idx].strip()
        except OSError:
            pass
        f.fingerprint = state.fingerprint(f.path, line_content, f.title)


_MARK_TITLE_RE = re.compile(r"\*\*(?:CRITICAL|HIGH|MEDIUM|LOW)\*\*:\s*(.+)")


def _mark_title(body: str) -> str:
    m = _MARK_TITLE_RE.search(body)
    return m.group(1).strip() if m else ""


def _filter_already_open(findings: list[Finding], marks: list) -> list[Finding]:
    """Drop findings that already have an open thread (fingerprint match, or
    same path + line within snap distance + similar title)."""
    from .pipeline import (
        CLUSTER_LINE_DISTANCE,
        CLUSTER_TITLE_JACCARD,
        _jaccard,
        _title_tokens,
    )

    open_marks = [m for m in marks if not m.thread_resolved]
    if not open_marks:
        return findings
    open_fps = {m.fingerprint for m in open_marks}

    kept: list[Finding] = []
    for f in findings:
        if f.fingerprint in open_fps:
            continue
        f_tokens = _title_tokens(f.title)
        f_line = f.anchored_line or f.line
        duplicate = any(
            m.path == f.path
            and m.line is not None
            and abs(m.line - f_line) <= CLUSTER_LINE_DISTANCE
            and _jaccard(f_tokens, _title_tokens(_mark_title(m.body))) >= CLUSTER_TITLE_JACCARD
            for m in open_marks
        )
        if not duplicate:
            kept.append(f)
    if len(kept) != len(findings):
        logger.info("dedupe: dropped %d already-open finding(s)", len(findings) - len(kept))
    return kept
