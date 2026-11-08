"""Structured claims. Agents must end their final message with a line:  CLAIMS: [ {...}, ... ]

Deterministic by design: free-text claim extraction by an LLM is a later, separately validated component."""
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any, Dict, List

VALID_TYPES = {"state", "exists", "created", "count", "no_change"}


@dataclass
class ParsedClaims:
    claims: List[Dict[str, Any]]
    malformed: bool
    error: str = ""


def parse_claims(final_message: str) -> ParsedClaims:
    marker = "CLAIMS:"
    lines = [ln for ln in (final_message or "").splitlines() if ln.strip().startswith(marker)]
    if not lines:
        return ParsedClaims([], False)                      # no claims made (not malformed)
    try:
        data = json.loads(lines[-1].strip()[len(marker):])
    except json.JSONDecodeError as e:
        return ParsedClaims([], True, f"invalid JSON: {e.msg}")
    if not isinstance(data, list):
        return ParsedClaims([], True, "claims must be a JSON list")
    good: List[Dict[str, Any]] = []
    for c in data:
        if not isinstance(c, dict) or c.get("type") not in VALID_TYPES:
            return ParsedClaims([], True, "unknown or invalid claim")
        good.append(c)
    return ParsedClaims(good, False)
