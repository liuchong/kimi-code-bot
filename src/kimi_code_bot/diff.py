"""Diff engine (ported from hoverstare src/diff.rs).

Parses unified diffs into structured data, answering two questions:
1. Review scope: which files and which lines were changed;
2. Commentable-line mapping: for each file, which line numbers on the RIGHT
   side can host inline comments.

Parsing is a fault-tolerant state machine: it never raises on malformed input
(input is treated as untrusted data).
"""

from __future__ import annotations

import fnmatch
import re

from .types import DiffFile, DiffHunk

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# File priority for large-diff truncation: source code > tests > docs > config > other
_CODE_EXTS = {
    "rs", "go", "py", "js", "jsx", "ts", "tsx", "java", "kt", "kts", "c", "h",
    "cc", "cpp", "cxx", "hpp", "cs", "rb", "php", "swift", "scala", "sh", "bash",
    "zsh", "sql", "vue", "svelte", "lua", "pl", "r", "dart", "ex", "exs", "erl",
    "hrl", "clj", "hs", "ml", "fs", "fsx", "vb", "groovy",
}
_DOC_EXTS = {"md", "markdown", "rst", "adoc", "txt"}
_CONFIG_EXTS = {"toml", "yaml", "yml", "json", "ini", "cfg", "xml", "properties", "lock", "gradle"}
_TEST_MARKERS = ("/tests/", "/test/", "test_", "_test.", ".test.", ".spec.", "/spec/", "/fixtures/")

# Max line distance for anchor degradation (snap_line)
SNAP_DISTANCE = 3


def _path_priority(path: str | None) -> int:
    if not path:
        return 5
    lower = path.lower()
    ext = lower.rsplit(".", 1)[-1] if "." in lower else ""
    looks_test = any(m in lower for m in _TEST_MARKERS)
    if ext in _CODE_EXTS:
        return 1 if looks_test else 0
    if looks_test:
        return 1
    if ext in _DOC_EXTS:
        return 2
    if ext in _CONFIG_EXTS:
        return 3
    return 4


def parse_diff(text: str) -> list[DiffFile]:
    """Fault-tolerant parse of a unified diff. Unrecognized lines are skipped."""
    files: list[DiffFile] = []
    current: DiffFile | None = None
    in_hunk = False

    for line in text.splitlines():
        # New file starts -> back to file-header state
        if line.startswith("diff --git "):
            if current is not None:
                files.append(current)
            current = DiffFile(path="")
            in_hunk = False
            continue

        if current is None:
            continue  # ignore anything before the first diff --git

        m = _HUNK_RE.match(line)
        if m:
            in_hunk = True
            current.hunks.append(
                DiffHunk(
                    old_start=int(m.group(1)),
                    old_lines=int(m.group(2)) if m.group(2) is not None else 1,
                    new_start=int(m.group(3)),
                    new_lines=int(m.group(4)) if m.group(4) is not None else 1,
                )
            )
            continue

        if not in_hunk:
            # File-header state: only here are +++ / --- / rename / binary
            # markers recognized; content lines inside a hunk body that start
            # with these strings are not misread.
            if line.startswith("+++ b/"):
                current.path = line[len("+++ b/"):]
            elif line.startswith("+++ /dev/null"):
                current.status = "deleted"
            elif line.startswith("--- a/"):
                # Deleted files have no +++ side, so path comes from the old
                # side; on rename it is overwritten by +++
                if not current.path:
                    current.path = line[len("--- a/"):]
            elif line.startswith("--- /dev/null"):
                current.status = "added"
            elif line.startswith("rename from "):
                current.old_path = line[len("rename from "):]
                current.status = "renamed"
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                current.is_binary = True
            continue

        # Hunk body: keep raw lines (each starts with ' ', '+', '-' or '\');
        # anything else (empty lines, garbage) is skipped.
        if line[:1] in (" ", "+", "-", "\\"):
            current.hunks[-1].lines.append(line)

    if current is not None:
        files.append(current)
    # Drop file sections without a path (abnormal input)
    return [f for f in files if f.path]


def _match_glob(path: str, pattern: str) -> bool:
    """fnmatch-style glob with common gitignore conveniences."""
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
        return True
    # A pattern without a slash also matches against the basename
    if "/" not in pattern and fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern):
        return True
    return False


def filter_ignored(
    files: list[DiffFile], ignore: list[str]
) -> tuple[list[DiffFile], list[DiffFile]]:
    """Split files into (kept, ignored) by fnmatch-style glob patterns."""
    kept: list[DiffFile] = []
    ignored: list[DiffFile] = []
    for f in files:
        if any(_match_glob(f.path, pat) for pat in ignore):
            ignored.append(f)
        else:
            kept.append(f)
    return kept, ignored


def render_diff(files: list[DiffFile]) -> str:
    """Re-render parsed files as unified diff text (for prompt injection)."""
    out: list[str] = []
    for f in files:
        old = f.old_path or f.path
        out.append(f"diff --git a/{old} b/{f.path}\n")
        if f.status == "renamed" and f.old_path:
            out.append(f"rename from {f.old_path}\nrename to {f.path}\n")
        if f.is_binary:
            out.append(f"Binary files a/{old} and b/{f.path} differ\n")
            continue
        old_side = "/dev/null" if f.status == "added" else f"a/{old}"
        new_side = "/dev/null" if f.status == "deleted" else f"b/{f.path}"
        out.append(f"--- {old_side}\n+++ {new_side}\n")
        for h in f.hunks:
            out.append(f"@@ -{h.old_start},{h.old_lines} +{h.new_start},{h.new_lines} @@\n")
            for raw in h.lines:
                out.append(raw)
                if not raw.endswith("\n"):
                    out.append("\n")
    return "".join(out)


def truncate_files(files: list[DiffFile], max_kb: int) -> tuple[list[DiffFile], bool]:
    """Truncate an oversized diff at whole-file granularity (never cuts a file
    in half). Files are kept by priority (source > tests > docs > config >
    other, ties by original order); the first file is always kept, even over
    budget (floor guarantee). Returns (kept, truncated)."""
    budget = max_kb * 1024
    sizes = [len(render_diff([f])) for f in files]
    if sum(sizes) <= budget:
        return list(files), False

    order = sorted(range(len(files)), key=lambda i: (_path_priority(files[i].path), i))
    kept_idx: set[int] = set()
    used = 0
    for i in order:
        if used == 0 or used + sizes[i] <= budget:
            kept_idx.add(i)
            used += sizes[i]
    kept = [f for i, f in enumerate(files) if i in kept_idx]
    return kept, len(kept) < len(files)


def snap_line(f: DiffFile, line: int) -> int | None:
    """Anchor degradation: if `line` is commentable, return it as-is; otherwise
    snap to the nearest commentable line within SNAP_DISTANCE; else None."""
    commentable = f.commentable_lines
    if line in commentable:
        return line
    best: int | None = None
    for cand in commentable:
        dist = abs(cand - line)
        if dist <= SNAP_DISTANCE and (best is None or (dist, cand) < (abs(best - line), best)):
            best = cand
    return best
