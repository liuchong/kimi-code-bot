"""Tests for kimibot.state (cross-commit state in GitHub comments)."""

from kimibot.state import (
    FINDING_MARK,
    META_MARK_RE,
    extract_fingerprints,
    fingerprint,
    parse_finding_marks,
    parse_meta,
    render_finding_mark,
    render_meta,
    unresolved_fingerprints,
)
from kimibot.types import ReviewMeta


def test_finding_mark_template():
    assert FINDING_MARK.format(fp="abc123") == "<!-- kimi-bot-finding:abc123 -->"
    assert render_finding_mark("abc123") == "<!-- kimi-bot-finding:abc123 -->"


def test_fingerprint_stable_under_line_drift_and_formatting():
    a = fingerprint("src/a.py", "x = compute()", "Null dereference")
    # Line drift does not matter (the fingerprint contains no line number);
    # whitespace/case changes do not matter either
    b = fingerprint("src/a.py", "  x   = compute()  ", "null DEREFERENCE")
    assert a == b
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)
    # Semantic code change -> fingerprint changes
    assert a != fingerprint("src/a.py", "x = compute_v2()", "Null dereference")
    # Different file -> fingerprint changes
    assert a != fingerprint("src/b.py", "x = compute()", "Null dereference")
    # Empty line content (file outside the diff) also works
    assert len(fingerprint("src/c.py", "", "t")) == 16


def test_extract_fingerprints():
    body = (
        "issue description\n<!-- kimi-bot-finding:0123456789abcdef -->\n\n---\n\n"
        "another one\n<!-- kimi-bot-finding:fedcba9876543210 -->"
    )
    assert extract_fingerprints(body) == ["0123456789abcdef", "fedcba9876543210"]
    # invalid content is not extracted
    assert extract_fingerprints("<!-- kimi-bot-finding:not-hex! -->") == []
    assert extract_fingerprints("no markers") == []
    assert extract_fingerprints("") == []
    # legacy hoverstare markers are NOT picked up
    assert extract_fingerprints("<!-- hoverstare-finding:0123456789abcdef -->") == []


def test_parse_finding_marks():
    comments = [
        {
            "id": 101,
            "path": "src/a.py",
            "line": 42,
            "body": "bug here\n<!-- kimi-bot-finding:0123456789abcdef -->",
        },
        {
            "id": 102,
            "path": "src/b.py",
            "original_line": 7,
            "body": "two issues <!-- kimi-bot-finding:aaaaaaaaaaaaaaaa -->"
            " and <!-- kimi-bot-finding:bbbbbbbbbbbbbbbb -->",
        },
        {"id": 103, "path": "src/c.py", "line": 1, "body": "plain comment, no mark"},
        {"id": 104, "path": "src/d.py", "line": 2, "body": None},
    ]
    marks = parse_finding_marks(comments)
    assert [m.fingerprint for m in marks] == [
        "0123456789abcdef",
        "aaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbb",
    ]
    assert marks[0].comment_id == 101
    assert marks[0].path == "src/a.py"
    assert marks[0].line == 42
    # falls back to original_line when line is absent
    assert marks[1].line == 7
    assert marks[1].thread_id is None
    assert marks[1].thread_resolved is False
    assert parse_finding_marks([]) == []


def test_meta_round_trip():
    meta = ReviewMeta(
        head_sha="abc123def", mode="incremental", files_reviewed=3,
        fingerprints=["0123456789abcdef", "fedcba9876543210"],
    )
    rendered = render_meta(meta)
    assert rendered == (
        "<!-- kimi-bot-meta mode=incremental head_sha=abc123def"
        " files_reviewed=3 finding: 0123456789abcdef fedcba9876543210 -->"
    )
    parsed = parse_meta("## Review\n\nsome prose\n" + rendered + "\nmore text")
    assert parsed == meta


def test_meta_round_trip_empty_fingerprints():
    meta = ReviewMeta(head_sha="deadbeef", mode="full", files_reviewed=0)
    parsed = parse_meta(render_meta(meta))
    assert parsed is not None
    assert parsed.head_sha == "deadbeef"
    assert parsed.mode == "full"
    assert parsed.files_reviewed == 0
    assert parsed.fingerprints == []


def test_parse_meta_none_when_absent():
    assert parse_meta("no metadata here") is None
    assert parse_meta("") is None
    assert parse_meta("<!-- kimi-bot-meta broken -->") is None
    assert META_MARK_RE.search("plain body") is None


def test_unresolved_fingerprints():
    threads = [
        {
            "id": "T1",
            "isResolved": False,
            "comments": {"nodes": [
                {"body": "open bug <!-- kimi-bot-finding:aaaaaaaaaaaaaaaa -->"},
            ]},
        },
        {
            "id": "T2",
            "isResolved": True,
            "comments": {"nodes": [
                {"body": "fixed bug <!-- kimi-bot-finding:bbbbbbbbbbbbbbbb -->"},
            ]},
        },
    ]
    marks = parse_finding_marks([
        {
            "id": 1, "path": "a.py", "line": 1,
            "body": "<!-- kimi-bot-finding:aaaaaaaaaaaaaaaa -->",
            "thread_id": "T1",
        },
        {
            "id": 2, "path": "b.py", "line": 2,
            "body": "<!-- kimi-bot-finding:bbbbbbbbbbbbbbbb -->",
            "thread_id": "T2",
        },
        {
            "id": 3, "path": "c.py", "line": 3,
            "body": "<!-- kimi-bot-finding:cccccccccccccccc -->",
            # no thread info -> conservatively treated as unresolved
        },
        {
            "id": 4, "path": "d.py", "line": 4,
            "body": "<!-- kimi-bot-finding:dddddddddddddddd -->",
            "thread_resolved": True,
        },
    ])
    unresolved = unresolved_fingerprints(threads, marks)
    assert unresolved == {"aaaaaaaaaaaaaaaa", "cccccccccccccccc"}
    # resolved thread + explicitly resolved mark are excluded
    assert "bbbbbbbbbbbbbbbb" not in unresolved
    assert "dddddddddddddddd" not in unresolved


def test_unresolved_fingerprints_empty():
    assert unresolved_fingerprints([], []) == set()
