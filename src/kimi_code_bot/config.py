"""Configuration loading.

Merge priority: environment variables > .github/kimi-code-bot.toml (workspace root) > defaults.
LLM credentials are NOT managed here — they belong to kimi-cli (env vars like
KIMI_API_KEY / KIMI_BASE_URL / KIMI_MODEL_NAME, `kimi login`, or ~/.kimi/config.toml).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = ".github/kimi-code-bot.toml"


class ConfigError(Exception):
    """Configuration-zone error. Fatal (exit 1) regardless of fail-open."""


@dataclass
class Config:
    # LLM (passed through to kimi-cli as model aliases; None => kimi-cli default)
    model: str | None = None
    reformat_model: str | None = None

    # review behavior
    passes: int = 3
    verify: bool = True
    severity_threshold: str = "medium"
    ignore: list[str] = field(default_factory=list)
    max_diff_kb: int = 400
    max_tool_calls: int = 20
    timeout_secs: int = 900
    review_drafts: bool = False
    fail_closed: bool = False
    status_checks: bool = True
    language: str = "en"
    # mention trigger for @commands (default avoids collision with the
    # existing GitHub user @kimi-bot)
    mention: str = "@kimi-code-bot"
    # repo instruction files loaded from the BASE branch (prompt augmentation)
    instructions: list[str] = field(default_factory=lambda: ["AGENTS.md"])

    # github runtime (from env)
    github_token: str = ""
    gh_pat: str | None = None  # for resolveReviewThread (default GITHUB_TOKEN can't)
    repo: str = ""
    workspace: Path = Path(".")
    event_path: Path | None = None
    event_name: str = ""
    api_url: str = "https://api.github.com"
    in_actions: bool = False


_ENV_TOML_KEYS = {
    "model": "KIMIBOT_MODEL",
    "reformat_model": "KIMIBOT_REFORMAT_MODEL",
    "passes": "KIMIBOT_PASSES",
    "verify": "KIMIBOT_VERIFY",
    "severity_threshold": "KIMIBOT_SEVERITY_THRESHOLD",
    "max_diff_kb": "KIMIBOT_MAX_DIFF_KB",
    "max_tool_calls": "KIMIBOT_MAX_TOOL_CALLS",
    "timeout_secs": "KIMIBOT_TIMEOUT_SECS",
    "review_drafts": "KIMIBOT_REVIEW_DRAFTS",
    "fail_closed": "KIMIBOT_FAIL_CLOSED",
    "status_checks": "KIMIBOT_STATUS_CHECKS",
    "language": "KIMIBOT_LANGUAGE",
    "mention": "KIMIBOT_MENTION",
}

_BOOL_KEYS = {"verify", "review_drafts", "fail_closed", "status_checks"}
_INT_KEYS = {"passes", "max_diff_kb", "max_tool_calls", "timeout_secs"}
_VALID_FIELDS = set(_ENV_TOML_KEYS) | {"ignore", "instructions"}


def _coerce(key: str, value: object) -> object:
    if key in _BOOL_KEYS and isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if key in _INT_KEYS and isinstance(value, str):
        return int(value)
    return value


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    cfg = Config()

    workspace = Path(env.get("GITHUB_WORKSPACE", ".")).resolve()
    cfg.workspace = workspace

    # --- toml layer
    toml_file = workspace / CONFIG_PATH
    if toml_file.is_file():
        try:
            data = tomllib.loads(toml_file.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as e:
            raise ConfigError(f"invalid {CONFIG_PATH}: {e}") from e
        unknown = set(data) - _VALID_FIELDS
        if unknown:
            raise ConfigError(f"{CONFIG_PATH}: unknown fields: {sorted(unknown)}")
        for k, v in data.items():
            setattr(cfg, k, _coerce(k, v))

    # --- env layer (wins over toml)
    for field_name, env_name in _ENV_TOML_KEYS.items():
        if env_name in env:
            setattr(cfg, field_name, _coerce(field_name, env[env_name]))

    # --- github runtime
    cfg.github_token = env.get("GITHUB_TOKEN", "")
    cfg.gh_pat = env.get("GH_PAT") or None
    cfg.repo = env.get("GITHUB_REPOSITORY", "")
    cfg.event_name = env.get("GITHUB_EVENT_NAME", "")
    cfg.api_url = env.get("GITHUB_API_URL", "https://api.github.com")
    cfg.in_actions = env.get("GITHUB_ACTIONS", "").lower() == "true"
    if ep := env.get("GITHUB_EVENT_PATH"):
        cfg.event_path = Path(ep)

    if cfg.severity_threshold not in ("critical", "high", "medium", "low"):
        raise ConfigError(f"invalid severity_threshold: {cfg.severity_threshold}")
    if cfg.passes < 1:
        raise ConfigError("passes must be >= 1")
    return cfg
