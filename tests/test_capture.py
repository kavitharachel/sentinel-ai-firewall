"""OFFLINE tests for the read-only capture lookup and the refund-scope boolean.
All responses below are MADE UP; none of these tests contacts PayPal."""
import json
from datetime import datetime, timezone

import pytest

from roadworthy.auth import REFUND_SCOPE, Credentials, fetch_token
from roadworthy.capture import lookup_capture, mask_id, validate_capture_id
from roadworthy.client import SandboxClient

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


class T:
    def __init__(self, status, body):
        self.status, self.body, self.reqs = status, body, []

    def request(self, method, url, headers, timeout):
        self.reqs.append((method, url))
        return self.status, json.dumps(self.body).encode()


def _cap(status="COMPLETED", value="10.00", cur="USD", created="2026-10-20T10:00:00Z", refund_link=True, refunded=None):
    b = {"id": "CAP123456789", "status": status, "amount": {"currency_code": cur, "value": value},
         "create_time": created, "payer_email": "buyer@example.com",
         "links": [{"rel": "self", "href": "x"}] + ([{"rel": "refund", "href": "y"}] if refund_link else [])}
    if refunded is not None:
        b["seller_receivable_breakdown"] = {"total_refunded_amount": {"currency_code": cur, "value": refunded}}
    return b


def _lookup(body, status=200, cid="CAP123456789"):
    t = T(status, body)
    return lookup_capture(SandboxClient(lambda: "tok", transport=t), cid, now=NOW), t


def test_lookup_is_get_only_sandbox_and_hits_the_capture_path():
    info, t = _lookup(_cap())
    assert [m for m, _ in t.reqs] == ["GET"]
    assert t.reqs[0][1] == "https://api-m.sandbox.paypal.com/v2/payments/captures/CAP123456789"
    assert info.found and info.capture_status == "COMPLETED"


def test_completed_usd_capture_is_a_candidate_capped_at_hard_limit():
    info, _ = _lookup(_cap(value="40.00"))
    assert info.refund_experiment_ok and str(info.remaining_refundable) == "40.00"
    assert str(info.experiment_amount) == "25.00"          # never above MAX_REFUND_AMOUNT
    info2, _ = _lookup(_cap(value="7.50"))
    assert str(info2.experiment_amount) == "7.50"


@pytest.mark.parametrize("kw,fragment", [
    ({"status": "REFUNDED"}, "status is REFUNDED"),
    ({"status": "PENDING"}, "status is PENDING"),
    ({"cur": "EUR"}, "currency"),
    ({"value": "0.50"}, "below 1.00"),
    ({"refund_link": False}, "no refund link"),
    ({"created": "2026-01-01T00:00:00Z"}, "180-day"),
])
def test_not_a_candidate_reasons(kw, fragment):
    info, _ = _lookup(_cap(**kw))
    assert not info.refund_experiment_ok and any(fragment in r for r in info.reasons)


def test_partially_refunded_uses_reported_total_else_unknown_never_guessed():
    info, _ = _lookup(_cap(status="PARTIALLY_REFUNDED", value="10.00", refunded="4.00"))
    assert str(info.remaining_refundable) == "6.00" and info.refund_experiment_ok
    info2, _ = _lookup(_cap(status="PARTIALLY_REFUNDED", value="10.00"))
    assert info2.remaining_refundable is None and not info2.refund_experiment_ok


def test_404_means_not_a_capture_and_is_not_a_candidate():
    info, _ = _lookup({"name": "RESOURCE_NOT_FOUND"}, status=404)
    assert not info.found and not info.refund_experiment_ok and "RESOURCE_NOT_FOUND" in info.error


def test_summary_keeps_no_payer_data_and_masks_the_id():
    info, _ = _lookup(_cap())
    dump = repr(info.__dict__)
    assert "buyer@example.com" not in dump and "CAP123456789" not in dump
    assert info.masked_id == "***6789" == mask_id("CAP123456789")


@pytest.mark.parametrize("bad", ["", "abc", "../v1/oauth2/token", "ID/refund", "ID?x=1", "a b c d e", "x" * 65, None])
def test_capture_id_is_validated_before_any_request(bad):
    t = T(200, {})
    with pytest.raises(ValueError):
        lookup_capture(SandboxClient(lambda: "tok", transport=t), bad)
    assert t.reqs == []
    with pytest.raises(ValueError):
        validate_capture_id(bad)


# ---- refund-scope boolean ----
class R:
    def __init__(self, scope): self.scope = scope
    def read(self): return json.dumps({"access_token": "A21AA-fake-token-value-123456789", "expires_in": 100, "scope": self.scope}).encode()


def _tok(scope):
    return fetch_token(Credentials("cid", "sec"), opener=lambda req, timeout=30: R(scope), now=lambda: 0.0)


def test_refund_scope_present_and_absent():
    assert _tok(f"openid {REFUND_SCOPE} https://uri.paypal.com/services/reporting/search/read").has_refund_scope is True
    assert _tok("openid https://uri.paypal.com/services/reporting/search/read").has_refund_scope is False
    assert _tok("").has_refund_scope is False


def test_refund_scope_match_is_exact_not_substring():
    assert _tok(REFUND_SCOPE + "x").has_refund_scope is False


def test_scope_names_and_token_never_appear_in_repr():
    t = _tok(f"{REFUND_SCOPE} other")
    text = repr(t) + str(t)
    assert "uri.paypal.com" not in text and "A21AA" not in text and "services/payments" not in text
    assert t.scope_count == 2
