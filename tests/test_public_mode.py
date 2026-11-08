"""SENTINEL_PUBLIC=1: LLM hard-off, host/origin allow-list, rate + concurrency limits, headers, no private evidence. OFFLINE; no PayPal; no model."""
import http.client
import json
import threading

import pytest

from roadworthy.dashboard import llm_evidence, service
from roadworthy.dashboard.server import PublicPolicy, serve

HOST = "demo.example.com"
ORDER, MERCH, RID, NONCE = "ZQ7FIXTUREORDER1", "ZQ7FIXTMERCH1", "RID-SECRET-1", "ab12cd34ef56ab78"
KEY = "sk-test-PUBLIC-CANARY-9f8e7d6c5b4a3210"
ENV = {"SENTINEL_LLM_ENABLE": "1", "SENTINEL_LLM_PROVIDER": "openai", "SENTINEL_LLM_MODEL": "gpt-4.1-mini", "SENTINEL_LLM_API_KEY": KEY}


@pytest.fixture
def evidence(tmp_path):
    d = tmp_path / "us"; d.mkdir()
    (d / "wallet_run_x.json").write_text(json.dumps({"run_id": "x", "state": "CREATED", "order_id": ORDER, "order_status": "PAYER_ACTION_REQUIRED", "amount": "2.00", "currency": "USD",
                                                     "payee_merchant_id": MERCH, "created_at": "2026-10-31T06:19:16Z", "request_id_create": RID, "nonce": NONCE}))
    return d


def start(monkeypatch, evidence=None, **env):
    monkeypatch.setenv("SENTINEL_PUBLIC", "1")
    monkeypatch.setenv("SENTINEL_PUBLIC_HOST", env.pop("hosts", HOST))
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    srv, port = serve(0, evidence, bind="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


@pytest.fixture
def pub(monkeypatch, evidence):
    srv, port = start(monkeypatch, evidence)
    yield port
    srv.shutdown(); srv.server_close()


def call(port, method, path, body=None, headers=None, host=HOST):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host, **(headers or {})}
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items():
        c.putheader(k, v)
    if data is not None:
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data)
    r = c.getresponse(); raw = r.read(); hd = {k.lower(): v for k, v in r.getheaders()}; c.close()
    return r.status, hd, raw


def run(port, prompt, source="operator", agent="overconfident", **kw):
    s, h, b = call(port, "POST", "/api/console/run", {"prompt": prompt, "source": source, "agent": agent, **kw}, {"Origin": f"https://{HOST}"})
    return s, (json.loads(b) if b else None)


# ---------------------------------------------------------------------------------------- LLM is hard-off
def test_public_mode_never_calls_a_model_even_with_full_llm_config(pub, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    calls = []
    monkeypatch.setattr(service, "_TRANSPORT", lambda *a, **k: calls.append(a) or (200, b"{}"))
    s, out = run(pub, "Refund $18.00 on CAP-1003", agent="llm", consent=True)
    assert s == 403 and "disabled" in out["error"]
    for path, body in (("/api/agent/run", {"agent": "llm", "consent": True}), ("/api/timemachine", {"agent": "llm"})):
        st, _, _ = call(pub, "POST", path, body, {"Origin": f"https://{HOST}"})
        assert st in (400, 403, 409), path
    assert calls == []
    st = service.llm_status()
    assert st["enabled"] is False and st["provider"] is None and st["model"] is None and st["destination_host"] is None and st["key_configured"] is False


def test_public_responses_never_leak_llm_config_or_keys(pub, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    blob = b""
    for p in ("/api/console/info", "/api/llm/status", "/api/overview", "/api/claims", "/api/attacks", "/api/health"):
        s, _, b = call(pub, "GET", p)
        assert s == 200, p
        blob += b
    low = blob.decode().lower()
    assert KEY not in blob.decode() and "enable_with" not in low and "sentinel_llm_api_key" not in low and "api.openai.com" not in low and "gpt-4.1-mini" not in low
    rec = call(pub, "GET", "/api/llm-evidence")[2].decode()          # the recorded run legitimately names the model and host it was recorded against
    assert KEY not in rec and "enable_with" not in rec and "sentinel_llm_api_key" not in rec.lower() and json.loads(rec)["replayed"] is True
    for p in ("/app.js", "/", "/app.css"):                     # static code may mention field names, but never a value
        assert KEY not in call(pub, "GET", p)[2].decode()
    info = json.loads(call(pub, "GET", "/api/console/info")[2])
    assert info["public"] is True and info["llm"]["enabled"] is False and "providers" not in info["llm"]


def test_scripted_fallback_runs_the_three_scenarios_in_public_mode(pub):
    from roadworthy.dashboard import console
    sug = {x["id"]: x for x in console.SUGGESTIONS}
    out = {}
    for sc in console.SCENARIOS:
        g = sug[sc["suggestion"]]
        s, r = run(pub, g["text"], g["source"], g["agent"])
        assert s == 200 and r["agent"]["real_llm"] is False and r["evidence"]["unchanged"] and r["verdict"]["simulated_cash_out"] == "0", sc["id"]
        out[sc["id"]] = r
    assert any(p["decision"] == "DENIED" for p in out["unsafe-refund"]["proposals"])
    assert out["false-claim"]["verdict"]["label"] == "CONTRADICTED"
    assert sum(1 for p in out["injection"]["proposals"] if p["decision"] == "DENIED") >= 1
    assert not any(p["executed"] and p["tool"] not in (None, "get_order", "list_transactions") for p in out["injection"]["proposals"])


# ---------------------------------------------------------------------------------------- host / origin
def test_host_allowlist(pub):
    assert call(pub, "GET", "/api/health")[0] == 200
    assert call(pub, "GET", "/api/health", host=f"127.0.0.1:{pub}")[0] == 200
    for bad in ("evil.example.com", f"{HOST}.evil.com", "demo.example.com:9999", "", "localhost", "127.0.0.1:1"):
        assert call(pub, "GET", "/api/overview", host=bad)[0] == 403, bad
        assert call(pub, "POST", "/api/trust", {"capability": "order.create", "events": []}, {"Origin": f"https://{HOST}"}, host=bad)[0] == 403


def test_without_a_configured_public_host_only_loopback_is_served(monkeypatch):
    monkeypatch.delenv("SENTINEL_PUBLIC_HOST", raising=False)
    monkeypatch.setenv("SENTINEL_PUBLIC", "1")
    srv, port = serve(0, bind="127.0.0.1"); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call(port, "GET", "/api/overview", host="demo.example.com")[0] == 403
        assert call(port, "GET", "/api/overview", host=f"127.0.0.1:{port}")[0] == 200
    finally:
        srv.shutdown(); srv.server_close()


def test_invalid_host_entries_are_dropped():
    p = PublicPolicy({"SENTINEL_PUBLIC_HOST": "ok.example.com, bad host!, ,*.evil.com,http://x.com,Good.EXAMPLE.org"})
    assert p.hosts == {"ok.example.com", "good.example.org"}


def test_origin_checks(pub):
    body = {"capability": "order.create", "events": []}
    for origin, want in ((f"https://{HOST}", 200), (f"http://{HOST}", 200), ("https://evil.example.com", 403), ("null", 403), (f"https://{HOST}.evil.com", 403), ("https://demo.example.com:444", 403)):
        assert call(pub, "POST", "/api/trust", body, {"Origin": origin})[0] == want, origin
    assert call(pub, "POST", "/api/trust", body, {"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert call(pub, "POST", "/api/trust", body, {"Sec-Fetch-Site": "same-origin"})[0] == 200


def test_post_hygiene(pub):
    o = {"Origin": f"https://{HOST}"}
    assert call(pub, "POST", "/api/trust", b"x=1", {**o, "Content-Type": "text/plain"})[0] == 415
    assert call(pub, "POST", "/api/trust", b"{" + b" " * 9000 + b"}", o)[0] == 413
    assert call(pub, "POST", "/api/trust", b"[1,2]", o)[0] == 400
    assert call(pub, "POST", "/api/trust", b"{not json", o)[0] == 400
    assert call(pub, "POST", "/api/nope", {}, o)[0] == 404
    for m in ("PUT", "DELETE", "PATCH"):
        assert call(pub, m, "/api/health", b"{}")[0] == 405


# ---------------------------------------------------------------------------------------- limits
def test_per_ip_post_rate_limit_returns_429(monkeypatch):
    srv, port = start(monkeypatch, SENTINEL_RATE_POST_PER_MIN=3)
    try:
        o = {"Origin": f"https://{HOST}"}
        codes = [call(port, "POST", "/api/trust", {"capability": "order.create", "events": []}, o)[0] for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        s, h, b = call(port, "POST", "/api/trust", {"capability": "order.create", "events": []}, o)
        assert s == 429 and h["retry-after"] and call(port, "GET", "/api/health")[0] == 200       # reads have their own budget
    finally:
        srv.shutdown(); srv.server_close()


def test_health_probe_works_with_any_host_but_nothing_else_does(pub):
    assert call(pub, "GET", "/api/health", host="10.1.2.3:10000")[0] == 200            # platform probes use an internal Host
    assert call(pub, "GET", "/api/overview", host="10.1.2.3:10000")[0] == 403
    assert call(pub, "GET", "/app.js", host="10.1.2.3:10000")[0] == 403


def test_trusted_hops_counts_from_the_right():
    p = PublicPolicy({"SENTINEL_TRUSTED_PROXY": "1", "SENTINEL_TRUSTED_HOPS": "2"})
    assert p.client_ip("10.0.0.1", "9.9.9.9, 198.51.100.7, 172.16.0.5") == "198.51.100.7"      # client, then two proxies appended theirs
    assert p.client_ip("10.0.0.1", "172.16.0.5") == "10.0.0.1"                                  # too short a chain: do not trust it
    assert PublicPolicy({"SENTINEL_TRUSTED_PROXY": "1"}).client_ip("10.0.0.1", "9.9.9.9, 198.51.100.7") == "198.51.100.7"


def test_health_client_key_lets_the_operator_verify_proxy_hops(monkeypatch):
    srv, port = start(monkeypatch, SENTINEL_TRUSTED_PROXY=1, SENTINEL_TRUSTED_HOPS=1)
    try:
        key = lambda xff=None: json.loads(call(port, "GET", "/api/health", headers={"X-Forwarded-For": xff} if xff else None)[2])["client_key"]
        a, b = key("198.51.100.7"), key("203.0.113.9")
        assert a != b and key("198.51.100.7") == a and len(a) == 8
        assert key("1.2.3.4, 198.51.100.7") == a                      # a spoofed left entry does not change the key
        assert "198.51" not in a and key() not in (a, b)              # opaque; no header -> the socket peer
    finally:
        srv.shutdown(); srv.server_close()
    monkeypatch.delenv("SENTINEL_TRUSTED_PROXY"); monkeypatch.delenv("SENTINEL_TRUSTED_HOPS")
    srv, port = start(monkeypatch)                                    # not behind a trusted proxy: header ignored
    try:
        k = lambda xff: json.loads(call(port, "GET", "/api/health", headers={"X-Forwarded-For": xff})[2])["client_key"]
        assert k("198.51.100.7") == k("203.0.113.9")
    finally:
        srv.shutdown(); srv.server_close()


def test_get_rate_limit(monkeypatch):
    srv, port = start(monkeypatch, SENTINEL_RATE_GET_PER_MIN=4)
    try:
        assert [call(port, "GET", "/api/health")[0] for _ in range(6)] == [200] * 4 + [429] * 2
    finally:
        srv.shutdown(); srv.server_close()


def test_forwarded_for_cannot_dodge_the_limit_unless_the_proxy_is_trusted(monkeypatch):
    srv, port = start(monkeypatch, SENTINEL_RATE_POST_PER_MIN=2)
    try:
        o = lambda i: {"Origin": f"https://{HOST}", "X-Forwarded-For": f"203.0.113.{i}"}
        codes = [call(port, "POST", "/api/trust", {"capability": "order.create", "events": []}, o(i))[0] for i in range(4)]
        assert codes == [200, 200, 429, 429]                          # untrusted header ignored: all from the same socket peer
    finally:
        srv.shutdown(); srv.server_close()
    p = PublicPolicy({"SENTINEL_TRUSTED_PROXY": "1"})
    assert p.client_ip("10.0.0.1", "1.2.3.4, 198.51.100.7") == "198.51.100.7"        # rightmost = appended by our proxy; a spoofed left entry is ignored
    assert p.client_ip("10.0.0.1", "garbage<script>") == "10.0.0.1"
    assert PublicPolicy({}).client_ip("10.0.0.1", "198.51.100.7") == "10.0.0.1"


def test_global_post_limit_and_window_expiry():
    t = [0.0]
    p = PublicPolicy({"SENTINEL_RATE_POST_PER_MIN": "100", "SENTINEL_RATE_GLOBAL_POST_PER_MIN": "5"}, clock=lambda: t[0])
    got = [p.allow(f"ip{i}", "post") for i in range(8)]
    assert got == [True] * 5 + [False] * 3
    t[0] = 61.0
    assert p.allow("ip9", "post") is True


def test_tracked_ip_table_is_bounded():
    p = PublicPolicy({}, clock=lambda: 0.0); p.max_ips = 50
    ok = sum(p.allow(f"ip{i}", "get") for i in range(500))
    assert ok <= 51 and len(p._hits) <= 51


def test_concurrency_cap_returns_503_then_recovers(monkeypatch):
    srv, port = start(monkeypatch, SENTINEL_MAX_CONCURRENT=1)
    try:
        o = {"Origin": f"https://{HOST}"}
        entered, release = threading.Event(), threading.Event()
        orig = service.trust
        def slow(*a, **k):
            entered.set(); release.wait(5); return orig(*a, **k)
        monkeypatch.setattr(service, "trust", slow)
        res = {}
        t = threading.Thread(target=lambda: res.setdefault("a", call(port, "POST", "/api/trust", {"capability": "order.create", "events": []}, o)[0]))
        t.start(); assert entered.wait(5)
        s, h, _ = call(port, "POST", "/api/trust", {"capability": "order.create", "events": []}, o)
        assert s == 503 and h.get("retry-after")
        assert call(port, "GET", "/api/health")[0] == 200            # reads are never blocked by a busy simulation
        release.set(); t.join(5)
        assert res["a"] == 200
        assert call(port, "POST", "/api/trust", {"capability": "order.create", "events": []}, o)[0] == 200
    finally:
        release.set(); srv.shutdown(); srv.server_close()


# ---------------------------------------------------------------------------------------- headers / static
def test_security_headers_everywhere(pub):
    for path in ("/", "/app.js", "/app.css", "/api/health", "/api/nope"):
        s, h, _ = call(pub, "GET", path)
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer", path
        assert "frame-ancestors 'none'" in h["content-security-policy"] and "script-src 'self'" in h["content-security-policy"] and "unsafe-eval" not in h["content-security-policy"]
        assert "camera=()" in h["permissions-policy"] and h["cross-origin-opener-policy"] == "same-origin" and h["cache-control"] == "no-store"
        assert "strict-transport-security" not in h and "server" in h and "Python" not in h["server"]


def test_hsts_only_behind_a_trusted_https_proxy(monkeypatch):
    srv, port = start(monkeypatch, SENTINEL_TRUSTED_PROXY=1)
    try:
        assert "strict-transport-security" in call(port, "GET", "/api/health", headers={"X-Forwarded-Proto": "https"})[1]
        assert "strict-transport-security" not in call(port, "GET", "/api/health")[1]
    finally:
        srv.shutdown(); srv.server_close()


def test_only_the_fixed_static_files_are_served(pub):
    for p in ("/../server.py", "/..%2fserver.py", "/static/app.js", "/index.html", "/evidence/us/wallet_run_x.json", "/public_data/public_evidence.json", "/%2e%2e/%2e%2e/etc/passwd", "/roadworthy/client.py", "/.env", "/Dockerfile"):
        assert call(pub, "GET", p)[0] == 404, p


# ---------------------------------------------------------------------------------------- evidence privacy
def test_public_mode_ignores_the_private_evidence_folder_entirely(pub):
    blob = ""
    for p in ("/api/overview", "/api/claims", "/api/console/info", "/api/attacks", "/api/audit", "/app.js", "/api/llm-evidence"):
        s, _, b = call(pub, "GET", p); assert s == 200, p
        blob += b.decode()
    for secret in (ORDER, MERCH, RID, NONCE, "sandbox.paypal.com", "HS7EQP", "6NN049"):
        assert secret not in blob, secret
    o = json.loads(call(pub, "GET", "/api/overview")[2])
    assert o["live"]["runs"][0]["order_id_masked"] == "WITHHELD" and o["live"]["runs"][0]["payee_masked"] == "WITHHELD"
    claims = json.loads(call(pub, "GET", "/api/claims")[2])
    texts = json.dumps(claims)
    assert "identifier withheld" in texts and "CONTRADICTED" in texts


def test_the_shipped_public_summary_is_identifier_free_and_labelled():
    p = service.PUBLIC_DATA / "public_evidence.json"
    assert p.is_file()
    raw = p.read_text()
    d = json.loads(raw)
    assert d["schema"] == "sentinel.public-evidence/1" and d["captures"] == 0
    import re
    assert not re.search(r"[A-Z0-9]{12,}", raw) and "paypal.com" not in raw and "order_id" not in raw and "merchant_id" not in raw


def test_live_labelling_distinguishes_the_three_kinds_of_evidence(pub):
    js = call(pub, "GET", "/app.js")[2].decode(); html = call(pub, "GET", "/")[2].decode()
    for label in ("SIMULATED", "LIVE-RECORDED", "LLM-RECORDED"):
        assert label in html or label in js, label
    assert "does not protect any live PayPal transaction" in js


# ---------------------------------------------------------------------------------------- recorded-LLM endpoint
def test_recorded_run_endpoint_reports_not_recorded_when_absent(pub, monkeypatch, tmp_path):
    monkeypatch.setattr(llm_evidence, "DEFAULT_PATH", tmp_path / "missing.json")
    d = json.loads(call(pub, "GET", "/api/llm-evidence")[2])
    assert d["available"] is False and d["label"] == "LLM-RECORDED" and d["replayed"] is True and "No recorded run" in d["note"]


def test_binds_all_interfaces_only_in_public_mode(monkeypatch):
    monkeypatch.setenv("SENTINEL_PUBLIC", "1")
    srv, _ = serve(0); assert srv.server_address[0] == "0.0.0.0"; srv.server_close()
    monkeypatch.delenv("SENTINEL_PUBLIC")
    srv, _ = serve(0); assert srv.server_address[0] == "127.0.0.1"; srv.server_close()


def test_local_mode_is_unchanged_by_the_public_machinery(monkeypatch, evidence):
    monkeypatch.delenv("SENTINEL_PUBLIC", raising=False)
    srv, port = serve(0, evidence); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call(port, "GET", "/api/health", host=f"127.0.0.1:{port}")[0] == 200
        assert call(port, "GET", "/api/health", host=HOST)[0] == 403
        assert json.loads(call(port, "GET", "/api/health", host=f"localhost:{port}")[2])["public"] is False
        assert "x-frame-options" not in call(port, "GET", "/api/health", host=f"127.0.0.1:{port}")[1]
    finally:
        srv.shutdown(); srv.server_close()
