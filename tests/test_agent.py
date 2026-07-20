"""Unit tests for agent.py template generation (no network, no kimi-cli needed)."""

import pytest

from kimibot.agent import _prepare_agent_dir, make_budget
from kimibot.types import AgentError


def test_prepare_agent_dir_with_tools(tmp_path):
    yaml_path = _prepare_agent_dir(tmp_path, "kimi-bot-reviewer", "You review code.", True)
    text = yaml_path.read_text()
    assert "kimi_cli.tools.file:ReadFile" in text
    assert "kimi_cli.tools.file:Grep" in text
    assert "kimi_cli.tools.file:Glob" in text
    assert "kimi_cli.tools.shell:Shell" in text
    assert "WriteFile" not in text
    assert "system_prompt_path: ./kimi-bot-reviewer.system.md" in text
    assert (tmp_path / ".kimi-bot" / "kimi-bot-reviewer.system.md").read_text() == "You review code."


def test_prepare_agent_dir_no_tools(tmp_path):
    yaml_path = _prepare_agent_dir(tmp_path, "kimi-bot-reformat", "Reformat.", False)
    assert "tools: []" in yaml_path.read_text()


def test_prepare_agent_dir_rejects_template_sequences(tmp_path):
    with pytest.raises(AgentError):
        _prepare_agent_dir(tmp_path, "x", "bad ${PROMPT}", True)
    with pytest.raises(AgentError):
        _prepare_agent_dir(tmp_path, "x", "bad {% raw %}", True)


def test_make_budget_headroom():
    b = make_budget(max_tool_calls=20, timeout_secs=900)
    assert b.max_steps == 22  # +2 wrap-up headroom
    assert b.timeout_secs == 900
