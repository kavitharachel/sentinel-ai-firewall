"""Redaction helpers: text that may reach a terminal, log or evidence file passes through scrub()."""
from __future__ import annotations
import re

_PATTERNS = [
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"A2[0-9A-Za-z][A-Za-z0-9._-]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?i)(client_secret|secret|password|access_token)\"?\s*[:=]\s*\"?[^\s\",}]+"), r"\1=[REDACTED]"),
]
_extra: list[str] = []


def register_secret(value: str | None) -> None:
    """Make an in-memory secret value redactable wherever it might appear."""
    if value and len(value) >= 6 and value not in _extra:
        _extra.append(value)


def scrub(text: object, limit: int = 300) -> str:
    s = str(text)
    for v in _extra:
        s = s.replace(v, "[REDACTED]")
    for pat, rep in _PATTERNS:
        s = pat.sub(rep, s)
    return s[:limit]
