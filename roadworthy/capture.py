"""Read-only lookup of ONE PayPal capture (GET /v2/payments/captures/{id}).

Safety properties:
  * GET only, through SandboxClient (sandbox host enforced, no write method exists).
  * The capture id is validated before it is placed in a URL path (no path injection).
  * The summary keeps status, amount, currency, age and booleans only. It keeps no payer,
    email, address or full id; display uses a masked id.
  * It never decides to refund anything. `refund_experiment_ok` only says whether this capture
    could be a candidate for a later, separately approved, hard-limited refund experiment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from .capabilities import ALLOWED_CURRENCIES, MAX_REFUND_AMOUNT
from .client import SandboxClient

REFUND_WINDOW_DAYS = 180          # PayPal: REFUND_NOT_ALLOWED_AFTER_180_DAYS
MIN_EXPERIMENT_AMOUNT = Decimal("1.00")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")
_REFUNDABLE_STATUSES = {"COMPLETED", "PARTIALLY_REFUNDED"}


def validate_capture_id(capture_id: str) -> str:
    if not isinstance(capture_id, str) or not _ID_RE.match(capture_id):
        raise ValueError("capture id must be 5-64 characters of letters, digits, '_' or '-'")
    return capture_id


def mask_id(capture_id: str) -> str:
    return "***" + capture_id[-4:] if len(capture_id) > 4 else "***"


def _dec(value: object) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass
class CaptureInfo:
    http_status: int
    masked_id: str
    found: bool = False
    capture_status: str = ""
    currency: str = ""
    gross_amount: Optional[Decimal] = None
    remaining_refundable: Optional[Decimal] = None      # None = cannot be determined from this lookup
    refund_link_present: bool = False
    age_days: Optional[int] = None
    experiment_amount: Optional[Decimal] = None          # largest amount a later experiment could use
    refund_experiment_ok: bool = False
    reasons: List[str] = field(default_factory=list)
    error: str = ""


def lookup_capture(client: SandboxClient, capture_id: str, now: Optional[datetime] = None) -> CaptureInfo:
    cid = validate_capture_id(capture_id)
    r = client.get(f"/v2/payments/captures/{cid}")
    info = CaptureInfo(http_status=r.status, masked_id=mask_id(cid))
    if not r.ok or not isinstance(r.body, dict):
        info.error = r.error_name() or f"HTTP {r.status}"
        if r.status == 404:
            info.reasons.append("not a capture id in this sandbox account (may be another transaction type)")
        else:
            info.reasons.append("lookup failed")
        return info

    b = r.body
    info.found = True
    info.capture_status = str(b.get("status", "")).upper()
    amt = b.get("amount") or {}
    info.currency = str(amt.get("currency_code", ""))
    info.gross_amount = _dec(amt.get("value"))

    breakdown = b.get("seller_receivable_breakdown") or {}
    refunded = _dec((breakdown.get("total_refunded_amount") or {}).get("value"))
    if info.capture_status == "COMPLETED":
        info.remaining_refundable = info.gross_amount
    elif info.capture_status == "REFUNDED":
        info.remaining_refundable = Decimal("0")
    elif info.capture_status == "PARTIALLY_REFUNDED" and refunded is not None and info.gross_amount is not None:
        info.remaining_refundable = info.gross_amount - refunded
    elif info.capture_status in ("PENDING", "DECLINED", "FAILED"):
        info.remaining_refundable = Decimal("0")
    # else: left None (unknown), never guessed

    info.refund_link_present = any(isinstance(l, dict) and l.get("rel") == "refund" for l in (b.get("links") or []))

    ct = b.get("create_time")
    if ct:
        try:
            created = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            info.age_days = ((now or datetime.now(timezone.utc)) - created).days
        except ValueError:
            pass

    # ---- candidate check for a LATER, separately approved experiment (nothing is executed here) ----
    why = info.reasons
    if info.capture_status not in _REFUNDABLE_STATUSES:
        why.append(f"capture status is {info.capture_status or 'unknown'}, not COMPLETED/PARTIALLY_REFUNDED")
    if info.currency not in ALLOWED_CURRENCIES:
        why.append(f"currency {info.currency or 'unknown'} not allowed by hard limits")
    if info.remaining_refundable is None:
        why.append("remaining refundable amount cannot be determined from this lookup")
    elif info.remaining_refundable < MIN_EXPERIMENT_AMOUNT:
        why.append("remaining refundable amount is below 1.00")
    if not info.refund_link_present:
        why.append("PayPal returned no refund link for this capture")
    if info.age_days is None:
        why.append("capture age unknown")
    elif info.age_days >= REFUND_WINDOW_DAYS:
        why.append("capture is outside the 180-day refund window")
    if not why:
        info.experiment_amount = min(info.remaining_refundable, Decimal(str(MAX_REFUND_AMOUNT)).quantize(Decimal("0.01")))
        info.refund_experiment_ok = True
    return info
