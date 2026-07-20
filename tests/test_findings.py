"""Tests for kimi_code_bot.findings (three-level extraction + normalization)."""

from kimi_code_bot.findings import FINDINGS_SCHEMA, extract_json, parse_findings


def test_schema_is_dict():
    assert FINDINGS_SCHEMA["type"] == "object"
    assert "findings" in FINDINGS_SCHEMA["required"]


def test_extract_json_direct():
    assert extract_json('{"findings": []}') == {"findings": []}
    # top-level non-object is not acceptable
    assert extract_json('[1, 2, 3]') is None
    assert extract_json("42") is None


def test_extract_json_fenced():
    text = 'Analysis result:\n```json\n{"findings": [], "cross_cutting": []}\n```\nDone.'
    assert extract_json(text) == {"findings": [], "cross_cutting": []}
    # bare fence works too
    text2 = '```\n{"findings": []}\n```'
    assert extract_json(text2) == {"findings": []}


def test_extract_json_braces():
    text = 'I found nothing. {"findings": [], "summary": "clean"} — done'
    assert extract_json(text) == {"findings": [], "summary": "clean"}
    assert extract_json("no json here at all") is None
    assert extract_json("") is None
    assert extract_json("{ not valid json }") is None


def test_parse_clean_json():
    raw = (
        '{"findings": [{"path": "a.py", "line": 3, "severity": "high",'
        ' "title": "t", "description": "d", "suggestion": "fix",'
        ' "confidence": 0.9, "end_line": 5}],'
        ' "cross_cutting": [{"path": "b.py", "line": 1, "severity": "low",'
        ' "title": "x", "description": "y"}]}'
    )
    result = parse_findings(raw)
    assert result is not None
    findings, cross, resolved_ids = result
    assert resolved_ids == set()
    assert len(findings) == 1
    f = findings[0]
    assert (f.path, f.line, f.end_line, f.severity, f.title) == ("a.py", 3, 5, "high", "t")
    assert f.suggestion == "fix"
    assert f.confidence == 0.9
    assert len(cross) == 1
    assert cross[0].path == "b.py"


def test_parse_tolerates_messy_fields():
    raw = """{"findings": [
        "garbage-entry",
        {"path": "a.py", "line": "42", "title": "t1"},
        {"path": "b.py", "line": 7.9, "severity": "HIGH", "title": ""},
        {"path": "c.py", "line": 1, "severity": "URGENT", "confidence": "0.5"},
        {"line": 1, "title": "no path"},
        {"path": "d.py", "line": "not-a-number"},
        {"path": "e.py", "line": 10, "end_line": 3}
    ], "cross_cutting": []}"""
    result = parse_findings(raw)
    assert result is not None
    findings, cross, _ids = result
    assert cross == []
    assert [f.path for f in findings] == ["a.py", "b.py", "c.py", "e.py"]
    a, b, c, e = findings
    assert a.line == 42
    assert a.severity == "medium"  # default
    assert a.confidence == 1.0  # default
    assert b.line == 7  # float truncated
    assert b.severity == "high"  # case-insensitive
    assert b.title == "(untitled)"  # empty title defaulted
    assert c.severity == "medium"  # unknown value downgraded
    assert c.confidence == 0.5  # numeric string tolerated
    assert e.end_line is None  # end_line < line dropped


def test_parse_severity_aliases():
    raw = (
        '{"findings": ['
        '{"path": "a", "line": 1, "severity": "Critical"},'
        '{"path": "a", "line": 1, "severity": "minor"},'
        '{"path": "a", "line": 1, "severity": "warning"},'
        '{"path": "a", "line": 1, "severity": 5}'
        "]}"
    )
    findings, _, _ids = parse_findings(raw)
    assert [f.severity for f in findings] == ["critical", "low", "medium", "medium"]


def test_parse_accepts_file_and_bugs_aliases():
    raw = '{"bugs": [{"file": "a.py", "line": 1, "title": "t"}]}'
    findings, cross, _ids = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].path == "a.py"
    assert cross == []


def test_parse_empty_findings_is_ok():
    findings, cross, _ids = parse_findings('{"findings": [], "cross_cutting": []}')
    assert findings == [] and cross == []


def test_parse_rejects_empty_and_prose():
    assert parse_findings("") is None
    assert parse_findings("   \n  ") is None
    assert parse_findings("The code looks fine, no issues found.") is None


def test_parse_rejects_structurally_wrong_output():
    # findings is an object instead of an array -> schema rejects (goes to
    # retry instead of silently yielding 0 findings)
    assert parse_findings('{"findings": {"path": "a.py"}, "cross_cutting": []}') is None
    # completely unrelated structure: normalization would silently produce 0
    # findings, so it is intercepted at the schema layer
    assert parse_findings('{"result": {"text": "no bugs"}}') is None
    # top-level array
    assert parse_findings('[{"path": "a.py", "line": 1}]') is None


def test_parse_fenced_with_surrounding_prose():
    raw = (
        "Here is my review.\n"
        "```json\n"
        '{"findings": [{"path": "x.py", "line": 2, "severity": "low",'
        ' "title": "t", "description": "d"}]}\n'
        "```\nHope this helps!"
    )
    findings, _, _ids = parse_findings(raw)
    assert len(findings) == 1
    assert findings[0].line == 2


def test_parse_resolved_finding_ids():
    raw = (
        '{"findings": [], "resolved_finding_ids": ["abc123", " def456 ", 42, ""]}'
    )
    findings, cross, ids = parse_findings(raw)
    assert findings == [] and cross == []
    assert ids == {"abc123", "def456"}
