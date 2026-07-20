"""Analysis pipeline: multi-pass parallel review -> clustering -> voting -> verifier.

Ported from hoverstare's pipeline.rs. Passes run the SAME review under different
"lenses" (focus instructions); findings need >=2 votes to be accepted outright,
single-vote findings go to an independent verifier ("rejection needs evidence,
doubt favors keeping", confidence >= 0.6).

Output fault-tolerance ladder (per pass):
  run -> parse -> [reformat pass with cheap model, no tools] -> [full retry] (x3)
Empty model output skips reformat and goes straight to retry.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from . import findings as findings_mod
from . import prompt as prompt_mod
from .agent import make_budget
from .config import Config
from .types import (
    AgentError,
    DiffFile,
    Finding,
    LENSES,
    LensName,
    ReviewBackend,
    ReviewRequest,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
REFORMAT_TIMEOUT_SECS = 120
SMALL_DIFF_ADDED_LINES = 50
VOTE_THRESHOLD = 2
VERIFY_CONFIDENCE = 0.6
CLUSTER_LINE_DISTANCE = 3
CLUSTER_TITLE_JACCARD = 0.5


@dataclass
class AnalysisResult:
    confirmed: list[Finding] = field(default_factory=list)
    cross_cutting: list[Finding] = field(default_factory=list)
    resolved_finding_ids: set[str] = field(default_factory=set)
    passes_run: int = 0
    stats: dict = field(default_factory=dict)


# ------------------------------------------------------------------- clustering


_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")
_WORD_RE = re.compile(r"[a-z0-9_]+")


def _title_tokens(title: str) -> set[str]:
    """CJK: unigrams + bigrams (word segmentation doesn't exist); latin: words."""
    title = title.lower()
    cjk = _CJK_RE.findall(title)
    tokens = set(cjk)
    tokens.update(a + b for a, b in zip(cjk, cjk[1:]))
    tokens.update(_WORD_RE.findall(title))
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _same_cluster(a: Finding, b: Finding) -> bool:
    return (
        a.path == b.path
        and abs(a.line - b.line) <= CLUSTER_LINE_DISTANCE
        and _jaccard(_title_tokens(a.title), _title_tokens(b.title)) >= CLUSTER_TITLE_JACCARD
    )


def cluster_findings(items: list[Finding]) -> list[list[Finding]]:
    """Union-find-ish greedy clustering; order-preserving."""
    clusters: list[list[Finding]] = []
    for f in items:
        for c in clusters:
            if _same_cluster(c[0], f):
                c.append(f)
                break
        else:
            clusters.append([f])
    return clusters


def _merge_cluster(cluster: list[Finding]) -> Finding:
    """Representative finding: highest severity, then highest confidence."""
    from .types import SEVERITY_ORDER

    rep = min(cluster, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), -f.confidence))
    rep.votes = len(cluster)
    rep.lenses = sorted({lens for f in cluster for lens in f.lenses})
    return rep


# ------------------------------------------------------------------- single pass


async def _run_pass(
    backend: ReviewBackend,
    req: ReviewRequest,
    reformat_model: str | None,
) -> tuple[list[Finding], list[Finding], set[str]] | None:
    """One review pass with the full fault-tolerance ladder. None => all attempts failed."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            run = await backend.run(req)
        except AgentError as e:
            logger.warning("pass %s attempt %d failed: %s", req.lens, attempt, e)
            continue
        parsed = findings_mod.parse_findings(run.raw_output)
        if parsed is not None:
            return parsed
        if not run.raw_output.strip():
            logger.warning("pass %s attempt %d: empty output, skipping reformat", req.lens, attempt)
            continue
        # reformat pass: cheap model rewrites prose into schema JSON, no tools
        try:
            reformat_req = ReviewRequest(
                system_prompt="You are a strict JSON reformatter. Output only valid JSON.",
                user_prompt=prompt_mod.reformat_prompt(run.raw_output, "en"),
                work_dir=req.work_dir,
                budget=make_budget(max_tool_calls=0, timeout_secs=REFORMAT_TIMEOUT_SECS),
                model=reformat_model or req.model,
                lens=f"{req.lens}-reformat",
                with_tools=False,
            )
            reformatted = await backend.run(reformat_req)
            parsed = findings_mod.parse_findings(reformatted.raw_output)
            if parsed is not None:
                return parsed
        except AgentError as e:
            logger.warning("pass %s reformat failed: %s", req.lens, e)
    logger.error("pass %s: all %d attempts failed", req.lens, MAX_ATTEMPTS)
    return None


# ------------------------------------------------------------------- verifier


_VERDICT_RE = re.compile(r"\{.*\}", re.DOTALL)


async def _verify(backend: ReviewBackend, req: ReviewRequest) -> bool:
    try:
        run = await backend.run(req)
    except AgentError as e:
        logger.warning("verifier failed, keeping finding (doubt favors keeping): %s", e)
        return True
    m = _VERDICT_RE.search(run.raw_output)
    if not m:
        return True
    try:
        import json

        verdict = json.loads(m.group(0))
        return bool(verdict.get("confirmed")) or float(verdict.get("confidence", 0)) < VERIFY_CONFIDENCE
    except (ValueError, TypeError):
        return True


# ------------------------------------------------------------------- entry


async def analyze(
    backend: ReviewBackend,
    *,
    cfg: Config,
    pr_title: str,
    files: list[DiffFile],
    diff_text: str,
    work_dir: str,
    incremental_note: str | None = None,
    unresolved: list[Finding] | None = None,
    instructions: str | None = None,
) -> AnalysisResult:
    added = sum(f.added_count for f in files)
    small_diff = added < SMALL_DIFF_ADDED_LINES

    passes = cfg.passes
    if small_diff:
        passes = 1
    lenses: list[tuple[LensName, float]] = LENSES[: max(1, min(passes, len(LENSES)))]

    budget = make_budget(cfg.max_tool_calls, cfg.timeout_secs)
    language = cfg.language

    async def one_lens(lens: LensName) -> tuple[list[Finding], list[Finding], set[str]] | None:
        req = ReviewRequest(
            system_prompt=prompt_mod.system_prompt(language)
            + "\n\n"
            + prompt_mod.lens_instruction(lens),
            user_prompt=prompt_mod.user_prompt(
                pr_title=pr_title,
                files=files,
                diff_text=diff_text,
                language=language,
                incremental_note=incremental_note,
                unresolved=unresolved,
                instructions=instructions,
            ),
            work_dir=work_dir,
            budget=budget,
            model=cfg.model,
            lens=lens,
        )
        return await _run_pass(backend, req, cfg.reformat_model)

    results = await asyncio.gather(*(one_lens(lens) for lens, _t in lenses))
    ok_results = [r for r in results if r is not None]
    result = AnalysisResult(passes_run=len(ok_results))
    if not ok_results:
        raise AgentError("all review passes failed")

    all_findings: list[Finding] = []
    all_cross: list[Finding] = []
    for parsed, (lens, _t) in zip(results, lenses):
        if parsed is None:
            continue
        items, cross, resolved_ids = parsed
        result.resolved_finding_ids.update(resolved_ids)
        for f in items:
            f.lenses = [lens]
        for f in cross:
            f.lenses = [lens]
        all_findings.extend(items)
        all_cross.extend(cross)

    # --- voting
    single_vote: list[Finding] = []
    for c in cluster_findings(all_findings):
        rep = _merge_cluster(c)
        if rep.votes >= VOTE_THRESHOLD:
            result.confirmed.append(rep)
        else:
            single_vote.append(rep)
    for c in cluster_findings(all_cross):
        rep = _merge_cluster(c)
        if rep.votes >= VOTE_THRESHOLD or len(lenses) == 1:
            result.cross_cutting.append(rep)

    # --- verifier for single-vote findings
    direct_passthrough = len(lenses) == 1 and not cfg.verify
    if direct_passthrough:
        result.confirmed.extend(single_vote)
    elif single_vote:
        if cfg.verify:
            verdicts = await asyncio.gather(
                *(
                    _verify(
                        backend,
                        _verifier_request(f, cfg, work_dir, budget, language),
                    )
                    for f in single_vote
                )
            )
            result.confirmed.extend(f for f, ok in zip(single_vote, verdicts) if ok)
        # verify=false with multi-pass: single-vote findings are dropped

    result.stats = {
        "lenses": [lens for lens, _ in lenses],
        "raw_findings": len(all_findings),
        "confirmed": len(result.confirmed),
        "small_diff": small_diff,
    }
    return result


def _verifier_request(
    finding: Finding, cfg: Config, work_dir: str, budget, language: str
) -> ReviewRequest:
    system, user = prompt_mod.verifier_prompt(finding, language)
    return ReviewRequest(
        system_prompt=system,
        user_prompt=user,
        work_dir=work_dir,
        budget=budget,
        model=cfg.model,
        lens="verifier",
    )
