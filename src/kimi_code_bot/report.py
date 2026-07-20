"""Report rendering (port of bugbot src/report.rs).

Pipeline: threshold split -> anchor (Exact -> Snapped -> BodySection fallback
chain) -> merge findings sharing an anchor into one comment (prevents GitHub
422) -> render body with cross-cutting section, collapsed nitpicks, stats and
the machine-readable meta comment.

Human-readable strings are localized (en / zh, fallback en); machine-readable
payloads (severity keywords, FINDING_MARK, the meta comment) are never localized.
"""

from __future__ import annotations

from .diff import snap_line
from .state import FINDING_MARK
from .types import SEVERITY_ORDER, DiffFile, Finding, InlineComment, ReviewMeta

try:  # state.render_meta is optional; fall back to the local renderer below
    from .state import render_meta as _state_render_meta
except ImportError:  # pragma: no cover - depends on state.py landing
    _state_render_meta = None

# ------------------------------------------------------------------ i18n

_ZH_TAGS = {"zh", "zh-cn", "zh-hans", "cn", "chinese", "中文"}

_SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}


def _is_zh(language: str) -> bool:
    return language.strip().lower().replace("_", "-") in _ZH_TAGS


def _scope_heading(language: str) -> str:
    return "审查范围" if _is_zh(language) else "Review scope"


def _scope_label(meta: ReviewMeta, language: str) -> str:
    if meta.mode == "incremental":
        return "增量审查" if _is_zh(language) else "Incremental review"
    return "全量审查" if _is_zh(language) else "Full review"


def _files_count(n: int, language: str) -> str:
    return f"{n} 个文件" if _is_zh(language) else f"{n} file(s)"


def _clean_verdict(language: str) -> str:
    return "✅ 未发现缺陷。" if _is_zh(language) else "✅ No defects found."


def _stats_line(inline: int, cross: int, threshold: str | None, language: str) -> str:
    if _is_zh(language):
        s = f"共 {inline} 条行内评论、{cross} 条跨文件/未锚定发现"
        if threshold:
            s += f"（阈值：{threshold}）"
        return s + "。"
    s = f"{inline} inline comment(s), {cross} cross-file/unanchored finding(s)"
    if threshold:
        s += f" (threshold: {threshold})"
    return s + "."


def _snap_note(orig_line: int, language: str) -> str:
    if _is_zh(language):
        return f"> ⚠️ *模型报告的行为第 {orig_line} 行（不在 diff 中），已吸附到最近的变更行。*"
    return (
        f"> ⚠️ *Reported line {orig_line} is not in the diff; "
        "anchored to the nearest changed line.*"
    )


def _truncated_note(language: str) -> str:
    if _is_zh(language):
        return "> ⚠️ diff 超出大小预算已被截断，本次审查仅覆盖所包含的部分。"
    return (
        "> ⚠️ The diff exceeded the size budget and was truncated; "
        "this review covers the included portion only."
    )


def _cross_cutting_heading(language: str) -> str:
    return "### 🧩 跨文件 / 未锚定发现" if _is_zh(language) else "### 🧩 Cross-cutting findings"


def _nitpicks_summary(n: int, language: str) -> str:
    return f"ℹ️ Nitpicks（{n} 条）" if _is_zh(language) else f"ℹ️ Nitpicks ({n})"


# ------------------------------------------------------------------ anchoring


def anchor_findings(
    findings: list[Finding], files: list[DiffFile], severity_threshold: str
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """Split findings into (inline, cross_cutting, nitpicks).

    - nitpicks: severity below `severity_threshold` (never posted inline).
    - inline: anchored via the fallback chain Exact (line in commentable_lines)
      -> Snapped (diff.snap_line); `f.anchored_line` is set to the anchor line.
      Sorted by (path, anchored_line); use build_inline_comments() to merge
      findings sharing one anchor into a single comment (prevents GitHub 422).
    - cross_cutting: path not in the diff, no commentable lines, or no snap
      target (`f.anchored_line` left as None) — rendered in the review body.
    """
    by_path = {f.path: f for f in files}
    threshold = SEVERITY_ORDER[severity_threshold]
    inline: list[Finding] = []
    cross_cutting: list[Finding] = []
    nitpicks: list[Finding] = []

    for f in findings:
        if SEVERITY_ORDER[f.severity] > threshold:
            nitpicks.append(f)
            continue
        df = by_path.get(f.path)
        if df is None:
            cross_cutting.append(f)  # BodySection: file not in the diff
            continue
        commentable = df.commentable_lines
        if not commentable:
            cross_cutting.append(f)  # BodySection: e.g. deleted file
            continue
        if f.line in commentable:
            f.anchored_line = f.line  # Exact
            inline.append(f)
            continue
        snapped = snap_line(df, f.line)
        if snapped is None:
            cross_cutting.append(f)  # BodySection: no snap target
        else:
            f.anchored_line = snapped  # Snapped
            inline.append(f)

    inline.sort(key=lambda f: (f.path, f.anchored_line or 0))
    return inline, cross_cutting, nitpicks


# ------------------------------------------------------------------ rendering


def render_inline(f: Finding, language: str) -> str:
    """One inline comment body: severity header + description + optional
    ```suggestion block + snap note + hidden fingerprint marker (last line)."""
    s = f"{_SEVERITY_EMOJI[f.severity]} **{f.severity.upper()}**: {f.title}\n\n{f.description}"
    if f.suggestion:
        s += f"\n\n```suggestion\n{f.suggestion}\n```"
    if f.anchored_line is not None and f.anchored_line != f.line:
        s += f"\n\n{_snap_note(f.line, language)}"
    # Hidden marker: cross-commit tracking, always on the last line
    s += f"\n\n{FINDING_MARK.format(fp=f.fingerprint)}"
    return s


def build_inline_comments(inline: list[Finding], language: str) -> list[InlineComment]:
    """Merge findings sharing one (path, anchor) into a single InlineComment
    (GitHub rejects duplicate comments on the same line with HTTP 422)."""
    groups: dict[tuple[str, int], list[Finding]] = {}
    for f in inline:
        assert f.anchored_line is not None  # guaranteed by anchor_findings
        groups.setdefault((f.path, f.anchored_line), []).append(f)
    return [
        InlineComment(
            path=path,
            line=line,
            body="\n\n---\n\n".join(render_inline(f, language) for f in group),
        )
        for (path, line), group in sorted(groups.items())
    ]


def _render_meta(meta: ReviewMeta) -> str:
    lines = [
        "<!-- kimi-code-bot-meta",
        f"mode: {meta.mode}",
        f"head_sha: {meta.head_sha}",
        f"files_reviewed: {meta.files_reviewed}",
    ]
    if meta.fingerprints:
        lines.append("fingerprints: " + ",".join(meta.fingerprints))
    lines.append("-->")
    return "\n".join(lines)


def render_body(
    *,
    inline: list[Finding],
    cross_cutting: list[Finding],
    nitpicks: list[Finding],
    meta: ReviewMeta,
    language: str,
    truncated: bool = False,
    stats: dict | None = None,
) -> str:
    """The review body: heading + scope summary + cross-cutting section +
    collapsed nitpicks + stats line + machine-readable meta comment.

    `stats` may carry "threshold" (rendered in the stats line) plus any extra
    key/values, rendered verbatim in the footer.
    """
    b = "## 🤖 kimi-code-bot Review\n\n"
    b += (
        f"**{_scope_heading(language)}** — {_scope_label(meta, language)}; "
        f"{_files_count(meta.files_reviewed, language)}\n\n"
    )
    if truncated:
        b += _truncated_note(language) + "\n\n"

    if cross_cutting:
        b += _cross_cutting_heading(language) + "\n\n"
        for f in cross_cutting:
            b += f"{_SEVERITY_EMOJI[f.severity]} **{f.severity.upper()}**: {f.title}\n\n"
            b += f"{f.description}\n\n"
            b += f"> 📍 `{f.path}:{f.line}`\n\n"

    if nitpicks:
        b += "<details>\n"
        b += f"<summary>{_nitpicks_summary(len(nitpicks), language)}</summary>\n\n"
        for f in nitpicks:
            b += (
                f"- {_SEVERITY_EMOJI[f.severity]} **{f.severity.upper()}** "
                f"`{f.path}:{f.line}` — {f.title}\n"
            )
        b += "\n</details>\n\n"

    b += "---\n\n"
    if not inline and not cross_cutting:
        b += _clean_verdict(language) + "\n\n"
    else:
        threshold = (stats or {}).get("threshold")
        b += _stats_line(len(inline), len(cross_cutting), threshold, language) + "\n\n"

    extras = {k: v for k, v in (stats or {}).items() if k != "threshold"}
    if extras:
        b += "`" + " · ".join(f"{k}: {v}" for k, v in extras.items()) + "`\n\n"

    # Machine-readable metadata (incremental review depends on it)
    render = _state_render_meta if _state_render_meta is not None else _render_meta
    b += render(meta)
    return b
