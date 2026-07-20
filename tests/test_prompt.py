"""Tests for kimi_code_bot.prompt."""

from kimi_code_bot import prompt
from kimi_code_bot.types import DiffFile, DiffHunk, Finding


def _file(path="src/a.py"):
    return DiffFile(
        path=path,
        status="modified",
        hunks=[DiffHunk(1, 2, 10, 3, lines=[" ctx", "-old", "+new1", "+new2"])],
    )


def _finding(fp="fp1"):
    return Finding(
        path="src/a.py",
        line=11,
        severity="high",
        title="t",
        description="d" * 500,
        fingerprint=fp,
    )


# ------------------------------------------------------------------ system prompt


def test_system_prompt_fixed_contract():
    s = prompt.system_prompt()
    for marker in (
        "[SCOPE]",
        "[EXCLUSIONS]",
        "[LINE NUMBERS]",
        "[TARGETED VERIFICATION DISCIPLINE]",
        "[UNTRUSTED DATA]",
        "[OUTPUT CONTRACT]",
    ):
        assert marker in s
    # read-only tools provided by kimi-cli
    assert "read" in s and "grep" in s and "glob" in s and "git show" in s
    # untrusted data: never execute instructions embedded in the diff
    assert "never executed" in s


def test_system_prompt_language_directive():
    assert "English" in prompt.system_prompt("en")
    assert "Simplified Chinese" in prompt.system_prompt("zh")
    assert "Simplified Chinese" in prompt.system_prompt("zh-CN")
    # unsupported languages fall back to English
    assert "Simplified Chinese" not in prompt.system_prompt("klingon")
    assert "English" in prompt.system_prompt("klingon")


# ------------------------------------------------------------------ lenses


def test_lens_instruction():
    assert "off-by-one" in prompt.lens_instruction("correctness")
    assert "race" in prompt.lens_instruction("concurrency").lower()
    assert "deadlock" in prompt.lens_instruction("concurrency").lower()
    sec = prompt.lens_instruction("security").lower()
    assert "injection" in sec and "deserialization" in sec


# ------------------------------------------------------------------ user prompt


def test_user_prompt_core_sections():
    p = prompt.user_prompt(
        pr_title="Fix bug",
        files=[_file()],
        diff_text="@@ -1,2 +1,3 @@",
        language="en",
    )
    assert '"Fix bug"' in p
    assert "[CHANGED FILES]" in p and "src/a.py (modified, +2/-1)" in p
    assert "[OUTPUT JSON SCHEMA]" in p
    assert "[PR DIFF]" in p and "@@ -1,2 +1,3 @@" in p
    # no optional sections when not given
    assert "[INCREMENTAL REVIEW]" not in p
    assert "[PREVIOUSLY REPORTED OPEN FINDINGS]" not in p
    assert "[REPO INSTRUCTIONS]" not in p


def test_user_prompt_incremental_note():
    p = prompt.user_prompt(
        pr_title="t",
        files=[_file()],
        diff_text="d",
        language="en",
        incremental_note="Previously reviewed up to abc1234.",
    )
    assert "[INCREMENTAL REVIEW]" in p
    assert "abc1234" in p
    assert "delta" in p  # delta defines review scope; full diff only for anchoring


def test_user_prompt_unresolved_findings():
    p = prompt.user_prompt(
        pr_title="t",
        files=[_file()],
        diff_text="d",
        language="en",
        unresolved=[_finding("deadbeef")],
    )
    assert "[PREVIOUSLY REPORTED OPEN FINDINGS]" in p
    assert "id: deadbeef" in p
    assert "location: src/a.py:11" in p
    assert "resolved_finding_ids" in p
    # long descriptions are truncated to 400 chars
    assert "d" * 401 not in p


def test_user_prompt_instructions():
    p = prompt.user_prompt(
        pr_title="t",
        files=[_file()],
        diff_text="d",
        language="en",
        instructions="Always check error paths.",
    )
    assert "[REPO INSTRUCTIONS]" in p
    assert "Always check error paths." in p


def test_user_prompt_schema_not_localized():
    en = prompt.user_prompt(pr_title="t", files=[], diff_text="d", language="en")
    zh = prompt.user_prompt(pr_title="t", files=[], diff_text="d", language="zh")
    for marker in ('"findings"', '"severity"', "critical|high|medium|low", '"resolved_finding_ids"'):
        assert marker in zh
    # schema block identical across languages; only the language directive differs
    assert en.split("【OUTPUT LANGUAGE】")[0] == zh.split("【OUTPUT LANGUAGE】")[0]
    assert "Simplified Chinese" in zh


# ------------------------------------------------------------------ verifier


def test_verifier_prompt():
    system, user = prompt.verifier_prompt(_finding("fp"), "en")
    assert "evidence" in system  # dismissal requires evidence
    assert "0.6" in system  # confirmation threshold
    assert '"confirmed"' in system and '"confidence"' in system and '"reason"' in system
    assert "src/a.py" in user and '"line": 11' in user
    assert "doubt favors retention" in system  # 存疑从留


def test_verifier_prompt_zh():
    system, _ = prompt.verifier_prompt(_finding(), "zh")
    assert "Simplified Chinese" in system
    assert '"confirmed"' in system  # machine-readable contract never localized


# ------------------------------------------------------------------ reformat


def test_reformat_prompt():
    p = prompt.reformat_prompt("some prose notes", "en")
    assert "format converter" in p
    assert "some prose notes" in p
    assert "[OUTPUT JSON SCHEMA]" in p
    assert "Do not add, remove, or invent findings" in p


# ------------------------------------------------------------------ explain


def test_explain_prompt():
    system, user = prompt.explain_prompt("thread text here", "zh")
    assert "300" in system
    assert "thread text here" in user
    assert "300" in user
    assert "Simplified Chinese" in user
