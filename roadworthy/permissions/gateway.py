"""Enforcement at the PayPal tool boundary. Fail-closed: anything unknown, ambiguous or unapproved is denied.

Decision order: environment -> known tool -> capability ceiling -> parameter hard limits -> license level -> approval.
Hard limits come first and no license can override them."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .engine import License
from ..capabilities import (ALLOWED_CURRENCIES, MAX_ORDER_TOTAL, MAX_REFUND_AMOUNT, capability_of, ceiling_of,
                            load_manifest_tools)
from ..levels import Level


@dataclass
class GatewayDecision:
    tool: str
    capability: str
    action: str            # ALLOW | REQUIRE_APPROVAL | DENY
    level: str             # AUTO | APPROVAL_REQUIRED | BLOCKED (the license level that applied)
    reason: str


def _money(v: Any) -> Optional[float]:
    """Accept '12.50', 12.5, or {'value': '12.50', ...}; return None when not determinable."""
    if isinstance(v, dict):
        v = v.get("value", v.get("amount"))
    try:
        f = float(str(v))
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None      # NaN/Infinity compare False against every limit: never determinable


def _currencies(v: Any, depth: int = 0) -> List[Any]:
    """Every currency code stated anywhere in the arguments (bounded recursion; fail-closed on non-string codes)."""
    out: List[Any] = []
    if depth > 6:
        return out
    if isinstance(v, dict):
        for k, x in v.items():
            if k in ("currency_code", "currencyCode", "currency"):
                out.append("<null>" if x is None else x if isinstance(x, str) else repr(x))      # an explicit null is ambiguous: it must not pass as "not stated"
            else:
                out.extend(_currencies(x, depth + 1))
    elif isinstance(v, list):
        for x in v[:50]:
            out.extend(_currencies(x, depth + 1))
    return out


def _order_total(args: Dict[str, Any]) -> Optional[float]:
    total = 0.0
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return None
    for it in items:
        if not isinstance(it, dict):
            return None
        qty = _money(it.get("quantity", 1))
        unit = None
        for k in ("itemCost", "unitAmount", "unit_amount", "price", "amount"):
            if k in it:
                unit = _money(it[k])
                break
        if qty is None or unit is None or qty <= 0 or unit < 0:      # negative quantities/prices could offset a large total
            return None
        total += qty * unit
    for k in ("shippingCost", "discount"):
        extra = _money(args.get(k)) if args.get(k) is not None else 0.0
        if extra is None:
            return None
        total += extra if k == "shippingCost" else -extra
    return total


class ToolGateway:
    def __init__(self, license: License, approver: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
                 environment: str = "SANDBOX"):
        self.license = license
        self.approver = approver            # None -> approvals are denied (fail closed)
        self.environment = environment
        self._known = set(load_manifest_tools())
        self.audit: List[Dict[str, Any]] = []

    # -- pure decision (no side effects) --------------------------------------------------------------
    def authorize(self, tool: str, args: Dict[str, Any]) -> GatewayDecision:
        def d(action: str, level: str, reason: str, cap: str = "?") -> GatewayDecision:
            return GatewayDecision(tool, cap, action, level, reason)
        if self.environment != "SANDBOX":
            return d("DENY", "BLOCKED", "hard limit: only SANDBOX is permitted")
        if tool not in self._known:
            return d("DENY", "BLOCKED", "unknown tool (not in the official toolkit manifest)")
        cap = capability_of(tool) or ""
        if not cap:
            return d("DENY", "BLOCKED", "tool has no capability mapping")
        if ceiling_of(cap) == Level.BLOCKED:
            return d("DENY", "BLOCKED", f"hard limit: capability {cap} is permanently blocked", cap)
        for cur in _currencies(args):                       # top-level AND nested (amount, itemCost, shipping, ...)
            if str(cur).upper() not in ALLOWED_CURRENCIES:
                return d("DENY", "BLOCKED", f"hard limit: currency {cur} not allowed", cap)
        if tool in ("create_refund", "create_order") and not _currencies(args):
            return d("DENY", "BLOCKED", "hard limit: a USD currency must be stated (a missing currency is ambiguous)", cap)
        forced_approval = ""
        if tool == "create_refund":
            a0 = args.get("amount")
            if isinstance(a0, dict) and not any(k in a0 for k in ("currency_code", "currencyCode", "currency")):
                return d("DENY", "BLOCKED", "hard limit: the refund amount must state its own currency", cap)
            amt = _money(args.get("amount"))
            if amt is None:
                return d("DENY", "BLOCKED", "hard limit: refund must state an explicit amount (omitted = full refund)", cap)
            if amt <= 0 or amt > MAX_REFUND_AMOUNT:
                return d("DENY", "BLOCKED", f"hard limit: refund amount outside 0..{MAX_REFUND_AMOUNT}", cap)
        if tool == "create_order":
            tot = _order_total(args)
            if tot is not None and (tot <= 0 or tot > MAX_ORDER_TOTAL):
                return d("DENY", "BLOCKED", f"hard limit: order total outside 0..{MAX_ORDER_TOTAL}", cap)
            if tot is None:
                forced_approval = "order total could not be determined"
        level = min(self.license.level_for(cap), ceiling_of(cap))
        if level == Level.BLOCKED:
            return d("DENY", "BLOCKED", "license: capability blocked", cap)
        if level == Level.APPROVAL_REQUIRED or forced_approval:
            return d("REQUIRE_APPROVAL", "APPROVAL_REQUIRED", forced_approval or "license: human approval required", cap)
        return d("ALLOW", "AUTO", "license: earned autonomy", cap)

    # -- enforcement ----------------------------------------------------------------------------------
    def call(self, tool: str, args: Dict[str, Any], executor: Callable[[str, Dict[str, Any]], Any]) -> Dict[str, Any]:
        dec = self.authorize(tool, args)
        approved: Optional[bool] = None
        if dec.action == "REQUIRE_APPROVAL":
            approved = bool(self.approver(tool, args, dec.reason)) if self.approver else False
        executed = dec.action == "ALLOW" or approved is True
        self.audit.append({"tool": tool, "capability": dec.capability, "action": dec.action, "level": dec.level,
                           "reason": dec.reason, "approved": approved, "executed": executed,
                           "args_digest": hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:12]})
        if not executed:
            return {"executed": False, "decision": dec, "approved": approved, "result": None}
        return {"executed": True, "decision": dec, "approved": approved, "result": executor(tool, args)}
