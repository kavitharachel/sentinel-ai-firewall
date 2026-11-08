"""SIMULATED end-to-end checkout lifecycle. Every PayPal response here is MADE UP by a stateful fake.
This proves SENTINEL's own logic (callback handling, state transitions, capture gating, idempotency).
It does NOT prove that PayPal approves a real buyer; genuine buyer approval is an external dependency."""
import json
import socket
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from roadworthy.client import SandboxClient
from roadworthy.wallet_run import (CONFIRM_PHRASE, ENV_FLAG, CallbackListener, RunStore, WalletRunError, WalletRunner)

OID, MERCH, CAP = "SIMORDER1234567890", "SIMMERCHANT123", "SIMCAPTURE123456"


class FakePayPal:
    """Stateful: CREATED -> (buyer approves) APPROVED -> COMPLETED. Mirrors the documented order lifecycle."""
    def __init__(self):
        self.status, self.merchant, self.captures, self.posts, self.gets = None, MERCH, [], [], 0
        self.custom_id = None
        self.created = datetime.now(timezone.utc) - timedelta(minutes=5)

    def body(self):
        u = {"custom_id": self.custom_id, "amount": {"currency_code": "USD", "value": "2.00"}, "payee": {"merchant_id": self.merchant}}
        if self.captures:
            u["payments"] = {"captures": self.captures}
        return {"id": OID, "status": self.status, "intent": "CAPTURE", "purchase_units": [u],
                "create_time": self.created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "links": [{"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=" + OID}]}

    def buyer_approves(self):           # SIMULATED buyer action (the real one is an external dependency)
        assert self.status == "PAYER_ACTION_REQUIRED"; self.status = "APPROVED"

    # transports
    def request(self, method, url, headers, body=None, timeout=0):      # used for both read and write transports
        if method == "GET" and "/payments/captures/" in url:
            if not self.captures:
                return 404, b"{}"
            return 200, json.dumps({"id": CAP, "status": "COMPLETED", "custom_id": self.custom_id,
                                    "amount": {"currency_code": "USD", "value": "2.00"},
                                    "create_time": self.created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "links": [{"rel": "refund", "href": "x"}]}).encode()
        if method == "GET":
            self.gets += 1
            return 200, json.dumps(self.body()).encode()
        self.posts.append(url)
        if url.endswith("/v2/checkout/orders"):
            self.custom_id = json.loads(body.decode())["purchase_units"][0]["custom_id"]
            self.status = "PAYER_ACTION_REQUIRED"
            return 201, json.dumps(self.body()).encode()
        if url.endswith("/capture"):
            if self.status != "APPROVED":
                return 422, json.dumps({"name": "ORDER_NOT_APPROVED"}).encode()
            self.captures = [{"id": CAP, "status": "COMPLETED"}]; self.status = "COMPLETED"
            return 201, json.dumps(self.body()).encode()
        return 404, b"{}"


class ReadAdapter:
    def __init__(self, pp): self.pp = pp
    def request(self, method, url, headers, timeout): return self.pp.request(method, url, headers, None, timeout)


def make(tmp_path, pp=None):
    pp = pp or FakePayPal()
    out = []
    pc = {"profile": "us", "status": "OK", "fingerprint": "fp1", "expected_merchant_id": MERCH}
    r = WalletRunner(RunStore(tmp_path), SandboxClient(lambda: "T", transport=ReadAdapter(pp)), lambda: "T", write_transport=pp,
                     env={ENV_FLAG: "1"}, say=out.append, profile="us", fingerprint="fp1", profile_check=pc)
    return r, pp, out


def created(tmp_path):
    r, pp, out = make(tmp_path)
    assert r.create(CONFIRM_PHRASE, callback="invalid") == 0
    return r, pp, out, RunStore(tmp_path).unfinished()[0]


# ---- state transitions ----------------------------------------------------------------------------
def test_full_lifecycle_create_approve_capture_verify(tmp_path):
    r, pp, out, rid = created(tmp_path)
    assert pp.status == "PAYER_ACTION_REQUIRED" and len(pp.posts) == 1
    pp.buyer_approves()
    assert r.capture(CONFIRM_PHRASE, rid) == 0
    assert pp.status == "COMPLETED" and len(pp.captures) == 1 and len(pp.posts) == 2
    assert RunStore(tmp_path).load(rid)["state"] == "CAPTURED"
    assert r.verify(rid, MERCH).passed


def test_capture_refused_before_buyer_approval_sends_no_post(tmp_path):
    r, pp, out, rid = created(tmp_path)
    assert r.capture(CONFIRM_PHRASE, rid) == 1
    assert len(pp.posts) == 1 and not pp.captures                     # only the create POST ever happened
    assert RunStore(tmp_path).load(rid)["state"] == "CREATED"


def test_capture_refused_on_wrong_payee(tmp_path):
    r, pp, out, rid = created(tmp_path)
    pp.buyer_approves(); pp.merchant = "SOMEONEELSE999"
    with pytest.raises(WalletRunError):
        r.capture(CONFIRM_PHRASE, rid)
    assert len(pp.posts) == 1 and not pp.captures


def test_capture_refused_on_tampered_amount_or_custom_id(tmp_path):
    r, pp, out, rid = created(tmp_path)
    pp.buyer_approves(); pp.custom_id = "forged"
    with pytest.raises(WalletRunError):
        r.capture(CONFIRM_PHRASE, rid)
    assert len(pp.posts) == 1


def test_capture_needs_both_write_gates(tmp_path):
    r, pp, out, rid = created(tmp_path)
    pp.buyer_approves()
    with pytest.raises(WalletRunError):
        r.capture("wrong phrase", rid)
    r2 = WalletRunner(RunStore(tmp_path), r.reader, lambda: "T", write_transport=pp, env={}, say=out.append, profile="us",
                      fingerprint="fp1", profile_check=r.profile_check if hasattr(r, "profile_check") else None)
    with pytest.raises(WalletRunError):
        r2.capture(CONFIRM_PHRASE, rid)
    assert len(pp.posts) == 1 and not pp.captures


def test_second_capture_is_refused_and_no_double_charge(tmp_path):
    r, pp, out, rid = created(tmp_path)
    pp.buyer_approves(); r.capture(CONFIRM_PHRASE, rid)
    with pytest.raises(WalletRunError):
        r.capture(CONFIRM_PHRASE, rid)
    assert len(pp.captures) == 1 and len(pp.posts) == 2


def test_abandoned_run_cannot_be_captured(tmp_path):
    r, pp, out, rid = created(tmp_path)
    assert r.abandon(rid) == 0
    pp.buyer_approves()
    with pytest.raises(WalletRunError):
        r.capture(CONFIRM_PHRASE, rid)
    assert len(pp.posts) == 1


def test_ambiguous_capture_that_actually_succeeded_is_recorded_without_resending(tmp_path):
    r, pp, out, rid = created(tmp_path)
    pp.buyer_approves(); pp.request("POST", f"x/v2/checkout/orders/{OID}/capture", {}, b"")   # PayPal captured; our client never saw it
    run = RunStore(tmp_path).load(rid); run["state"] = "CAPTURE_SENT"; RunStore(tmp_path).save(run)
    posts = len(pp.posts)
    assert r.capture(CONFIRM_PHRASE, rid) == 0
    assert len(pp.posts) == posts and RunStore(tmp_path).load(rid)["state"] == "CAPTURED"


# ---- callback listener -----------------------------------------------------------------------------
def _get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status


def test_listener_return_and_cancel_and_wrong_nonce():
    l = CallbackListener("abc123")
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(f"http://127.0.0.1:{l.port}/sentinel/WRONG/return")
        assert e.value.code == 404 and l.result is None
        assert _get(l.return_url + "?token=SECRET&PayerID=P") == 200      # query string is ignored, never stored
        assert l.wait(1) == "return"
    finally:
        l.close()


def test_listener_idle_preconnect_does_not_block_the_real_redirect():
    """Regression for the confirmed defect: a browser's idle speculative connection froze the old single-threaded listener."""
    l = CallbackListener("n1")
    idle = socket.create_connection(("127.0.0.1", l.port))             # connects, sends nothing
    try:
        t0 = time.time()
        assert _get(l.return_url) == 200
        assert l.wait(2) == "return" and time.time() - t0 < 3
    finally:
        idle.close(); l.close()


def test_listener_binds_loopback_only():
    l = CallbackListener("n2")
    try:
        assert l.httpd.server_address[0] == "127.0.0.1"
    finally:
        l.close()
