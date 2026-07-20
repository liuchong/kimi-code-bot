"""Tests for kimibot.diff (ported from diff.rs tests + Python-specific API)."""

from kimibot.diff import (
    filter_ignored,
    parse_diff,
    render_diff,
    snap_line,
    truncate_files,
)

SIMPLE = """\
diff --git a/src/main.rs b/src/main.rs
index 1111111..2222222 100644
--- a/src/main.rs
+++ b/src/main.rs
@@ -10,6 +10,7 @@ fn main() {
 context
-old
+new
+new2
 context2
"""


def test_parses_commentable_lines():
    files = parse_diff(SIMPLE)
    assert len(files) == 1
    f = files[0]
    assert f.path == "src/main.rs"
    assert f.status == "modified"
    lines = f.commentable_lines
    # context=10, new=11, new2=12, context2=13
    assert lines == {10, 11, 12, 13}
    assert f.added_count == 2
    h = f.hunks[0]
    assert (h.old_start, h.old_lines, h.new_start, h.new_lines) == (10, 6, 10, 7)


def test_hunk_header_variants():
    text = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +5,9 @@
 ctx
@@ -10 +12 @@ fn x() {
 ctx2
"""
    files = parse_diff(text)
    hunks = files[0].hunks
    assert (hunks[0].old_start, hunks[0].old_lines, hunks[0].new_start, hunks[0].new_lines) == (
        1, 2, 5, 9,
    )
    assert (hunks[1].old_start, hunks[1].old_lines, hunks[1].new_start, hunks[1].new_lines) == (
        10, 1, 12, 1,
    )
    assert files[0].commentable_lines == {5, 12}


def test_literal_diff_markers_inside_content_not_misread():
    # Content lines starting with "+++ b/" or "@@" must not be treated as
    # file headers when they appear (with a '+' prefix) inside a hunk body.
    text = """\
diff --git a/docs/guide.md b/docs/guide.md
--- a/docs/guide.md
+++ b/docs/guide.md
@@ -1,2 +1,4 @@
 intro
+example: +++ b/fake.rs
+example: @@ -1,1 +1,1 @@
 tail
"""
    files = parse_diff(text)
    assert len(files) == 1
    assert files[0].path == "docs/guide.md"
    assert files[0].commentable_lines == {1, 2, 3, 4}


def test_new_and_deleted_files():
    text = """\
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+a
+b
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
"""
    files = {f.path: f for f in parse_diff(text)}
    assert files["new.txt"].status == "added"
    assert files["old.txt"].status == "deleted"
    assert files["new.txt"].commentable_lines == {1, 2}
    # Deleted files exist but have no commentable lines on the RIGHT side
    assert files["old.txt"].commentable_lines == set()


def test_rename_tracked():
    text = """\
diff --git a/old.rs b/new.rs
similarity index 90%
rename from old.rs
rename to new.rs
--- a/old.rs
+++ b/new.rs
@@ -1,1 +1,1 @@
-x
+y
"""
    files = parse_diff(text)
    f = files[0]
    assert f.path == "new.rs"
    assert f.status == "renamed"
    assert f.old_path == "old.rs"


def test_binary_file():
    text = """\
diff --git a/logo.png b/logo.png
index 1111111..2222222 100644
Binary files a/logo.png and b/logo.png differ
"""
    # no +++/--- markers -> no path -> section is dropped (abnormal input)
    assert parse_diff(text) == []
    text_with_paths = """\
diff --git a/logo.png b/logo.png
index 1111111..2222222 100644
--- a/logo.png
+++ b/logo.png
Binary files a/logo.png and b/logo.png differ
"""
    files = parse_diff(text_with_paths)
    assert len(files) == 1
    assert files[0].path == "logo.png"
    assert files[0].is_binary


def test_no_newline_marker_and_malformed():
    text = """\
diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-x
\\ No newline at end of file
+y
\\ No newline at end of file
"""
    files = parse_diff(text)
    assert files[0].commentable_lines == {1}
    # garbage / empty input never raises, yields no files
    assert parse_diff("") == []
    assert parse_diff("garbage\n\n") == []
    # file section without any path marker is dropped
    assert parse_diff("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n") == []
    # truncated hunk header is skipped, not fatal
    weird = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ nonsense @@\n c\n"
    files = parse_diff(weird)
    assert len(files) == 1
    assert files[0].hunks == []


def test_filter_ignored():
    files = parse_diff(SIMPLE)
    kept, ignored = filter_ignored(files, ["**/*.rs"])
    assert kept == [] and len(ignored) == 1
    kept, ignored = filter_ignored(files, ["*.py"])
    assert len(kept) == 1 and ignored == []
    # basename-style pattern matches nested paths
    many = parse_diff(SIMPLE + SIMPLE.replace("src/main.rs", "deep/nested/Cargo.lock"))
    kept, ignored = filter_ignored(many, ["Cargo.lock"])
    assert [f.path for f in ignored] == ["deep/nested/Cargo.lock"]
    # "**/name" also matches a root-level file
    kept, ignored = filter_ignored(many, ["**/Cargo.lock"])
    assert len(ignored) == 1
    # empty ignore list keeps everything
    kept, ignored = filter_ignored(many, [])
    assert len(kept) == 2 and ignored == []


def _file_section(path: str, body_line: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1 +1 @@\n-x\n{body_line}\n"
    )


def test_truncate_keeps_source_over_docs():
    code = _file_section("src/a.rs", "+y")
    big_doc = _file_section("docs/big.md", "+" + "d" * 3000)
    files = parse_diff(code + big_doc)
    kept, truncated = truncate_files(files, 1)
    assert truncated
    assert [f.path for f in kept] == ["src/a.rs"]


def test_truncate_keeps_first_file_even_if_over_budget():
    files = parse_diff(_file_section("src/huge.rs", "+" + "x" * 9000))
    kept, truncated = truncate_files(files, 1)
    assert [f.path for f in kept] == ["src/huge.rs"]
    assert not truncated


def test_truncate_noop_under_budget():
    files = parse_diff(SIMPLE)
    kept, truncated = truncate_files(files, 400)
    assert not truncated
    assert [f.path for f in kept] == ["src/main.rs"]


def test_render_diff_round_trip():
    files = parse_diff(SIMPLE)
    rendered = render_diff(files)
    files2 = parse_diff(rendered)
    assert len(files2) == 1
    assert files2[0].path == files[0].path
    assert files2[0].status == files[0].status
    assert files2[0].commentable_lines == files[0].commentable_lines
    assert files2[0].hunks[0].lines == files[0].hunks[0].lines
    # idempotent
    assert render_diff(files2) == rendered


def test_render_diff_statuses():
    text = """\
diff --git a/new.txt b/new.txt
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,1 @@
+a
diff --git a/old.txt b/old.txt
--- a/old.txt
+++ /dev/null
@@ -1,1 +0,0 @@
-a
"""
    rendered = render_diff(parse_diff(text))
    assert "--- /dev/null" in rendered
    assert "+++ /dev/null" in rendered
    files2 = {f.path: f for f in parse_diff(rendered)}
    assert files2["new.txt"].status == "added"
    assert files2["old.txt"].status == "deleted"


def test_snap_line():
    f = parse_diff(SIMPLE)[0]
    # commentable line returned as-is
    assert snap_line(f, 11) == 11
    # not commentable but within distance 3 -> snap to nearest (12: dist 2, 13: dist 1)
    assert snap_line(f, 14) == 13
    assert snap_line(f, 15) == 13  # dist 2
    # too far away -> None
    assert snap_line(f, 100) is None
    # deleted-side-only file: nothing commentable
    deleted = parse_diff(
        "diff --git a/o.txt b/o.txt\n--- a/o.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )[0]
    assert snap_line(deleted, 1) is None
