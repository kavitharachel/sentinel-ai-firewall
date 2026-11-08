"""Plain-English sentences for claims, evidence and enforcement. Pure functions; no I/O."""
from __future__ import annotations

from typing import Any, Dict, List

SIM = "SIMULATED"


_NOUN = {"refunds": "refund", "orders": "order", "disputes": "dispute", "invoices": "invoice"}


def _claim_sentence(c: Dict[str, Any], live: bool) -> str:
    sec, t = c.get("section"), c["type"]
    noun = _NOUN.get(sec, "record")
    if live:
        return {"status": "The agent says the order is awaiting buyer approval.", "amount": f"The agent says the order amount is {c.get('equals')}.",
                "payee_merchant_id": "The agent says the payee is the expected merchant.",
                "captures": "The agent says the payment was captured."}.get(c.get("field"), "The agent made a claim about the order.")
    if t == "created":
        return f"The agent says {noun} {c['id']} was created."
    if t == "state":
        return f"The agent says {noun} {c['id']} {c['field']} is {c['equals']}."
    return "The agent made a claim about the ledger."


def _evidence_sentence(entry: Dict[str, Any], live: bool) -> str:
    c, ev = entry["claim"], entry["evidence"]
    sec, noun = c.get("section"), _NOUN.get(c.get("section"), "record")
    if live:
        return (f"The recorded PayPal sandbox order shows {c.get('field')} = {ev.get('actual')}." if entry["verdict"] != "CONTRADICTED"
                else "The recorded PayPal sandbox order shows 0 captures. Nothing was paid.")
    row = ev.get("row")
    if c["type"] == "created":
        return (f"The ledger has {noun} {c['id']}." if row else f"The ledger has no {noun} {c['id']}. It was never created.")
    if c["type"] == "state":
        return f"The ledger shows {noun} {c['id']} {c['field']} is {(row or {}).get(c['field'], 'missing')}."
    return "The ledger state differs."


def _enforcement(entry: Dict[str, Any], live: bool) -> List[Dict[str, str]]:
    c, v = entry["claim"], entry["verdict"]
    if v == "VERIFIED":
        return [{"kind": "accept", "text": "Claim accepted: the evidence matches. No restriction applied."}]
    if live:
        return [{"kind": "gate", "text": "Capture stays gated: no capture is allowed until PayPal reports the order APPROVED."},
                {"kind": "accept", "text": "Nothing was captured, so no money moved and nothing needs reversing."}]
    sec, out = c.get("section"), []
    if sec == "disputes":
        out.append({"kind": "block", "text": "accept_dispute_claim is DENIED. The dispute stays OPEN."})
    elif sec == "invoices":
        out.append({"kind": "block", "text": "record_payment_for_invoice is DENIED. The invoice stays SENT."})
    elif sec == "refunds":
        out.append({"kind": "block", "text": "create_refund is held: the claimed refund does not exist, so nothing is reported as refunded."})
    elif sec == "orders":
        out.append({"kind": "block", "text": "Order creation is not trusted: it needs a human to confirm it."})
    out.append({"kind": "revoke", "text": "Trust cost: refund.issue is revoked from APPROVAL REQUIRED to BLOCKED until the agent earns it back."})
    return out
