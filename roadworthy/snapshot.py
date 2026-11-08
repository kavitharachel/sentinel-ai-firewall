"""Read-only PayPal state snapshots. This is the ground truth the grader compares agent claims against."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable

from .client import SandboxClient

Snapshot = Dict[str, Dict[str, Dict[str, Any]]]
SECTIONS = ("invoices", "products", "plans", "orders", "refunds")


def digest(snap: Snapshot) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def counts(snap: Snapshot) -> Dict[str, int]:
    return {k: len(v) for k, v in snap.items()}


def _amt(a: Any) -> tuple:
    a = a or {}
    return a.get("value"), a.get("currency_code")


def _paged(client: SandboxClient, path: str, key: str, params: dict, max_pages: int = 5):
    for page in range(1, max_pages + 1):
        r = client.get(path, {**params, "page": page, "page_size": 20, "total_required": "true"})
        if not r.ok:
            yield None, r
            return
        items = (r.body or {}).get(key, []) or []
        yield items, r
        if page >= int((r.body or {}).get("total_pages", 1) or 1) or not items:
            return


def take_snapshot(client: SandboxClient, order_ids: Iterable[str] = (), refund_ids: Iterable[str] = ()) -> Snapshot:
    """Collect invoices, catalog products, subscription plans, plus explicitly named orders and refunds."""
    snap: Snapshot = {s: {} for s in SECTIONS}
    for items, _ in _paged(client, "/v2/invoicing/invoices", "items", {}):
        if items is None:
            snap["invoices"] = {"__unavailable__": {"reason": "list failed"}}
            break
        for it in items:
            v, c = _amt(it.get("amount"))
            snap["invoices"][it["id"]] = {"status": (it.get("status") or "?"), "amount": v, "currency": c}
    for items, _ in _paged(client, "/v1/catalogs/products", "products", {}):
        if items is None:
            snap["products"] = {"__unavailable__": {"reason": "list failed"}}
            break
        for it in items:
            snap["products"][it["id"]] = {"name": it.get("name")}
    for items, _ in _paged(client, "/v1/billing/plans", "plans", {}):
        if items is None:
            snap["plans"] = {"__unavailable__": {"reason": "list failed"}}
            break
        for it in items:
            snap["plans"][it["id"]] = {"status": it.get("status"), "product_id": it.get("product_id")}
    for oid in order_ids:
        r = client.get(f"/v2/checkout/orders/{oid}")
        if r.ok:
            pu = ((r.body or {}).get("purchase_units") or [{}])[0]
            v, c = _amt(pu.get("amount"))
            snap["orders"][oid] = {"status": r.body.get("status"), "amount": v, "currency": c}
    for rid in refund_ids:
        r = client.get(f"/v2/payments/refunds/{rid}")
        if r.ok:
            v, c = _amt(r.body.get("amount"))
            snap["refunds"][rid] = {"status": r.body.get("status"), "amount": v, "currency": c}
    return snap
