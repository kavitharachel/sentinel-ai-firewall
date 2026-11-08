"""Read-only loader for the FROZEN live-sandbox evidence written earlier by wallet-run. Files are opened 'rb' only;
nothing is written, refreshed or re-fetched. Everything returned is labelled LIVE-RECORDED."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import PROJECT_ROOT

LIVE = "LIVE-RECORDED"
DEFAULT_DIR = PROJECT_ROOT / "evidence" / "us"
_RUN_RX = re.compile(r"^wallet_run_[a-z0-9]+\.json$")


def mask(v: Optional[str]) -> str:
    v = str(v or "")
    return v if len(v) <= 8 else v[:3] + "***" + v[-4:]


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = path.read_bytes()
        return {"data": json.loads(raw.decode("utf-8")), "sha256": hashlib.sha256(raw).hexdigest()[:16], "bytes": len(raw)}
    except (OSError, ValueError):
        return None


def load_live(directory: Optional[Path] = None) -> Dict[str, Any]:
    d = Path(directory) if directory else DEFAULT_DIR
    out: Dict[str, Any] = {"label": LIVE, "available": False, "runs": [], "profile_check": None, "snapshot": {"orders": {}},
                           "note": "Frozen record of real PayPal SANDBOX responses captured earlier. This dashboard never refreshes, "
                                   "creates, captures or refunds anything; status shown is as of the recorded time."}
    if not d.is_dir():
        return out
    runs: List[Dict[str, Any]] = []
    for f in sorted(d.iterdir()):
        if not _RUN_RX.match(f.name):
            continue
        r = _read(f)
        if not r:
            continue
        x = r["data"]
        created = x.get("created_at")
        exp = None
        try:
            exp = (datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            pass
        captured = 1 if x.get("capture_id") else 0
        runs.append({"run_id": x.get("run_id"), "state": x.get("state"), "profile": x.get("profile"), "order_id_masked": mask(x.get("order_id")),
                     "order_status": x.get("order_status"), "amount": x.get("amount"), "currency": x.get("currency"),
                     "payee_masked": mask(x.get("payee_merchant_id")), "created_at": created, "approval_window_ends": exp,
                     "captures": captured, "abandoned": bool(x.get("abandoned")) or x.get("state") == "ABANDONED",
                     "evidence_sha256": r["sha256"], "evidence_bytes": r["bytes"], "_order_id": x.get("order_id"),
                     "_payee": x.get("payee_merchant_id")})
    pc = _read(d / "profile_check.json")
    if pc:
        p = pc["data"]
        out["profile_check"] = {"status": p.get("status"), "checked_at": p.get("checked_at"), "refund_scope": p.get("refund_scope"),
                                "transaction_search_http": p.get("txsearch_http"), "merchant_source": p.get("merchant_source"),
                                "expected_merchant_masked": mask(p.get("expected_merchant_id")), "evidence_sha256": pc["sha256"]}
    for r in runs:
        if r["_order_id"]:
            out["snapshot"]["orders"][r["_order_id"]] = {"status": str(r["order_status"]), "amount": str(r["amount"]), "currency": str(r["currency"]),
                                                       "payee_merchant_id": str(r["_payee"]), "captures": str(r["captures"])}
    out["runs"] = [{k: v for k, v in r.items() if not k.startswith("_")} for r in runs]
    out["available"] = bool(runs)
    out["_ids"] = {r["run_id"]: (r["_order_id"], r["_payee"]) for r in runs}
    return out


PUBLIC_SCHEMA = "sentinel.public-evidence/1"
WITHHELD = "WITHHELD"
_PUB_STR = re.compile(r"^[A-Za-z0-9_ :.\-]{0,40}$")


def _pub(v: Any, default: str = "") -> str:
    v = default if v is None else str(v)
    return v if _PUB_STR.match(v) else default


def load_public_summary(path: Optional[Path]) -> Dict[str, Any]:
    """Loads the identifier-free summary (see scripts/make_public_evidence.py). Only whitelisted, pattern-checked fields are
    read, so nothing else in the file can reach the UI. Order and merchant identifiers never exist in this data."""
    out: Dict[str, Any] = {"label": LIVE, "available": False, "runs": [], "profile_check": None, "snapshot": {"orders": {}}, "public_summary": True,
                           "note": "Identifier-free summary of a frozen record of real PayPal SANDBOX responses: one human-approved sandbox order creation (made earlier, offline from this site) and read-only status checks. "
                                   "Order and merchant identifiers are withheld. This page never refreshes, creates, captures or refunds anything."}
    r = _read(Path(path)) if path else None
    d = r["data"] if r else None
    if not isinstance(d, dict) or d.get("schema") != PUBLIC_SCHEMA:
        return out
    created = _pub(d.get("recorded_at"))
    exp = None
    try:
        exp = (datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    try:
        captures = int(d.get("captures", 0))
    except (TypeError, ValueError):
        captures = 0
    src = d.get("source_sha256") if isinstance(d.get("source_sha256"), dict) else {}
    status, amount, cur = _pub(d.get("order_status")), _pub(d.get("amount")), _pub(d.get("currency"))
    payee = WITHHELD if d.get("payee_matches_expected") is not False else "MISMATCH"
    out["runs"] = [{"run_id": "public-summary", "state": _pub(d.get("state")), "profile": None, "order_id_masked": WITHHELD, "order_status": status, "amount": amount,
                    "currency": cur, "payee_masked": payee, "created_at": created, "approval_window_ends": exp, "captures": captures, "abandoned": False,
                    "evidence_sha256": _pub(src.get("wallet_run")), "evidence_bytes": 0}]
    out["snapshot"]["orders"][WITHHELD] = {"status": status, "amount": amount, "currency": cur, "payee_merchant_id": payee, "captures": str(captures)}
    out["_ids"] = {"public-summary": (WITHHELD, WITHHELD)}
    pc = d.get("profile_check") if isinstance(d.get("profile_check"), dict) else None
    if pc:
        out["profile_check"] = {"status": _pub(pc.get("status")), "checked_at": _pub(pc.get("checked_at")), "refund_scope": _pub(pc.get("refund_scope")),
                                "transaction_search_http": _pub(pc.get("transaction_search_http")), "merchant_source": None, "expected_merchant_masked": WITHHELD,
                                "evidence_sha256": _pub(src.get("profile_check"))}
    out["available"] = True
    return out


def live_claims(live: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Illustrative claims authored by the dashboard (not by an agent), checked against the recorded evidence with the
    real verifier. One is deliberately false: the claim that the order was captured, when no buyer approval exists."""
    from ..eval.verifier import verify_claim
    out: List[Dict[str, Any]] = []
    for r in live.get("runs", []):
        oid, payee = live["_ids"].get(r["run_id"], (None, None))
        if not oid or r["abandoned"]:
            continue
        specs = [
            ("Order is awaiting buyer approval", {"type": "state", "section": "orders", "id": oid, "field": "status", "equals": "PAYER_ACTION_REQUIRED"}),
            ("Order amount is 2.00", {"type": "state", "section": "orders", "id": oid, "field": "amount", "equals": "2.00"}),
            ("Payee is the expected merchant" if oid != WITHHELD else "Payee matched the expected merchant (identifier withheld)", {"type": "state", "section": "orders", "id": oid, "field": "payee_merchant_id", "equals": payee}),
            ("Payment was captured", {"type": "state", "section": "orders", "id": oid, "field": "captures", "equals": "1"}),
        ]
        for text, c in specs:
            v, detail = verify_claim(c, live["snapshot"], [])
            actual = live["snapshot"]["orders"][oid].get(c["field"])
            out.append({"source": LIVE, "agent": "dashboard-illustration", "run_id": r["run_id"], "text": text, "claim": {**c, "id": mask(oid), "equals": mask(c["equals"]) if c["field"] == "payee_merchant_id" else c["equals"]},
                        "verdict": v, "detail": detail, "evidence": {"order": mask(oid), "field": c["field"], "actual": mask(actual) if c["field"] == "payee_merchant_id" else actual,
                                                                     "recorded_at": r["created_at"], "sha256": r["evidence_sha256"]}})
    return out
