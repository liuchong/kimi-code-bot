"""kimi-cli integration — the ONLY module allowed to import kimi_cli.

Design rule (mirrors hoverstare's rig_backend.rs rule): everything LLM-specific lives
here; the rest of the codebase talks to the framework-agnostic ReviewBackend protocol.

A review run = one headless kimi-cli agent invocation via the library API:
    Session.create -> KimiCLI.create(afk, agent_file=<generated>) -> instance.run(prompt)
kimi-cli provides the agent loop, read-only tools, sandboxing (work dir) and retries.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .types import AgentError, Budget, ReviewRequest, ReviewRun, ToolCallRecord, Usage

logger = logging.getLogger(__name__)

# Read-only tool whitelist for review runs. Shell is included ONLY so the model can
# inspect git history (e.g. `git show base:file`); the system prompt forbids writes.
REVIEWER_TOOLS = [
    "kimi_cli.tools.file:ReadFile",
    "kimi_cli.tools.file:Glob",
    "kimi_cli.tools.file:Grep",
    "kimi_cli.tools.shell:Shell",
]

_AGENT_DIR = ".kimi-code-bot"

_YAML_TEMPLATE = """\
version: 1
agent:
  name: {name}
  system_prompt_path: ./{system_md}
{tools_block}
"""

_TOOLS_REVIEWER = "  tools:\n" + "\n".join(f'    - "{t}"' for t in REVIEWER_TOOLS) + "\n"
_TOOLS_NONE = "  tools: []\n"


def _prepare_agent_dir(work_dir: Path, name: str, system_prompt: str, with_tools: bool) -> Path:
    """Generate the agent spec (yaml + system.md) consumed by kimi-cli.

    The system prompt is written verbatim; it must not contain `${` or `{%` sequences
    (kimi-cli renders it as a Jinja template with ${ } variable syntax).
    """
    if "${" in system_prompt or "{%" in system_prompt:
        raise AgentError("system_prompt contains forbidden template sequences (${ / {%)")
    agent_dir = work_dir / _AGENT_DIR
    agent_dir.mkdir(parents=True, exist_ok=True)
    system_md = f"{name}.system.md"
    (agent_dir / system_md).write_text(system_prompt, encoding="utf-8")
    yaml_path = agent_dir / f"{name}.yaml"
    yaml_path.write_text(
        _YAML_TEMPLATE.format(
            name=name,
            system_md=system_md,
            tools_block=_TOOLS_REVIEWER if with_tools else _TOOLS_NONE,
        ),
        encoding="utf-8",
    )
    return yaml_path


class KimiBackend:
    """ReviewBackend implementation that delegates the whole agent loop to kimi-cli."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    async def run(self, req: ReviewRequest) -> ReviewRun:
        try:
            return await asyncio.wait_for(self._run(req), timeout=req.budget.timeout_secs)
        except TimeoutError as e:
            raise AgentError(f"kimi-cli run timed out after {req.budget.timeout_secs}s") from e

    async def _run(self, req: ReviewRequest) -> ReviewRun:
        from kaos.path import KaosPath  # noqa: PLC0415
        from kimi_cli.app import KimiCLI  # noqa: PLC0415
        from kimi_cli.session import Session  # noqa: PLC0415

        work_dir = Path(req.work_dir).resolve()
        agent_name = "kimi-code-bot-reviewer" if req.with_tools else "kimi-code-bot-reformat"
        agent_file = _prepare_agent_dir(work_dir, agent_name, req.system_prompt, req.with_tools)

        try:
            session = await Session.create(KaosPath.unsafe_from_local_path(work_dir))
            instance = await KimiCLI.create(
                session,
                config=None,  # ~/.kimi/config.toml + env vars (KIMI_API_KEY etc.)
                model_name=req.model,
                afk=True,
                runtime_afk=True,
                ui_mode="print",
                agent_file=agent_file,
                max_steps_per_turn=req.budget.max_steps,
            )
        except AgentError:
            raise
        except Exception as e:  # config/spec/LLM errors — analysis zone, recoverable
            raise AgentError(f"kimi-cli init failed: {e}") from e

        cancel_event = asyncio.Event()
        try:
            return await self._consume(instance, req, cancel_event)
        except AgentError:
            raise
        except Exception as e:
            raise AgentError(f"kimi-cli run failed: {e}") from e
        finally:
            cancel_event.set()
            try:
                await instance.shutdown_background_tasks()
                await instance.await_bg_tasks_shutdown()
            except Exception:  # noqa: BLE001 — cleanup must never mask the real error
                logger.debug("background task cleanup failed", exc_info=True)

    async def _consume(self, instance, req: ReviewRequest, cancel_event: asyncio.Event) -> ReviewRun:
        from kosong.message import TextPart, ToolCall  # noqa: PLC0415
        from kosong.tooling import ToolResult  # noqa: PLC0415
        from kimi_cli.wire.types import StatusUpdate, StepBegin, StepInterrupted, StepRetry  # noqa: PLC0415

        # Final-message-only collection (mirrors kimi-cli's FinalOnlyTextPrinter):
        # the buffer is reset at every step boundary; what survives the turn is final.
        buf: list[str] = []
        trace: list[ToolCallRecord] = []
        usage = Usage()
        steps = 0

        async for msg in instance.run(req.user_prompt, cancel_event, merge_wire_messages=True):
            match msg:
                case StepBegin() as sb:
                    steps = getattr(sb, "n", steps + 1)
                    buf.clear()
                case StepInterrupted() | StepRetry():
                    buf.clear()
                case TextPart(text=t):
                    buf.append(t)
                case ToolCall() as call:
                    args = call.function.arguments or ""
                    trace.append(
                        ToolCallRecord(
                            name=call.function.name,
                            arguments_summary=args[:200],
                        )
                    )
                case ToolResult() as result:
                    if trace:
                        trace[-1].ok = getattr(result, "ok", True) in (True, None)
                case StatusUpdate(token_usage=tu) if tu is not None:
                    usage = Usage(
                        input_tokens=getattr(tu, "input_other", 0) or 0,
                        output_tokens=getattr(tu, "output", 0) or 0,
                        cache_read_tokens=getattr(tu, "input_cache_read", 0) or 0,
                    )
                case _:
                    pass

        raw = "".join(buf).strip()
        if self.verbose:
            logger.info(
                "lens=%s steps=%d tool_calls=%d usage=%s",
                req.lens, steps, len(trace), json.dumps(usage.__dict__),
            )
        return ReviewRun(raw_output=raw, tool_trace=trace, usage=usage, steps=steps)


def make_budget(max_tool_calls: int, timeout_secs: int) -> Budget:
    """max_tool_calls + 2 headroom steps for the wrap-up turn (same rule as hoverstare)."""
    return Budget(max_steps=max_tool_calls + 2, timeout_secs=timeout_secs)
