"""OFFLINE tests for the wallet-run experiment. All PayPal responses are MADE UP; no test contacts PayPal.
(The loopback-listener tests use 127.0.0.1 only.)"""
import json
import threading
import urllib.error
import urllib.request

import pytest

from roadworthy.client import SandboxClient
from roadworthy.permissions.gateway import ToolGateway
from roadworthy.permissions.engine import issue_license
from roadworthy.wallet_run import (CONFIRM_PHRASE, ENV_FLAG, GATEWAY_CREATE_ARGS, CallbackListener, PreparedRequest, RunStore,
                                   WalletRunError, WalletRunner, WriteGuardedClient, build_create_body, make_gateway)

ENV_ON = {ENV_FLAG: "1"}
OID, CID, MERCH = "ORDER1234567890", "CAPTURE12345678", "MERCHANT123456"
APPROVE = "https://www.sandbox.paypal.com/checkoutnow?token=" + OID


class FakeWrite:
    """Records every POST. Behaviour per path is scripted by `handler(method, url, body) -> (status, dict)`."""
    def __init__(self, handler): self.handler, self.reqs = handler, []
    def request(self, method, url, headers, body, timeout):
        b = json.loads(body.decode()) if body else None
        self.reqs.append({"method": method, "url": url, "headers": headers, "body": b})
        s, d = self.handler(method, url, b)
        return s, json.dumps(d).encode()


class FakeRead:
    def __init__(self, routes): self.routes, self.urls = routes, []
    def request(self, method, url, headers, timeout):
        self.urls.append((method, url))
        s, d = self.routes(url)
        return s, json.dumps(d).encode()


def order_body(cid, status="APPROVED", amount="2.00", cur="USD", captures=None, custom=None, merchant=MERCH):
    u = {"custom_id": custom or cid, "amount": {"currency_code": cur, "value": amount}, "payee": {"merchant_id": merchant}}
    if captures is not None:
        u["payments"] = {"captures": captures}
    return {"id": OID, "status": status, "intent": "CAPTURE", "purchase_units": [u]}


def default_write(method, url, body):
    if url.endswith("/v2/checkout/orders"):
        cid = body["purchase_units"][0]["custom_id"]
        r = order_body(cid, "PAYER_ACTION_REQUIRED")
        r["links"] = [{"rel": "payer-action", "href": APPROVE}]
        return 201, r
    if url.endswith("/capture"):
        return 201, {"id": OID, "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": CID, "status": "COMPLETED"}]}}]}
    return 404, {"name": "NOPE"}


def capture_json(cid, status="COMPLETED", amount="2.00", links=True):
    return {"id": CID, "status": status, "amount": {"currency_code": "USD", "value": amount}, "custom_id": cid,
            "create_time": "2026-10-30T10:00:00Z", "links": [{"rel": "refund", "href": "x"}] if links else []}


def make(tmp_path, write=default_write, read_routes=None, env=ENV_ON):
    out = []
    wt = FakeWrite(write)
    rt = FakeRead(read_routes or (lambda url: (404, {})))
    r = WalletRunner(RunStore(tmp_path), SandboxClient(lambda: "TOKEN-SECRET-VALUE", transport=rt), lambda: "TOKEN-SECRET-VALUE",
                     write_transport=wt, env=env, say=out.append)
    return r, wt, rt, out


def create_run(tmp_path, **kw):
    r, wt, rt, out = make(tmp_path, **kw)
    assert r.create(CONFIRM_PHRASE, callback="invalid") == 0
    run_id = RunStore(tmp_path).unfinished()[0]
    return r, wt, rt, out, run_id


# ------------------------------------------------------------------ gates
def test_create_without_env_flag_or_phrase_sends_nothing_and_writes_no_file(tmp_path):
    for env, phrase in [({}, CONFIRM_PHRASE), (ENV_ON, None), (ENV_ON, "wrong")]:
        r, wt, _, _ = make(tmp_path, env=env)
        with pytest.raises(WalletRunError):
            r.create(phrase, callback="invalid")
        assert wt.reqs == [] and list(tmp_path.glob("*")) == []


def test_gateway_refusal_sends_nothing(tmp_path):
    gw = ToolGateway(issue_license("a", "scripted_baseline", []), approver=None)           # no approver -> fail closed
    wt = FakeWrite(default_write)
    c = WriteGuardedClient(lambda: "t", gw, wt)
    prep = PreparedRequest("POST", "/v2/checkout/orders", build_create_body({"custom_id": "SENTINEL-x", "return_url": "https://a.invalid/r", "cancel_url": "https://a.invalid/c"}), "rid")
    out = c.call("create_order", GATEWAY_CREATE_ARGS, prep)
    assert out["executed"] is False and wt.reqs == []


def test_production_gateway_is_refused(tmp_path):
    gw = ToolGateway(issue_license("a", "scripted_baseline", []), approver=lambda *a: True, environment="PRODUCTION")
    c = WriteGuardedClient(lambda: "t", gw, FakeWrite(default_write))
    with pytest.raises(WalletRunError):
        c.call("create_order", GATEWAY_CREATE_ARGS, PreparedRequest("POST", "/v2/checkout/orders", {}, "rid"))


# ------------------------------------------------------------------ write client structure
def test_write_client_requires_gateway_and_has_no_public_post():
    with pytest.raises(TypeError):
        WriteGuardedClient(lambda: "t", None)
    c = WriteGuardedClient(lambda: "t", make_gateway(CONFIRM_PHRASE, ENV_ON), FakeWrite(default_write))
    public = [n for n in dir(c) if not n.startswith("_")]
    assert public == ["call", "sent"]


def test_private_post_refuses_without_a_gateway_permit():
    wt = FakeWrite(default_write)
    c = WriteGuardedClient(lambda: "t", make_gateway(CONFIRM_PHRASE, ENV_ON), wt)
    prep = PreparedRequest("POST", "/v2/checkout/orders/ORDER1234567890/capture", {}, "rid")
    for permit in ("guess", "", None):
        with pytest.raises(WalletRunError):
            c._post(permit, "pay_order", {"id": OID}, prep)
    assert wt.reqs == []


@pytest.mark.parametrize("tool,args,prep", [
    ("create_refund", {"capture_id": "C"}, PreparedRequest("POST", "/v2/payments/captures/CAPTURE12345678/refund", {}, "r")),
    ("pay_order", {"id": OID}, PreparedRequest("POST", "/v2/payments/captures/CAPTURE12345678/refund", {}, "r")),
    ("pay_order", {"id": OID}, PreparedRequest("POST", "/v2/checkout/orders/ORDER1234567890/authorize", {}, "r")),
    ("pay_order", {"id": OID}, PreparedRequest("POST", "/v2/checkout/orders/OTHERORDER9999/capture", {}, "r")),
    ("pay_order", {"id": OID}, PreparedRequest("POST", "/v2/checkout/orders/ORDER1234567890/capture", {"x": 1}, "r")),
    ("pay_order", {"id": OID}, PreparedRequest("PATCH", "/v2/checkout/orders/ORDER1234567890/capture", {}, "r")),
    ("pay_order", {"id": OID}, PreparedRequest("POST", "/v2/checkout/orders/ORDER1234567890/capture", {}, "")),
    ("create_order", GATEWAY_CREATE_ARGS, PreparedRequest("POST", "/v2/checkout/orders/x/capture", {}, "r")),
])
def test_only_two_requests_are_allow_listed(tool, args, prep):
    wt = FakeWrite(default_write)
    c = WriteGuardedClient(lambda: "t", make_gateway(CONFIRM_PHRASE, ENV_ON), wt)
    with pytest.raises(WalletRunError):
        c.call(tool, args, prep)
    assert wt.reqs == []


@pytest.mark.parametrize("value", ["2.01", "20.00", "0.01", "2"])
def test_order_amount_must_be_exactly_two_usd(value):
    wt = FakeWrite(default_write)
    c = WriteGuardedClient(lambda: "t", make_gateway(CONFIRM_PHRASE, ENV_ON), wt)
    body = build_create_body({"custom_id": "SENTINEL-x", "return_url": "https://a.invalid/r", "cancel_url": "https://a.invalid/c"})
    body["purchase_units"][0]["amount"]["value"] = value
    args = json.loads(json.dumps(GATEWAY_CREATE_ARGS))
    if value == "2":
        pass                                   # "2" == Decimal 2.00 numerically: still exactly two dollars, allowed
    else:
        with pytest.raises(WalletRunError):
            c.call("create_order", args, PreparedRequest("POST", "/v2/checkout/orders", body, "rid"))
        assert wt.reqs == []


def test_gateway_arguments_must_agree_with_the_request():
    c = WriteGuardedClient(lambda: "t", make_gateway(CONFIRM_PHRASE, ENV_ON), FakeWrite(default_write))
    bad = json.loads(json.dumps(GATEWAY_CREATE_ARGS)); bad["items"][0]["itemCost"]["value"] = "3.00"
    body = build_create_body({"custom_id": "SENTINEL-x", "return_url": "https://a.invalid/r", "cancel_url": "https://a.invalid/c"})
    with pytest.raises(WalletRunError):
        c.call("create_order", bad, PreparedRequest("POST", "/v2/checkout/orders", body, "rid"))


# ------------------------------------------------------------------ create
def test_create_happy_path_one_post_with_idempotency_key_and_state_saved(tmp_path):
    r, wt, _, out, run_id = create_run(tmp_path)
    assert len(wt.reqs) == 1
    q = wt.reqs[0]
    assert q["method"] == "POST" and q["url"] == "https://api-m.sandbox.paypal.com/v2/checkout/orders"
    assert q["body"]["intent"] == "CAPTURE" and q["body"]["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "2.00"}
    assert q["headers"]["PayPal-Request-Id"] and len(q["headers"]["PayPal-Request-Id"]) <= 108
    ec = q["body"]["payment_source"]["paypal"]["experience_context"]
    assert ec["return_url"].startswith("https://sentinel.invalid/") and "example.com" not in json.dumps(q["body"])
    run = RunStore(tmp_path).load(run_id)
    assert run["state"] == "CREATED" and run["order_id"] == OID and run["request_id_create"] == q["headers"]["PayPal-Request-Id"]
    assert APPROVE in out and any("***7890" in l for l in out) and not any(OID in l for l in out if l != APPROVE)
    assert "TOKEN-SECRET" not in (tmp_path / f"wallet_run_{run_id}.json").read_text()


def test_unsafe_approval_link_is_not_accepted(tmp_path):
    def evil(method, url, body):
        s, r = default_write(method, url, body); r["links"] = [{"rel": "payer-action", "href": "https://evil.example/pay"}]; return s, r
    r, wt, _, out = make(tmp_path, write=evil)
    with pytest.raises(WalletRunError):
        r.create(CONFIRM_PHRASE, callback="invalid")
    assert "https://evil.example/pay" not in "\n".join(out)


def test_second_create_is_refused_while_a_run_is_unfinished(tmp_path):
    r, wt, _, _, run_id = create_run(tmp_path)
    with pytest.raises(WalletRunError):
        r.create(CONFIRM_PHRASE, callback="invalid")
    with pytest.raises(WalletRunError):
        r.create(CONFIRM_PHRASE, run_id=run_id, callback="invalid")         # CREATED cannot be "resumed" into a new order
    assert len(wt.reqs) == 1


def test_create_4xx_is_terminal_with_no_retry(tmp_path):
    r, wt, _, _ = make(tmp_path, write=lambda m, u, b: (422, {"name": "UNPROCESSABLE_ENTITY"}))
    with pytest.raises(WalletRunError) as e:
        r.create(CONFIRM_PHRASE, callback="invalid")
    assert "UNPROCESSABLE_ENTITY" in str(e.value) and len(wt.reqs) == 1
    assert RunStore(tmp_path).unfinished() == []


def test_create_5xx_is_ambiguous_then_resume_replays_same_key_and_body(tmp_path):
    calls = {"n": 0}
    def flaky(method, url, body):
        calls["n"] += 1
        return (503, {}) if calls["n"] == 1 else default_write(method, url, body)
    r, wt, _, _ = make(tmp_path, write=flaky)
    with pytest.raises(WalletRunError):
        r.create(CONFIRM_PHRASE, callback="invalid")
    assert len(wt.reqs) == 1                                   # no automatic retry
    run_id = RunStore(tmp_path).unfinished()[0]
    assert RunStore(tmp_path).load(run_id)["state"] == "CREATE_SENT"
    assert r.create(CONFIRM_PHRASE, run_id=run_id) == 0
    assert len(wt.reqs) == 2
    assert wt.reqs[0]["headers"]["PayPal-Request-Id"] == wt.reqs[1]["headers"]["PayPal-Request-Id"] and wt.reqs[0]["body"] == wt.reqs[1]["body"]


def test_create_transport_exception_is_ambiguous_not_retried(tmp_path):
    def boom(method, url, body): raise TimeoutError("t")
    r, wt, _, _ = make(tmp_path, write=boom)
    with pytest.raises(WalletRunError) as e:
        r.create(CONFIRM_PHRASE, callback="invalid")
    assert "unknown" in str(e.value) and len(wt.reqs) == 1


def test_echoed_amount_mismatch_stops(tmp_path):
    def wrong(method, url, body):
        s, r = default_write(method, url, body); r["purchase_units"][0]["amount"]["value"] = "3.00"; return s, r
    r, _, _, _ = make(tmp_path, write=wrong)
    with pytest.raises(WalletRunError):
        r.create(CONFIRM_PHRASE, callback="invalid")
    assert RunStore(tmp_path).unfinished() == []


# ------------------------------------------------------------------ capture
def _reader_for(tmp_path_run, status="APPROVED", **kw):
    def routes(url):
        if url.endswith(f"/v2/checkout/orders/{OID}"):
            return 200, order_body(tmp_path_run["custom_id"], status, **kw)
        if url.endswith(f"/v2/payments/captures/{CID}"):
            return 200, capture_json(tmp_path_run["custom_id"])
        return 404, {}
    return routes


def _prep(tmp_path, status="APPROVED", **kw):
    r, wt, rt, out, run_id = create_run(tmp_path)
    run = RunStore(tmp_path).load(run_id)
    rt.routes = _reader_for(run, status, **kw)
    return r, wt, rt, out, run_id


def test_capture_refuses_unless_paypal_says_approved(tmp_path):
    r, wt, rt, out, run_id = _prep(tmp_path, status="PAYER_ACTION_REQUIRED")
    assert r.capture(CONFIRM_PHRASE, run_id) == 1
    assert len(wt.reqs) == 1                                    # only the create POST; no capture POST
    assert RunStore(tmp_path).load(run_id)["state"] == "CREATED"


@pytest.mark.parametrize("kw", [{"amount": "3.00"}, {"cur": "EUR"}, {"custom": "SENTINEL-other"}])
def test_capture_refuses_a_mismatching_order(tmp_path, kw):
    r, wt, rt, out, run_id = _prep(tmp_path, **kw)
    with pytest.raises(WalletRunError):
        r.capture(CONFIRM_PHRASE, run_id)
    assert len(wt.reqs) == 1


def test_capture_happy_path_posts_once_with_saved_key_and_cannot_repeat(tmp_path):
    r, wt, rt, out, run_id = _prep(tmp_path)
    assert r.capture(CONFIRM_PHRASE, run_id) == 0
    cap = wt.reqs[1]
    assert cap["url"] == f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{OID}/capture" and cap["body"] == {}
    run = RunStore(tmp_path).load(run_id)
    assert cap["headers"]["PayPal-Request-Id"] == run["request_id_capture"] != run["request_id_create"]
    assert run["state"] == "CAPTURED" and run["capture_id"] == CID
    assert [a["tool"] for a in run["gateway_audit"]] == ["create_order", "pay_order"]
    with pytest.raises(WalletRunError):
        r.capture(CONFIRM_PHRASE, run_id)
    assert len(wt.reqs) == 2
    assert not any("refund" in q["url"] for q in wt.reqs)
    assert not any(CID in l for l in out)


def test_capture_already_completed_records_id_without_posting(tmp_path):
    r, wt, rt, out, run_id = _prep(tmp_path, status="COMPLETED", captures=[{"id": CID, "status": "COMPLETED"}])
    assert r.capture(CONFIRM_PHRASE, run_id) == 0
    assert len(wt.reqs) == 1 and RunStore(tmp_path).load(run_id)["capture_id"] == CID


def test_capture_4xx_is_terminal(tmp_path):
    def w(method, url, body):
        return (422, {"name": "ORDER_NOT_APPROVED"}) if url.endswith("/capture") else default_write(method, url, body)
    r, wt, rt, out, run_id = create_run(tmp_path, write=w)
    rt.routes = _reader_for(RunStore(tmp_path).load(run_id))
    with pytest.raises(WalletRunError):
        r.capture(CONFIRM_PHRASE, run_id)
    assert RunStore(tmp_path).load(run_id)["state"] == "CAPTURE_FAILED"
    with pytest.raises(WalletRunError):
        r.capture(CONFIRM_PHRASE, run_id)
    assert len(wt.reqs) == 2


def test_capture_needs_gates(tmp_path):
    r, wt, rt, out, run_id = _prep(tmp_path)
    r.env = {}
    with pytest.raises(WalletRunError):
        r.capture(CONFIRM_PHRASE, run_id)
    r.env = ENV_ON
    with pytest.raises(WalletRunError):
        r.capture("nope", run_id)
    assert len(wt.reqs) == 1


# ------------------------------------------------------------------ verify
def _captured(tmp_path):
    r, wt, rt, out, run_id = _prep(tmp_path)
    r.capture(CONFIRM_PHRASE, run_id)
    run = RunStore(tmp_path).load(run_id)
    rt.routes = _reader_for(run, "COMPLETED", captures=[{"id": CID, "status": "COMPLETED"}])
    return r, wt, rt, run_id


def test_verify_is_get_only_and_passes(tmp_path):
    r, wt, rt, run_id = _captured(tmp_path)
    posts_before, gets_before = len(wt.reqs), len(rt.urls)
    rep = r.verify(run_id, expect_merchant_id=MERCH)
    assert rep.passed, rep.checks
    assert len(wt.reqs) == posts_before                      # verify never POSTs
    assert all(m == "GET" for m, _ in rt.urls[gets_before:]) and len(rt.urls) > gets_before


def test_verify_flags_wrong_merchant_and_unrefundable_capture(tmp_path):
    r, wt, rt, run_id = _captured(tmp_path)
    assert not r.verify(run_id, expect_merchant_id="OTHERMERCH99").passed
    assert any(s == "NOT_COMPARED" for _, s, _ in r.verify(run_id).checks)
    run = RunStore(tmp_path).load(run_id)
    base = rt.routes
    rt.routes = lambda url: (200, capture_json(run["custom_id"], status="PARTIALLY_REFUNDED")) if url.endswith(CID) else base(url)
    assert not r.verify(run_id).passed


def test_verify_fails_on_wrong_capture_amount(tmp_path):
    r, wt, rt, run_id = _captured(tmp_path)
    run = RunStore(tmp_path).load(run_id)
    base = rt.routes
    rt.routes = lambda url: (200, capture_json(run["custom_id"], amount="5.00")) if url.endswith(CID) else base(url)
    assert not r.verify(run_id).passed


# ------------------------------------------------------------------ callback listener (loopback only)
def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


def test_listener_accepts_only_its_nonce_path_and_ignores_the_query():
    l = CallbackListener("abc123nonce")
    try:
        assert l.return_url.startswith("http://127.0.0.1:")
        t = threading.Thread(target=lambda: (_get(l.return_url.replace("abc123nonce", "wrong") + "?token=SECRET&PayerID=X"),
                                             _get(l.return_url + "?token=SECRET&PayerID=X")))
        t.start()
        assert l.wait(5) == "return"
        t.join()
        assert "SECRET" not in repr(l.__dict__)
    finally:
        l.close()


def test_listener_times_out_quietly():
    l = CallbackListener("n")
    try:
        assert l.wait(0.6) is None
    finally:
        l.close()


def test_listener_binds_loopback_only():
    l = CallbackListener("n")
    try:
        assert l.httpd.server_address[0] == "127.0.0.1"
    finally:
        l.close()


def test_loopback_create_uses_the_listener_urls_and_closes_it(tmp_path):
    made = []
    def lf(nonce):
        l = CallbackListener(nonce); made.append(l); return l
    wt = FakeWrite(default_write)
    out = []
    r = WalletRunner(RunStore(tmp_path), SandboxClient(lambda: "t", transport=FakeRead(lambda u: (404, {}))), lambda: "t",
                     write_transport=wt, env=ENV_ON, say=out.append, listener_factory=lf)
    assert r.create(CONFIRM_PHRASE, callback="loopback", wait_seconds=0.3) == 0
    ec = wt.reqs[0]["body"]["payment_source"]["paypal"]["experience_context"]
    assert ec["return_url"] == made[0].return_url and "127.0.0.1" in ec["return_url"] and "example.com" not in ec["return_url"]


# ------------------------------------------------------------------ project-wide invariants
def test_read_client_is_still_get_only_and_cli_has_no_refund_command():
    c = SandboxClient(lambda: "t", transport=FakeRead(lambda u: (200, {})))
    assert not any(hasattr(c, n) for n in ("post", "put", "patch", "delete", "request_write"))
    from roadworthy import cli
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "refund" not in buf.getvalue().lower()                # no refund subcommand is advertised
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        cli.main(["refund"])                                     # no such command
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        cli.main(["wallet-run", "refund"])                       # not a wallet-run action


def test_run_files_hold_no_credentials(tmp_path):
    r, wt, rt, out, run_id = _prep(tmp_path)
    r.capture(CONFIRM_PHRASE, run_id)
    text = (tmp_path / f"wallet_run_{run_id}.json").read_text()
    assert "TOKEN-SECRET" not in text and "Bearer" not in text and "@" not in text
