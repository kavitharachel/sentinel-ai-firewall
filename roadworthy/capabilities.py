"""Tool -> capability map and IMMUTABLE hard limits.

HARD_CEILING is source code, not data: test results, licenses and retests can only move a permission
DOWN from the ceiling, never above it. Changing a ceiling is a code change that needs human review.
"""
from __future__ import annotations

import json
from typing import Dict, List

from .config import MANIFEST_PATH
from .levels import Level

TOOL_CAPABILITY: Dict[str, str] = {}
for _cap, _tools in {
    "read.transactions": ["list_transactions", "get_merchant_insights"],
    "read.invoices": ["list_invoices", "get_invoice", "search_invoicing", "generate_invoice_number",
                      "generate_invoice_qr_code", "get_recurring_series"],
    "read.catalog": ["list_products", "show_product_details", "list_subscription_plans",
                     "show_subscription_plan_details", "show_subscription_details"],
    "read.orders_payments": ["get_order", "get_refund"],
    "read.disputes": ["list_disputes", "get_dispute"],
    "read.shipping": ["get_shipment_tracking"],
    "catalog.write": ["create_product", "update_product", "create_subscription_plan", "update_plan"],
    "invoice.draft": ["create_invoice", "update_invoicing", "delete_invoice", "create_recurring_series",
                      "delete_recurring_series", "create_conditional_rules_for_invoice",
                      "setup_invoice_auto_reminders", "update_invoice_auto_reminder", "cancel_invoice_auto_reminder"],
    "invoice.send": ["send_invoice", "send_invoice_reminder", "activate_recurring_series"],
    "invoice.cancel": ["cancel_sent_invoice", "cancel_recurring_series"],
    "invoice.record_external": ["record_payment_for_invoice", "record_refund_for_invoice"],
    "order.create": ["create_order", "create_subscription"],
    "order.capture": ["pay_order"],
    "refund.issue": ["create_refund"],
    "subscription.modify": ["update_subscription", "cancel_subscription"],
    "shipping.write": ["create_shipment_tracking"],
    "dispute.accept": ["accept_dispute_claim"],
}.items():
    for _t in _tools:
        TOOL_CAPABILITY[_t] = _cap

# Immutable ceilings. Reads and reversible drafts may be earned up to AUTO. Anything that sends external
# messages, captures/refunds money or cancels things can never be AUTO in this PoC. Two capabilities are
# permanently BLOCKED: accepting disputes (irreversible loss) and recording payments/refunds that did not happen
# in PayPal (could simulate payment).
HARD_CEILING: Dict[str, Level] = {
    "read.transactions": Level.AUTO, "read.invoices": Level.AUTO, "read.catalog": Level.AUTO,
    "read.orders_payments": Level.AUTO, "read.disputes": Level.AUTO, "read.shipping": Level.AUTO,
    "catalog.write": Level.AUTO, "invoice.draft": Level.AUTO, "order.create": Level.AUTO, "shipping.write": Level.AUTO,
    "invoice.send": Level.APPROVAL_REQUIRED, "invoice.cancel": Level.APPROVAL_REQUIRED,
    "order.capture": Level.APPROVAL_REQUIRED, "refund.issue": Level.APPROVAL_REQUIRED,
    "subscription.modify": Level.APPROVAL_REQUIRED,
    "invoice.record_external": Level.BLOCKED, "dispute.accept": Level.BLOCKED,
}

# Reads need no earning; every write capability starts at APPROVAL_REQUIRED until evidence supports more.
READ_CAPABILITIES = {c for c in HARD_CEILING if c.startswith("read.")}

# Parameter-level hard limits enforced at the gateway regardless of license (USD sandbox play money).
MAX_ORDER_TOTAL = 100.00
MAX_REFUND_AMOUNT = 25.00
ALLOWED_CURRENCIES = {"USD"}


def capability_of(tool: str) -> str | None:
    return TOOL_CAPABILITY.get(tool)


def ceiling_of(capability: str) -> Level:
    return HARD_CEILING.get(capability, Level.BLOCKED)    # unknown capability -> BLOCKED


def load_manifest_tools() -> List[str]:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        return [t["name"] for t in json.load(f)["tools"]]
