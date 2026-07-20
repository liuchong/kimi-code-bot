"""Tests for kimibot.report.

kimibot.diff / kimibot.state are implemented by other modules; when they are
not present yet, minimal stubs matching their documented interfaces are
injected so these tests stay self-contained. When the real modules land, they
are used instead.
"""

import sys
import types as _types


def _ensure_parallel_module_stubs():
    import kimibot  # noqa: F401  (package must import first)

    try:
        import kimibot.diff  # noqa: F401
    except ImportError:

        def snap_line(f, line):
            """Nearest commentable line (ties -> earlier line), or None."""
            lines = f.commentable_lines
            if not lines:
                return None
            return min(lines, key=lambda ln: (abs(ln - line), ln))

        def render_diff(files):
            return "\n".join(f.path for f in files)

        stub = _types.ModuleType("kimibot.diff")
        stub.snap_line = snap_line
        stub.render_diff = render_diff
        sys.modules["kimibot.diff"] = stub

    try:
        import kimibot.state  # noqa: F401
    except ImportError:
        stub = _types.ModuleType("kimibot.state")
        stub.FINDING_MARK = "<!-- kimi-bot-finding:{fp} -->"
        stub.fingerprint = lambda *parts: "stubfp"
        sys.modules["kimibot.state"] = stub


_ensure_parallel_module_stubs()

from kimibot import report  # noqa: E402
from kimibot.state import FINDING_MARK  # noqa: E402
from kimibot.types import DiffFile, DiffHunk, Finding, ReviewMeta  # noqa: E402

# ------------------------------------------------------------------ fixtures


def _diff_file(path="src/a.py"):
    # new-side commentable lines: 10 (ctx), 11 (+new1), 12 (+new2)
    return DiffFile(
        path=path,
        status="modified",
        hunks=[DiffHunk(1, 2, 10, 3, lines=[" ctx", "-old", "+new1", "+new2"])],
    )


def _finding(path="src/a.py", line=11, severity="high", title="t", fp="fp1"):
    return Finding(
        path=path,
        line=line,
        severity=severity,
        title=title,
        description="d",
        fingerprint=fp,
    )


def _meta(mode="full"):
    return ReviewMeta(head_sha="abc123", mode=mode, files_reviewed=1, fingerprints=["fp1"])


# ------------------------------------------------------------------ anchoring


def test_anchor_exact():
    inline, cross, nitpicks = report.anchor_findings([_finding(line=11)], [_diff_file()], "medium")
    assert len(inline) == 1 and not cross and not nitpicks
    assert inline[0].anchored_line == 11


def test_anchor_snapped():
    # commentable lines are 10/11/12; SNAP_DISTANCE=3, so 14 snaps to 12
    f = _finding(line=14)
    inline, cross, nitpicks = report.anchor_findings([f], [_diff_file()], "medium")
    assert len(inline) == 1 and not cross and not nitpicks
    assert inline[0].anchored_line == 12  # snapped to nearest commentable line
    body = report.render_inline(inline[0], "en")
    assert "anchored to the nearest changed line" in body


def test_anchor_snap_too_far_goes_to_body():
    # beyond SNAP_DISTANCE -> no snap target -> BodySection (cross_cutting)
    f = _finding(line=999)
    inline, cross, nitpicks = report.anchor_findings([f], [_diff_file()], "medium")
    assert not inline and len(cross) == 1 and not nitpicks
    assert cross[0].anchored_line is None


def test_anchor_body_section_path_not_in_diff():
    inline, cross, nitpicks = report.anchor_findings(
        [_finding(path="src/other.py", line=5, severity="critical")],
        [_diff_file()],
        "medium",
    )
    assert not inline and len(cross) == 1 and not nitpicks
    assert cross[0].anchored_line is None


def test_anchor_body_section_no_commentable_lines():
    deleted = DiffFile(path="src/gone.py", status="deleted", hunks=[])
    inline, cross, _ = report.anchor_findings(
        [_finding(path="src/gone.py", line=3)], [deleted], "medium"
    )
    assert not inline and len(cross) == 1


def test_threshold_split():
    fs = [_finding(severity="low", fp="a"), _finding(severity="medium", fp="b")]
    inline, cross, nitpicks = report.anchor_findings(fs, [_diff_file()], "medium")
    assert [f.fingerprint for f in inline] == ["b"]
    assert [f.fingerprint for f in nitpicks] == ["a"]
    assert not cross


# ------------------------------------------------------------------ merging / inline rendering


def test_same_anchor_merges_into_one_comment():
    fs = [_finding(line=11, severity="high", title="t1", fp="a"),
          _finding(line=12, severity="critical", title="t2", fp="b")]
    # force both onto the same anchor
    fs[1].line = 11
    inline, _, _ = report.anchor_findings(fs, [_diff_file()], "medium")
    comments = report.build_inline_comments(inline, "en")
    assert len(comments) == 1
    assert comments[0].line == 11 and comments[0].path == "src/a.py"
    assert "\n\n---\n\n" in comments[0].body
    assert "t1" in comments[0].body and "t2" in comments[0].body


def test_render_inline_fingerprint_marker_and_suggestion():
    f = _finding(line=11, severity="critical")
    f.anchored_line = 11
    f.suggestion = "x = 1"
    body = report.render_inline(f, "en")
    assert body.startswith("🔴 **CRITICAL**: t")
    assert "```suggestion\nx = 1\n```" in body
    assert FINDING_MARK.format(fp="fp1") in body
    # marker is always on the last line
    assert body.strip().splitlines()[-1] == FINDING_MARK.format(fp="fp1")
    # exact anchor -> no snap note
    assert "anchored to the nearest changed line" not in body


def test_render_inline_severity_emoji():
    for sev, emoji in (("critical", "🔴"), ("high", "🟠"), ("medium", "🟡"), ("low", "🔵")):
        f = _finding(severity=sev)
        f.anchored_line = 11
        assert report.render_inline(f, "en").startswith(emoji)


# ------------------------------------------------------------------ body rendering


def test_body_contains_meta_comment():
    inline, cross, nitpicks = report.anchor_findings([_finding()], [_diff_file()], "medium")
    body = report.render_body(
        inline=inline, cross_cutting=cross, nitpicks=nitpicks, meta=_meta(), language="en"
    )
    assert body.startswith("## 🤖 kimi-bot Review")
    assert "<!-- kimi-bot-meta" in body
    # meta comment is rendered by state.render_meta (machine-readable format)
    assert "head_sha=abc123" in body
    assert "mode=full" in body
    assert "finding: fp1" in body
    assert "1 inline comment(s), 0 cross-file/unanchored finding(s)" in body
    # no clean verdict when there are findings
    assert "No defects found" not in body


def test_body_stats_threshold_via_stats_dict():
    inline, cross, nitpicks = report.anchor_findings([_finding()], [_diff_file()], "medium")
    body = report.render_body(
        inline=inline,
        cross_cutting=cross,
        nitpicks=nitpicks,
        meta=_meta(),
        language="en",
        stats={"threshold": "medium"},
    )
    assert "(threshold: medium)" in body


def test_body_clean_verdict():
    body = report.render_body(
        inline=[], cross_cutting=[], nitpicks=[], meta=_meta(), language="en"
    )
    assert "✅ No defects found." in body
    assert "<!-- kimi-bot-meta" in body


def test_body_cross_cutting_and_nitpicks_sections():
    inline, cross, nitpicks = report.anchor_findings(
        [_finding(path="other.py", severity="critical", fp="x"),
         _finding(severity="low", fp="y")],
        [_diff_file()],
        "medium",
    )
    body = report.render_body(
        inline=inline, cross_cutting=cross, nitpicks=nitpicks, meta=_meta(), language="en"
    )
    assert "Cross-cutting findings" in body
    assert "`other.py:11`" in body
    assert "<details>" in body and "Nitpicks (1)" in body
    assert "🔵 **LOW**" in body


def test_body_truncated_note():
    body = report.render_body(
        inline=[], cross_cutting=[], nitpicks=[], meta=_meta(), language="en", truncated=True
    )
    assert "truncated" in body


def test_body_zh_rendering():
    fs = [_finding(line=14, fp="a")]  # snapped -> zh snap note
    inline, cross, nitpicks = report.anchor_findings(fs, [_diff_file()], "medium")
    body = report.render_body(
        inline=inline,
        cross_cutting=cross,
        nitpicks=nitpicks,
        meta=_meta(mode="incremental"),
        language="zh",
        stats={"threshold": "medium"},
    )
    assert "审查范围" in body and "增量审查" in body
    assert "共 1 条行内评论、0 条跨文件/未锚定发现（阈值：medium）。" in body
    assert "<!-- kimi-bot-meta" in body  # machine-readable, never localized
    comment_body = report.render_inline(inline[0], "zh")
    assert "已吸附到最近的变更行" in comment_body


def test_body_zh_clean_verdict():
    body = report.render_body(
        inline=[], cross_cutting=[], nitpicks=[], meta=_meta(), language="zh"
    )
    assert "✅ 未发现缺陷。" in body


def test_body_unsupported_language_falls_back_to_en():
    body = report.render_body(
        inline=[], cross_cutting=[], nitpicks=[], meta=_meta(), language="klingon"
    )
    assert "✅ No defects found." in body
