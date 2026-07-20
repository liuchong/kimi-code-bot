"""CLI entry: `kimi-code-bot review|mention`, or event dispatch from GitHub Actions env."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .agent import KimiBackend
from .config import ConfigError, load_config
from .github import GitHubClient
from .types import MentionEvent, ReviewTarget

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kimi-code-bot", description="AI code review bot powered by kimi-cli")
    p.add_argument("--verbose", "-v", action="store_true")
    sub = p.add_subparsers(dest="command")

    r = sub.add_parser("review", help="review a pull request")
    r.add_argument("--repo", help="owner/name (default: $GITHUB_REPOSITORY)")
    r.add_argument("--pr", type=int, required=True)
    r.add_argument("--full", action="store_true", help="force full review (skip incremental)")
    r.add_argument("--dry-run", action="store_true", help="print review instead of posting")

    sub.add_parser("mention", help="handle an @kimi-code-bot mention (from Actions event env)")

    return p


async def _async_main(args: argparse.Namespace) -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1

    backend = KimiBackend(verbose=args.verbose)
    gh = GitHubClient(cfg.github_token, cfg.api_url, cfg.gh_pat)
    try:
        if args.command == "review":
            repo = args.repo or cfg.repo
            if not repo:
                print("error: --repo or GITHUB_REPOSITORY required", file=sys.stderr)
                return 1
            from .orchestrator import run_review

            return await run_review(
                cfg, gh, ReviewTarget(repo=repo, pr_number=args.pr, force_full=args.full),
                backend, dry_run=args.dry_run,
            )

        if args.command == "mention":
            from .event import parse_event
            from .mention import run_mention

            ev = parse_event(cfg)
            if not isinstance(ev, MentionEvent):
                print("error: no mention event found in environment", file=sys.stderr)
                return 1
            return await run_mention(cfg, gh, ev, backend)

        # no subcommand: dispatch from GitHub Actions event
        from .event import parse_event
        from .mention import run_mention
        from .orchestrator import run_review

        ev = parse_event(cfg)
        if isinstance(ev, ReviewTarget):
            return await run_review(cfg, gh, ev, backend)
        if isinstance(ev, MentionEvent):
            return await run_mention(cfg, gh, ev, backend)
        print("nothing to do (no actionable event)", file=sys.stderr)
        return 0
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — analysis zone: fail-open unless configured
        logger.exception("run failed")
        if cfg.fail_closed:
            return 1
        print(f"analysis failed (fail-open, exit 0): {e}", file=sys.stderr)
        return 0
    finally:
        await gh.close()


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    sys.exit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
