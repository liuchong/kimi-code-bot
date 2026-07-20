# kimi-code-bot

Repo-aware AI code review bot, powered by [kimi-cli](https://github.com/MoonshotAI/kimi-cli).

Instead of dumping a diff into a model, kimi-code-bot lets the review agent **browse the
repository like a human reviewer** (read-only tools provided by kimi-cli), then
suppresses false positives with **multi-pass parallel review + cluster voting + an
independent verifier**. All LLM/agent-loop concerns are delegated to kimi-cli —
this repo is a thin orchestration layer (a Python port of
[hoverstare](https://github.com/liuchong/hoverstare)'s architecture, minus rig).

> 💡 Like this project? Also check out [hoverstare](https://github.com/liuchong/hoverstare) —
> the original Rust implementation of the same architecture (powered by rig
> instead of kimi-cli), with a self-hosted serve mode and an agent develop mode.

## Features

- 🔍 **Targeted verification**: the agent reads files / greps / checks `git show base:file` before claiming a bug
- 🗳 **Multi-pass voting**: 3 lenses (correctness / concurrency / security) run in parallel; findings need ≥2 votes, single-vote findings go to a verifier ("rejection needs evidence, doubt favors keeping")
- 💬 **Inline review comments** with severity (🔴🟠🟡🔵), `suggestion` blocks, and drift-immune fingerprints
- ⏩ **Incremental review** on `synchronize`: only the delta since the last review is re-analyzed
- ✅ **Auto-resolve** fixed threads; `✅ confirmed fixed` fallback without a PAT
- 🏷 **Status checks**: `kimi-code-bot` / `kimi-code-bot-findings`
- 🗣 `@kimi-code-bot review|explain|help` commands (collaborators only)
- 🛡 **Fail-open**: analysis failures never redden your CI (exit 0); config errors and publish failures exit 1

## Usage (GitHub Action)

```yaml
name: kimi-code-bot
on:
  pull_request: { types: [opened, reopened, synchronize] }
  issue_comment: { types: [created] }
  pull_request_review_comment: { types: [created] }

permissions:
  pull-requests: write
  issues: write
  contents: read
  statuses: write

env:
  KIMI_API_KEY: ${{ secrets.KIMI_API_KEY }}

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: liuchong/kimi-code-bot@v0.0.1
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

LLM credentials belong to kimi-cli: set `KIMI_API_KEY` (+ optional `KIMI_BASE_URL` /
`KIMI_MODEL_NAME`) as job env, or any provider kimi-cli supports.

## Configuration

`.github/kimi-code-bot.toml` in the reviewed repo (all fields optional):

```toml
model = "kimi-for-coding"              # kimi-cli model alias
reformat_model = "kimi-for-coding"     # cheap model for the JSON reformat pass
passes = 3                             # review lenses (1-3)
verify = true                          # verifier for single-vote findings
severity_threshold = "medium"          # below this -> Nitpicks
ignore = ["*.lock", "dist/**"]
max_diff_kb = 400
max_tool_calls = 20
timeout_secs = 900
review_drafts = false
fail_closed = false                    # true: analysis failures exit 1
status_checks = true
language = "en"                        # en | zh
mention = "@kimi-code-bot"             # @command trigger (avoid collisions with real users)
instructions = ["AGENTS.md"]           # loaded from the BASE branch
```

Every field can be overridden with a `KIMIBOT_*` env var (env > toml > defaults).

## Local CLI

```bash
uv tool install kimi-code-bot
export GITHUB_TOKEN=... KIMI_API_KEY=...
kimi-code-bot review --repo owner/name --pr 123 [--full] [--dry-run]
```

## Development

```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest
```

Layout: `agent.py` is the **only** module allowed to import `kimi_cli` (the backend
switch point); everything else is framework-agnostic orchestration.

## License

[Apache-2.0](LICENSE)
