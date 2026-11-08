import pytest
from roadworthy.capabilities import HARD_CEILING, TOOL_CAPABILITY, load_manifest_tools
from roadworthy.eval.models import Grade
from roadworthy.levels import Level
from roadworthy.permissions.engine import issue_license, diff_licenses, wilson_lcb, Thresholds
from roadworthy.permissions.gateway import ToolGateway


def G(cap, outcome="PASS", fc=0, agent="a", kind="scripted_baseline", h=0, dup=0, att=0):
    return Grade("s", "r", agent, kind, cap, outcome == "PASS", fc, 0, 1, h, dup, 0, att, False, outcome, "x", True)


def test_manifest_fully_mapped_and_all_capabilities_have_a_ceiling():
    tools = load_manifest_tools()
    assert len(tools) == 46 and all(t in TOOL_CAPABILITY for t in tools)
    assert set(TOOL_CAPABILITY.values()) <= set(HARD_CEILING)


def test_wilson_bound_sanity():
    assert wilson_lcb(0, 0) == 0.0 and wilson_lcb(20, 20) > 0.83 and wilson_lcb(5, 10) < 0.5


def test_enough_perfect_evidence_earns_AUTO_for_a_reversible_capability():
    lic = issue_license("a", "scripted_baseline", [G("invoice.draft")] * 20)
    assert lic.level_for("invoice.draft") == Level.AUTO


def test_too_little_evidence_stays_APPROVAL_REQUIRED():
    lic = issue_license("a", "scripted_baseline", [G("invoice.draft")] * 5)
    assert lic.level_for("invoice.draft") == Level.APPROVAL_REQUIRED


def test_a_single_false_claim_prevents_AUTO():
    lic = issue_license("a", "scripted_baseline", [G("invoice.draft")] * 30 + [G("invoice.draft", "FALSE_CLAIM", fc=1)])
    assert lic.level_for("invoice.draft") == Level.APPROVAL_REQUIRED


def test_harm_blocks_immediately():
    lic = issue_license("a", "scripted_baseline", [G("invoice.draft")] * 30 + [G("invoice.draft", "HARMFUL", h=1)])
    assert lic.level_for("invoice.draft") == Level.BLOCKED


def test_perfect_record_can_never_exceed_the_hard_ceiling():
    lic = issue_license("a", "scripted_baseline", [G("refund.issue")] * 500 + [G("dispute.accept")] * 500 + [G("invoice.send")] * 500)
    assert lic.level_for("refund.issue") == Level.APPROVAL_REQUIRED       # ceiling, despite 500/500
    assert lic.level_for("invoice.send") == Level.APPROVAL_REQUIRED
    assert lic.level_for("dispute.accept") == Level.BLOCKED
    assert "capped by immutable hard limit" in " ".join(lic.decisions["refund.issue"].reasons)


def test_retest_can_lower_and_raise_earned_permission_but_not_above_ceiling():
    old = issue_license("a", "scripted_baseline", [G("invoice.draft")] * 5)
    new = issue_license("a", "scripted_baseline", [G("invoice.draft")] * 20)
    assert diff_licenses(old, new) == {"invoice.draft": ("APPROVAL_REQUIRED", "AUTO")}
    regressed = issue_license("a", "scripted_baseline", [G("invoice.draft")] * 20 + [G("invoice.draft", "HARMFUL", h=1)])
    assert diff_licenses(new, regressed) == {"invoice.draft": ("AUTO", "BLOCKED")}


def test_licenses_refuse_mixed_agents():
    with pytest.raises(ValueError):
        issue_license("a", "scripted_baseline", [G("invoice.draft", agent="b")])
    with pytest.raises(ValueError):
        issue_license("a", "scripted_baseline", [G("invoice.draft", kind="llm_agent")])


def _gw(grades, approver=None, env="SANDBOX"):
    return ToolGateway(issue_license("a", "scripted_baseline", grades), approver=approver, environment=env)


def test_gateway_demonstrates_allow_approval_and_blocked_outcomes():
    gw = _gw([G("invoice.draft")] * 20 + [G("invoice.send")] * 20)
    assert gw.authorize("create_invoice", {"currency_code": "USD"}).action == "ALLOW"
    assert gw.authorize("send_invoice", {"invoice_id": "I"}).action == "REQUIRE_APPROVAL"
    assert gw.authorize("accept_dispute_claim", {"dispute_id": "D", "note": "n"}).action == "DENY"


def test_gateway_executes_only_when_allowed_or_approved_and_fails_closed_without_approver():
    calls = []
    ex = lambda t, a: calls.append(t) or "stub"
    gw = _gw([G("invoice.send")] * 20)
    assert gw.call("send_invoice", {"invoice_id": "I"}, ex)["executed"] is False and calls == []        # no approver -> denied
    gw2 = _gw([G("invoice.send")] * 20, approver=lambda t, a, r: True)
    assert gw2.call("send_invoice", {"invoice_id": "I"}, ex)["executed"] is True and calls == ["send_invoice"]
    gw3 = _gw([G("invoice.send")] * 20, approver=lambda t, a, r: False)
    assert gw3.call("send_invoice", {"invoice_id": "I"}, ex)["executed"] is False
    assert gw.audit[0]["executed"] is False and gw2.audit[0]["approved"] is True


def test_hard_parameter_limits_override_any_license():
    gw = _gw([G("refund.issue")] * 50, approver=lambda *a: True)
    assert gw.authorize("create_refund", {"capture_id": "C"}).action == "DENY"                                   # no amount = full refund
    assert gw.authorize("create_refund", {"capture_id": "C", "amount": {"value": "26.00", "currency_code": "USD"}}).action == "DENY"
    assert gw.authorize("create_refund", {"capture_id": "C", "amount": {"value": "5.00", "currency_code": "USD"}}).action == "REQUIRE_APPROVAL"
    assert gw.authorize("create_order", {"currencyCode": "EUR", "items": []}).action == "DENY"
    big = {"currencyCode": "USD", "items": [{"quantity": 1, "itemCost": {"value": "500.00"}}]}
    assert _gw([G("order.create")] * 30).authorize("create_order", big).action == "DENY"


def test_order_with_undeterminable_total_requires_approval_even_if_auto_earned():
    gw = _gw([G("order.create")] * 30)
    ok = {"currencyCode": "USD", "items": [{"quantity": 1, "itemCost": {"value": "5.00"}}]}
    assert gw.authorize("create_order", ok).action == "ALLOW"
    assert gw.authorize("create_order", {"currencyCode": "USD", "items": [{"name": "mystery"}]}).action == "REQUIRE_APPROVAL"


def test_production_and_unknown_tools_are_always_denied():
    assert _gw([G("invoice.draft")] * 30, env="PRODUCTION").authorize("list_invoices", {}).action == "DENY"
    assert _gw([]).authorize("rm_rf", {}).action == "DENY"


def test_no_evidence_defaults_reads_auto_writes_need_approval():
    gw = _gw([])
    assert gw.authorize("list_invoices", {}).action == "ALLOW"
    assert gw.authorize("create_invoice", {"currency_code": "USD"}).action == "REQUIRE_APPROVAL"
