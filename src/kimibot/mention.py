"""@kimi-bot mention commands — ported from hoverstare's mention.rs.

Commands (collaborators only): `review` (force full re-review), `explain`
(plain-language explanation of the finding in the thread, <=300 words), `help`.
Reactions: 🚀 on pickup, +1 on success, -1 on failure.
"""

from __future__ import annotations

import logging

from . import prompt as prompt_mod
from .agent import KimiBackend, make_budget
from .config import Config
from .github import GitHubClient
from .orchestrator import run_review
from .types import AgentError, MentionEvent, ReviewRequest, ReviewTarget

logger = logging.getLogger(__name__)

_AUTHORIZED = {"OWNER", "MEMBER", "COLLABORATOR"}

HELP_TEXT = """\
Available commands:
- `@kimi-bot review` — force a full re-review of this PR
- `@kimi-bot explain` — explain the finding in this thread in plain language
- `@kimi-bot help` — show this message
"""


def parse_command(body: str) -> str:
    """`body` is the command part after the mention (see event.py)."""
    cmd = (body.strip().split() or [""])[0].lower()
    if cmd in ("review", "explain", "help"):
        return cmd
    return "help" if not cmd else ""


async def run_mention(
    cfg: Config,
    gh: GitHubClient,
    ev: MentionEvent,
    backend: KimiBackend | None = None,
) -> int:
    backend = backend or KimiBackend()

    if ev.author_association not in _AUTHORIZED:
        try:
            if not await gh.is_collaborator(ev.repo, ev.comment_author):
                logger.info("ignoring mention from non-collaborator %s", ev.comment_author)
                return 0
        except Exception:  # noqa: BLE001
            return 0

    command = parse_command(ev.comment_body)
    if not command:
        return 0

    try:
        await gh.add_reaction(ev.repo, ev.comment_id, "rocket")
    except Exception:  # noqa: BLE001
        pass

    ok = True
    try:
        if command == "review":
            rc = await run_review(
                cfg, gh, ReviewTarget(repo=ev.repo, pr_number=ev.pr_number, force_full=True), backend
            )
            ok = rc == 0
        elif command == "explain":
            await _explain(cfg, gh, ev, backend)
        else:
            await gh.create_issue_comment(ev.repo, ev.pr_number, HELP_TEXT)
    except AgentError as e:
        logger.error("mention command %s failed: %s", command, e)
        ok = False
    except Exception:  # noqa: BLE001
        logger.exception("mention command %s failed unexpectedly", command)
        ok = False

    try:
        await gh.add_reaction(ev.repo, ev.comment_id, "+1" if ok else "-1")
    except Exception:  # noqa: BLE001
        pass
    return 0 if ok else 1


async def _explain(cfg: Config, gh: GitHubClient, ev: MentionEvent, backend: KimiBackend) -> None:
    # gather thread context: the finding comment being replied to + the mention itself
    parts: list[str] = []
    if ev.in_reply_to_id:
        try:
            parent = await gh.get_pull_comment(ev.repo, ev.in_reply_to_id)
            loc = f" ({parent.get('path')}:{parent.get('line')})" if parent.get("path") else ""
            parts.append(f"Finding under discussion{loc}:\n{parent.get('body', '')}")
        except Exception:  # noqa: BLE001 — context is best-effort
            pass
    parts.append(f"User question: {ev.comment_body or 'explain this finding'}")
    system, user = prompt_mod.explain_prompt("\n\n".join(parts), cfg.language)
    run = await backend.run(
        ReviewRequest(
            system_prompt=system,
            user_prompt=user,
            work_dir=str(cfg.workspace),
            budget=make_budget(cfg.max_tool_calls, cfg.timeout_secs),
            model=cfg.model,
            lens="explain",
        )
    )
    text = run.raw_output.strip() or "Sorry, I could not generate an explanation."
    if ev.in_reply_to_id:
        await gh.create_reply(ev.repo, ev.pr_number, ev.comment_id, text)
    else:
        await gh.create_issue_comment(ev.repo, ev.pr_number, text)
