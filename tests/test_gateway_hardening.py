"""Regression tests for gateway hard limits: non-finite numbers, nested currencies and offsetting quantities must fail closed."""
import pytest

from roadworthy.capabilities import capability_of, ceiling_of
from roadworthy.levels import Level
from roadworthy.permissions.engine import Decision, Evidence, License
from roadworthy.permissions.gateway import ToolGateway


def _gw(earned_auto: bool) -> ToolGateway:
    lic = License("t", "t")
    if earned_auto:
        for tool in ("create_refund", "create_order"):
            cap = capability_of(tool)
            lic.decisions[cap] = Decision(cap, min(Level.AUTO, ceiling_of(cap)), Level.AUTO, ceiling_of(cap), [], Evidence())
    return ToolGateway(lic)


def _refund(amount, cur="USD", inner="USD"):
    a = {"capture_id": "CAP-1", "amount": amount if not isinstance(amount, str) else {"value": amount, "currency_code": inner}}
    if cur is not None:
        a["currency_code"] = cur
    return a


def _order(items, cur="USD"):
    return {"currency_code": cur, "items": items}


@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("bad", ["NaN", "nan", "-nan", "Infinity", "-Infinity", "0x10", "", "25.01", "0", "-5"])
def test_refund_with_non_finite_or_out_of_range_amount_is_denied(auto, bad):
    assert _gw(auto).authorize("create_refund", _refund(bad)).action == "DENY"


def test_refund_float_nan_is_denied():
    assert _gw(True).authorize("create_refund", {"capture_id": "CAP-1", "currency_code": "USD", "amount": float("nan")}).action == "DENY"


@pytest.mark.parametrize("auto", [False, True])
def test_nested_currency_swap_is_denied(auto):
    assert _gw(auto).authorize("create_refund", _refund("10.00", cur="USD", inner="EUR")).action == "DENY"
    assert _gw(auto).authorize("create_refund", _refund("10.00", cur=None, inner="EUR")).action == "DENY"
    item = [{"name": "a", "quantity": 1, "itemCost": {"value": "50", "currency_code": "EUR"}}]
    assert _gw(auto).authorize("create_order", _order(item)).action == "DENY"


def test_valid_in_limit_requests_are_still_handled_normally():
    assert _gw(False).authorize("create_refund", _refund("10.00")).action == "REQUIRE_APPROVAL"
    assert _gw(False).authorize("create_refund", _refund("25.00")).action == "REQUIRE_APPROVAL"
    ok = [{"name": "a", "quantity": 1, "itemCost": {"value": "99", "currency_code": "USD"}}]
    assert _gw(True).authorize("create_order", _order(ok)).action == "ALLOW"


@pytest.mark.parametrize("items", [
    [{"name": "a", "quantity": 1, "itemCost": {"value": "nan", "currency_code": "USD"}}],
    [{"name": "a", "quantity": 10, "itemCost": {"value": "50", "currency_code": "USD"}},
     {"name": "b", "quantity": -9, "itemCost": {"value": "50", "currency_code": "USD"}}],
    [{"name": "a", "quantity": 1, "itemCost": {"value": "-5", "currency_code": "USD"}}],
])
def test_undeterminable_or_offsetting_order_totals_are_never_auto_allowed(items):
    # fail closed: an order whose total cannot be trusted needs a human even for an agent that earned AUTO
    assert _gw(True).authorize("create_order", _order(items)).action != "ALLOW"


# ---- regression: null / missing currency must fail closed (hardening sprint) ----------------------------------------------------------
@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("args", [
    {"capture_id": "C", "currency_code": None, "amount": {"value": "5.00", "currency_code": "USD"}},              # explicit null, top level
    {"capture_id": "C", "currency_code": "USD", "amount": {"value": "5.00", "currency_code": None}},              # explicit null, nested
    {"capture_id": "C", "amount": {"value": "5.00"}},                                                              # no currency anywhere
    {"capture_id": "C", "currency_code": "USD", "amount": {"value": "5.00"}},                                      # dict amount without its own currency
    {"capture_id": "C", "amount": "5.00"},                                                                         # bare amount, no currency
    {"capture_id": "C", "currency_code": "", "amount": {"value": "5.00", "currency_code": "USD"}},
], ids=["null-top", "null-nested", "none", "dict-no-currency", "bare", "empty"])
def test_refund_with_missing_or_null_currency_is_denied(auto, args):
    d = _gw(auto).authorize("create_refund", args)
    assert d.action == "DENY" and "currency" in d.reason


@pytest.mark.parametrize("auto", [False, True])
def test_order_with_missing_or_null_currency_is_denied(auto):
    item = {"name": "x", "quantity": 1, "itemCost": {"value": "5.00"}}
    assert _gw(auto).authorize("create_order", {"items": [item]}).action == "DENY"
    assert _gw(auto).authorize("create_order", {"currency_code": None, "items": [item]}).action == "DENY"
    assert _gw(auto).authorize("create_order", {"currency_code": "USD", "items": [{**item, "itemCost": {"value": "5.00", "currency_code": None}}]}).action == "DENY"


@pytest.mark.parametrize("auto", [False, True])
def test_legitimate_calls_still_work(auto):
    ref = {"capture_id": "C", "currency_code": "USD", "amount": {"value": "5.00", "currency_code": "USD"}}
    assert _gw(auto).authorize("create_refund", ref).action == "REQUIRE_APPROVAL"
    assert _gw(auto).authorize("create_refund", {**ref, "amount": {"value": "25.00", "currency_code": "usd"}}).action == "REQUIRE_APPROVAL"
    od = {"currency_code": "USD", "items": [{"name": "x", "quantity": 2, "itemCost": {"value": "10.00", "currency_code": "USD"}}]}
    assert _gw(auto).authorize("create_order", od).action in ("ALLOW", "REQUIRE_APPROVAL")
    assert _gw(auto).authorize("get_order", {"id": "ORD-1"}).action in ("ALLOW", "REQUIRE_APPROVAL")      # read tools need no currency
