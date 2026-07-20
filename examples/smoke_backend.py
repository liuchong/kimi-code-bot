"""Manual smoke test for the kimi-cli backend (needs KIMI_API_KEY / kimi login).

Usage: uv run python examples/smoke_backend.py
Creates a tiny temp repo, runs one reviewer pass (with tools) and one reformat
pass (no tools), prints raw output / tool trace / usage.
"""

import asyncio
import tempfile
from pathlib import Path

from kimi_code_bot.agent import KimiBackend, make_budget
from kimi_code_bot.types import ReviewRequest


async def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="kimi_code_bot-smoke-"))
    (work / "app.py").write_text(
        'def add(a, b):\n    return a - b  # BUG: should be +\n', encoding="utf-8"
    )
    backend = KimiBackend(verbose=True)

    print("=== pass 1: reviewer (with tools) ===")
    run = await backend.run(
        ReviewRequest(
            system_prompt=(
                "You are a code reviewer. Use the read-only tools to inspect files. "
                'Output ONLY JSON: {"findings": [{"path","line","severity","title","description"}], '
                '"cross_cutting": []}'
            ),
            user_prompt="Review app.py in the current directory and report any bugs.",
            work_dir=str(work),
            budget=make_budget(max_tool_calls=5, timeout_secs=300),
            lens="smoke",
        )
    )
    print("raw_output:", run.raw_output[:500])
    print("tool_trace:", [(t.name, t.ok) for t in run.tool_trace])
    print("usage:", run.usage)
    print("steps:", run.steps)

    print("\n=== pass 2: reformat (no tools) ===")
    run2 = await backend.run(
        ReviewRequest(
            system_prompt="You are a strict JSON reformatter. Output only valid JSON.",
            user_prompt='Rewrite as JSON {"findings": [...], "cross_cutting": []}: "app.py line 2 has a bug: add subtracts"',
            work_dir=str(work),
            budget=make_budget(max_tool_calls=0, timeout_secs=120),
            lens="smoke-reformat",
            with_tools=False,
        )
    )
    print("raw_output:", run2.raw_output[:500])


if __name__ == "__main__":
    asyncio.run(main())
