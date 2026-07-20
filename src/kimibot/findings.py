"""Model output parsing and normalization (ported from hoverstare src/findings.rs).

Model output is untrusted: three-level JSON extraction + schema validation +
field normalization must happen before it is allowed into the system.

Output contract: {"findings": [...], "cross_cutting": [...]} where each item has
path/line/end_line/severity/title/description/suggestion/confidence.

Empty output or pure prose yields None — the caller is expected to retry.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema

from .types import Finding, Severity

FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        # Item-level problems (missing fields / wrong types) are handled one by
        # one during normalization; the schema only governs the top-level
        # structure, so structurally wrong output (e.g. findings written as an
        # object, or a completely unrelated structure) is rejected here instead
        # of silently normalizing into "0 findings".
        "findings": {"type": "array", "items": {"type": "object"}},
        "cross_cutting": {"type": "array", "items": {"type": "object"}},
        "resolved_finding_ids": {"type": "array", "items": {"type": "string"}},
    },
}

_SEVERITY_ALIASES: dict[str, Severity] = {
    "critical": "critical",
    "crit": "critical",
    "blocker": "critical",
    "high": "high",
    "major": "high",
    "error": "high",
    "medium": "medium",
    "med": "medium",
    "moderate": "medium",
    "warning": "medium",
    "warn": "medium",
    "low": "low",
    "minor": "low",
    "info": "low",
    "informational": "low",
    "nit": "low",
}


def _extract_fence(text: str) -> str | None:
    for marker in ("```json", "```"):
        start = text.find(marker)
        if start != -1:
            rest = text[start + len(marker):]
            end = rest.find("```")
            if end != -1:
                return rest[:end].strip()
    return None


def _extract_braces(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return None


def extract_json(text: str) -> dict | None:
    """Three-level JSON extraction: direct parse -> ```json fence -> first '{'
    to last '}' brace pairing. Returns the top-level object, or None."""
    for candidate in (text, _extract_fence(text), _extract_braces(text)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _parse_severity(value: Any) -> Severity:
    if isinstance(value, str):
        return _SEVERITY_ALIASES.get(value.strip().lower(), "medium")
    return "medium"


def _parse_int(value: Any) -> int | None:
    """Tolerate integers / floats / numeric strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            try:
                return int(float(value.strip()))
            except ValueError:
                return None
    return None


def _parse_confidence(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0
    if isinstance(value, (int, float)):
        conf = float(value)
    elif isinstance(value, str):
        try:
            conf = float(value.strip())
        except ValueError:
            return 1.0
    else:
        return 1.0
    if conf != conf:  # NaN
        return 1.0
    return max(0.0, min(1.0, conf))


def _normalize_finding(obj: dict[str, Any]) -> Finding | None:
    path = obj.get("path", obj.get("file"))
    if not isinstance(path, str) or not path.strip():
        return None
    line = _parse_int(obj.get("line"))
    if line is None or line < 0:
        return None

    title = obj.get("title")
    title = title.strip() if isinstance(title, str) else ""
    description = obj.get("description")
    suggestion = obj.get("suggestion")
    suggestion = suggestion.strip() if isinstance(suggestion, str) else ""

    end_line = _parse_int(obj.get("end_line"))
    if end_line is not None and end_line < line:
        end_line = None

    return Finding(
        path=path.strip(),
        line=line,
        end_line=end_line,
        severity=_parse_severity(obj.get("severity")),
        title=title or "(untitled)",
        description=description if isinstance(description, str) else "",
        suggestion=suggestion or None,
        confidence=_parse_confidence(obj.get("confidence")),
    )


def parse_findings(text: str) -> tuple[list[Finding], list[Finding], set[str]] | None:
    """Extract + validate + normalize model output.

    Returns (findings, cross_cutting, resolved_finding_ids). Returns None on
    empty output, pure prose, unextractable JSON, or schema violations — the
    caller retries.
    """
    if not text or not text.strip():
        return None
    data = extract_json(text.strip())
    if data is None:
        return None

    # Tolerant reshaping: unify the bugs -> findings key; drop non-object
    # entries up front (schema validation focuses on top-level structure;
    # item-level problems are handled by normalization).
    if "findings" not in data and "bugs" in data:
        data["findings"] = data.pop("bugs")
    for key in ("findings", "cross_cutting"):
        if isinstance(data.get(key), list):
            data[key] = [v for v in data[key] if isinstance(v, dict)]
    if isinstance(data.get("resolved_finding_ids"), list):
        data["resolved_finding_ids"] = [
            v for v in data["resolved_finding_ids"] if isinstance(v, str)
        ]

    try:
        jsonschema.validate(data, FINDINGS_SCHEMA)
    except jsonschema.ValidationError:
        return None

    def collect(key: str) -> list[Finding]:
        items = data.get(key) or []
        return [f for f in (_normalize_finding(v) for v in items) if f is not None]

    raw_ids = data.get("resolved_finding_ids") or data.get("resolved_ids") or []
    resolved_ids = {v.strip() for v in raw_ids if isinstance(v, str) and v.strip()}

    return collect("findings"), collect("cross_cutting"), resolved_ids
