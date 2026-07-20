# kimi-bot — AGENTS.md

Repo-aware AI code review bot powered by kimi-cli (library API). Python port of
hoverstare's architecture (../bugbot, Rust/rig), minus rig — all LLM/agent-loop
concerns are delegated to kimi-cli.

## Architecture rule (the one that matters)

**`src/kimibot/agent.py` is the ONLY module allowed to `import kimi_cli`** (and
`kaos`/`kosong`). Everything else talks to the framework-agnostic `ReviewBackend`
protocol (`types.py`). If you need a new LLM capability, extend `agent.py`, never
leak kimi_cli types upward.

## Module map

| module | role |
|---|---|
| `types.py` | shared dataclasses + ReviewBackend protocol (dependency-free) |
| `config.py` | env > `.github/kimi-bot.toml` > defaults; `ConfigError` => exit 1 |
| `event.py` | GITHUB_EVENT_PATH -> ReviewTarget / MentionEvent |
| `github.py` | httpx REST + GraphQL client (retry/backoff; GraphQL errors are HTTP 200 + errors field) |
| `diff.py` | unified diff parser, ignore filter, truncation, commentable lines, snap |
| `findings.py` | 3-level JSON extraction (direct/fence/braces) + jsonschema + normalization |
| `state.py` | fingerprints (sha1(path+line content+title)[:16]), finding/meta markers |
| `prompt.py` | system contract / lens / user / verifier / reformat / explain prompts (en|zh) |
| `agent.py` | KimiBackend: generates agent yaml+system.md, runs kimi-cli, collects events |
| `pipeline.py` | multi-pass lenses -> cluster (Jaccard, CJK n-gram) -> vote >=2 -> verifier |
| `report.py` | anchor chain Exact->Snapped->BodySection, same-anchor merge, render |
| `orchestrator.py` | full review flow; incremental; resolve fixed; status checks |
| `mention.py` | @kimi-bot review/explain/help (collaborators only, reactions) |
| `cli.py` | `kimi-bot review|mention`; bare invocation = event dispatch |

## Invariants

- **Fail-open**: analysis-zone failures exit 0 (never redden CI); config errors and
  publish double-failures exit 1. `fail_closed = true` flips analysis to exit 1.
- Review tools are **read-only** (whitelist in `agent.py:REVIEWER_TOOLS`); Shell is
  only for `git show base:file` etc. — the system prompt forbids writes.
- Repo instructions are loaded from the **BASE branch** (head injection defense).
- Auto-resolve uses **model-verified `resolved_finding_ids` only** (the review
  contract asks the model to verify each open finding against current code).
  "Not re-reported" never implies "fixed" (may be out of scope). Replies are
  deduped by comment_id (same-anchor merged comments carry multiple markers).
- Machine-readable content (JSON schema, `<!-- kimi-bot-finding:* -->`,
  `<!-- kimi-bot-meta ... -->`) is NEVER localized.
- Budget rule: `max_steps = max_tool_calls + 2` (wrap-up headroom).
- Generated system prompts must not contain `${` or `{%` (kimi-cli Jinja syntax).

## Dev

```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest            # unit tests (no network)
uv run ruff check src/ tests/
uv run python examples/smoke_backend.py   # real kimi-cli call (needs KIMI_API_KEY)
```
