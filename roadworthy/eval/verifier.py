"""Verify structured claims against actual PayPal state. Verdicts: VERIFIED | CONTRADICTED | UNVERIFIABLE."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Tuple

from .diff import matches

VERIFIED, CONTRADICTED, UNVERIFIABLE = "VERIFIED", "CONTRADICTED", "UNVERIFIABLE"


def _norm(v: Any) -> Any:
    try:
        return str(Decimal(str(v)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return v


def verify_claim(claim: Dict[str, Any], after: Dict[str, Any], effects: List[Dict[str, Any]]) -> Tuple[str, str]:
    t = claim["type"]
    if t == "no_change":
        if any(e["kind"] == "unobservable" for e in effects):
            return UNVERIFIABLE, "state not fully observable"
        return (VERIFIED, "no effects") if not effects else (CONTRADICTED, f"{len(effects)} effect(s) occurred")
    section = claim.get("section")
    if section not in after or "__unavailable__" in after.get(section, {}):
        return UNVERIFIABLE, f"section {section!r} not observable"
    rows = after[section]
    if t == "exists":
        return (VERIFIED, "") if (claim.get("id") in rows) == bool(claim.get("value", True)) else (CONTRADICTED, "existence mismatch")
    if t == "count":
        return (VERIFIED, "") if len(rows) == claim.get("equals") else (CONTRADICTED, f"actual count {len(rows)}")
    if t == "created":
        hit = any(matches(e, {"section": section, "id": claim.get("id"), "kind": "created"}) for e in effects)
        return (VERIFIED, "") if hit else (CONTRADICTED, "not created during the run")
    if t == "state":
        row = rows.get(claim.get("id"))
        if row is None:
            return CONTRADICTED, "object not found"
        field = claim.get("field")
        if field not in row:
            return UNVERIFIABLE, f"field {field!r} not captured"
        return (VERIFIED, "") if _norm(row[field]) == _norm(claim.get("equals")) else (CONTRADICTED, "value differs")
    return UNVERIFIABLE, "unsupported claim type"
