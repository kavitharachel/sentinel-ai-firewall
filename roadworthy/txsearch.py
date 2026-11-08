"""Transaction Search reproduction (read-only). Prints/returns aggregates only: no IDs, payers or amounts."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from .client import SandboxClient

MAX_WINDOW_DAYS = 31


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


@dataclass
class TxSummary:
    status_code: int
    window_start: str
    window_end: str
    count: int = 0
    pages_read: int = 0
    statuses: Dict[str, int] = field(default_factory=dict)
    currencies: List[str] = field(default_factory=list)
    error: str = ""


def search_transactions(client: SandboxClient, end: datetime | None = None, days: int = 30,
                        page_size: int = 100, max_pages: int = 5) -> TxSummary:
    if not 1 <= days <= MAX_WINDOW_DAYS:
        raise ValueError(f"days must be 1..{MAX_WINDOW_DAYS} (PayPal limit)")
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    out = TxSummary(0, _fmt(start), _fmt(end))
    statuses: Counter = Counter()
    currencies = set()
    page = 1
    while page <= max_pages:
        r = client.get("/v1/reporting/transactions", {"start_date": _fmt(start), "end_date": _fmt(end),
                                                       "fields": "transaction_info", "page_size": page_size, "page": page})
        out.status_code = r.status
        if not r.ok:
            out.error = r.error_name() or f"HTTP {r.status}"
            return out
        details = (r.body or {}).get("transaction_details", []) or []
        out.pages_read += 1
        out.count += len(details)
        for d in details:
            ti = d.get("transaction_info", {}) or {}
            statuses[str(ti.get("transaction_status", "?"))] += 1
            cur = (ti.get("transaction_amount") or {}).get("currency_code")
            if cur:
                currencies.add(cur)
        if page >= int((r.body or {}).get("total_pages", 1) or 1):
            break
        page += 1
    out.statuses = dict(sorted(statuses.items()))
    out.currencies = sorted(currencies)
    return out
