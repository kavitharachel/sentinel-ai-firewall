"""Security audit of the public Judge Bench POST endpoints (OFFLINE, public mode): validation, size limits, abuse resistance, cross-origin, error handling."""
import http.client
import json
import os
import threading

import pytest

from roadworthy.dashboard.server import serve

HOST_HDR = "demo.example.com"


def _start(env):
    keys = ("SENTINEL_PUBLIC", "SENTINEL_PUBLIC_HOST", "SENTINEL_RATE_POST_PER_MIN", "SENTINEL_RATE_GLOBAL_POST_PER_MIN", "SENTINEL_RATE_GET_PER_MIN")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({"SENTINEL_PUBLIC": "1", "SENTINEL_PUBLIC_HOST": HOST_HDR, **env})
    s, port = serve(0, bind="127.0.0.1")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, port, old


def _stop(s, old):
    s.shutdown(); s.server_close()
    for k, v in old.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@pytest.fixture(scope="module")
def port():
    s, p, old = _start({"SENTINEL_RATE_POST_PER_MIN": "100000", "SENTINEL_RATE_GLOBAL_POST_PER_MIN": "100000", "SENTINEL_RATE_GET_PER_MIN": "100000"})
    yield p
    _stop(s, old)


def req(port, method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}"}; h.update(headers or {})
    if isinstance(body, (dict, list)): body = json.dumps(body).encode(); h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read(); c.close()
    try: js = json.loads(data)
    except ValueError: js = None
    return r.status, js, dict(r.getheaders()), data


GOOD = {"tool": "get_order", "args": {"id": "ORD-1"}}
JSON_CT = {"Content-Type": "application/json"}


def test_wrong_host_header_is_rejected_dns_rebinding(port):
    assert req(port, "POST", "/api/bench/run", GOOD, host="evil.example.com")[0] == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1:1", "https://demo.example.com.evil.example"])
def test_cross_origin_posts_are_rejected(port, origin):
    assert req(port, "POST", "/api/bench/run", GOOD, {"Origin": origin})[0] == 403


def test_cross_site_fetch_without_origin_is_rejected(port):
    assert req(port, "POST", "/api/bench/run", GOOD, {"Sec-Fetch-Site": "cross-site"})[0] == 403


@pytest.mark.parametrize("ct", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""])
def test_only_json_content_type(port, ct):
    h = {"Content-Type": ct} if ct else {}
    assert req(port, "POST", "/api/bench/run", json.dumps(GOOD).encode(), h)[0] == 415


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_other_methods_are_refused(port, method):
    assert req(port, method, "/api/bench/run", b"{}", JSON_CT)[0] in (405, 501)


def test_get_on_post_routes_and_path_tricks(port):
    assert req(port, "GET", "/api/bench/run")[0] in (404, 405)
    for path in ["/api/bench/../attacks/run", "/api/bench/run/", "/api/bench/%2e%2e/run", "/api/bench/RUN", "/api/bench"]:
        assert req(port, "POST", path, GOOD)[0] in (404, 400, 403), path


def test_size_limits_and_framing(port):
    assert req(port, "POST", "/api/bench/run", b"x" * 9000, JSON_CT)[0] == 413
    assert req(port, "POST", "/api/bench/run", b"", JSON_CT)[0] == 400                              # empty object has no tool
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)                                  # declared length far beyond the cap: refused without reading
    c.putrequest("POST", "/api/bench/run"); c.putheader("Host", f"127.0.0.1:{port}"); c.putheader("Content-Type", "application/json"); c.putheader("Content-Length", "99999999"); c.endheaders()
    assert c.getresponse().status == 413; c.close()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest("POST", "/api/bench/run"); c.putheader("Host", f"127.0.0.1:{port}"); c.putheader("Content-Type", "application/json"); c.putheader("Content-Length", "-5"); c.endheaders()
    assert c.getresponse().status in (400, 413); c.close()


@pytest.mark.parametrize("raw", [b"\xff\xfe\x00", b'{"tool": "a", "args": {"x": "\\ud800"}}', b"{" * 100, b"null", b"[]", b'"str"', b"12", b"{\"tool\":}"])
def test_malformed_bodies_get_clean_4xx(port, raw):
    s, js, _, _ = req(port, "POST", "/api/bench/run", raw, JSON_CT)
    assert 400 <= s < 500 or s == 200
    assert req(port, "GET", "/api/health")[0] == 200


def test_deep_and_wide_arguments_are_bounded(port):
    deep = {"tool": "get_order", "args": json.loads('{"a":' * 40 + "1" + "}" * 40)}
    assert req(port, "POST", "/api/bench/run", deep)[0] == 400
    wide = {"tool": "get_order", "args": {f"k{i}": i for i in range(600)}}
    assert req(port, "POST", "/api/bench/run", wide)[0] in (400, 413)
    assert req(port, "POST", "/api/bench/run", {"tool": "get_order", "args": {"k" * 65: 1}})[0] == 400


def test_error_messages_never_reflect_input(port):
    marker = "<script>alert(1)</script>"
    for body in [{"tool": marker * 20, "args": {}}, {"tool": "x", "args": {marker * 20: 1}}, {"tool": "x", "args": {"a": marker * 20}}, {"tool": "x", "args": {}, "licence": marker}]:
        s, js, hdrs, data = req(port, "POST", "/api/bench/run", body)
        assert s == 400 and marker not in data.decode(errors="replace")
        assert hdrs["Content-Type"].startswith("application/json")


def test_unknown_tool_and_hostile_strings_are_data_not_code(port):
    s, js, hdrs, data = req(port, "POST", "/api/bench/run", {"tool": "<img src=x onerror=alert(1)>", "args": {"a": "'; DROP TABLE x;--"}})
    assert s == 200 and js["decision"]["action"] == "DENY" and js["executed"] is False
    assert hdrs["Content-Type"].startswith("application/json") and hdrs.get("X-Content-Type-Options") == "nosniff"


def test_forge_and_verify_accept_only_bounded_receipts(port):
    r = req(port, "POST", "/api/bench/run", GOOD)[1]
    assert req(port, "POST", "/api/bench/verify", {"receipt": r})[1]["ok"] is True
    nan_receipt = json.loads(json.dumps(r)); nan_receipt["input"]["args"]["id"] = float("nan")
    raw = json.dumps({"receipt": nan_receipt}).encode()                                           # contains a literal NaN token
    s, js, _, data = req(port, "POST", "/api/bench/forge", raw, JSON_CT)
    json.loads(data.decode(), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))        # response must be strict JSON
    s, js, _, _ = req(port, "POST", "/api/bench/verify", raw, JSON_CT); assert js["ok"] is False
    for junk in [None, 5, "x", [], {"decision": 5}, {"decision": {"action": "x"}, "hash": "y"}]:
        assert req(port, "POST", "/api/bench/verify", {"receipt": junk})[1]["ok"] is False


def test_the_bench_cannot_execute_or_reach_paypal(port):
    for p in __import__("roadworthy.dashboard.bench", fromlist=["x"]).PRESETS:
        s, js, _, _ = req(port, "POST", "/api/bench/run", {"tool": p["tool"], "args": p["args"]})
        assert s == 200 and js["executed"] is False
    assert req(port, "GET", "/api/health")[1]["writes_to_paypal"] is False
    import roadworthy.dashboard.bench as b, inspect
    src = inspect.getsource(b)
    for banned in ("wallet_run", "urllib", "http.client", "socket", "subprocess", "open(", "eval(", "exec(", "executor", "approver="):
        assert banned not in src.replace("approver=None", "").replace("no executor, no approver", ""), banned


def test_security_headers_on_bench_responses(port):
    for s_, js, h, _ in [req(port, "GET", "/api/bench/info"), req(port, "POST", "/api/bench/run", GOOD)]:
        assert s_ == 200
        for k in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
            assert k in h, k


def test_per_ip_and_global_rate_limits_apply_to_the_bench():
    s, p, old = _start({"SENTINEL_RATE_POST_PER_MIN": "5", "SENTINEL_RATE_GLOBAL_POST_PER_MIN": "100000", "SENTINEL_RATE_GET_PER_MIN": "100000"})
    try:
        codes = [req(p, "POST", "/api/bench/run", GOOD)[0] for _ in range(12)]
        assert codes[:5] == [200] * 5 and set(codes[5:]) == {429}
        assert req(p, "GET", "/api/health")[0] == 200
    finally:
        _stop(s, old)
    s, p, old = _start({"SENTINEL_RATE_POST_PER_MIN": "100000", "SENTINEL_RATE_GLOBAL_POST_PER_MIN": "7", "SENTINEL_RATE_GET_PER_MIN": "100000"})
    try:
        codes = [req(p, "POST", "/api/bench/run", GOOD, {"X-Forwarded-For": f"10.0.0.{i}"})[0] for i in range(14)]
        assert codes.count(200) <= 7 and 429 in codes
    finally:
        _stop(s, old)


def test_concurrent_hammering_stays_healthy(port):
    codes = []
    def worker():
        for _ in range(25): codes.append(req(port, "POST", "/api/bench/run", GOOD)[0])
    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(codes) == 200 and set(codes) <= {200, 429, 503}
    assert req(port, "GET", "/api/health")[0] == 200


# ---- request-body handling on early rejections / parser limits (found by the Windows run of 2026-11-11) ----------------------------------
import socket

from roadworthy.dashboard import server as _server


def test_recursion_error_in_the_json_parser_is_a_clean_400_not_a_dropped_connection(port, monkeypatch):
    """json.loads raises RecursionError on deeply nested input on some interpreters/platforms (Python 3.11, Windows 3.13). Simulated deterministically."""
    class Shim:
        def __getattr__(self, name): return getattr(json, name)
        @staticmethod
        def loads(s, *a, **k):
            if isinstance(s, str) and s.startswith('{"tool":"deep"'): raise RecursionError("maximum recursion depth exceeded")
            return json.loads(s, *a, **k)
    monkeypatch.setattr(_server, "json", Shim())
    st, js, _, _ = req(port, "POST", "/api/bench/run", b'{"tool":"deep","args":{}}', JSON_CT)
    assert st == 400 and js == {"error": "bad request"}
    assert req(port, "POST", "/api/bench/run", GOOD)[0] == 200                      # the server is still healthy afterwards


@pytest.mark.parametrize("name,headers,length,status", [
    ("wrong content type", {"Content-Type": "multipart/form-data"}, 7000, 415),
    ("oversize body", {"Content-Type": "application/json"}, 20000, 413),
    ("cross origin", {"Content-Type": "application/json", "Origin": "http://evil.example"}, 5000, 403),
])
def test_early_rejections_consume_the_request_body_before_replying(port, name, headers, length, status):
    """Replying and closing with unread request bytes makes Windows reset the connection (client sees WinError 10053 instead of the 4xx).
    So the server must read the (bounded) body first: with only part of the body sent it must NOT have replied yet; once the rest arrives it replies cleanly."""
    head = f"POST /api/bench/run HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: {length}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    body = b"x" * length
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(head.encode() + body[:100])
        s.settimeout(0.7)
        with pytest.raises(socket.timeout):
            s.recv(1)                                                           # still waiting for the body: it was not answered early
        s.settimeout(5); s.sendall(body[100:])
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk: break
            data += chunk
    finally:
        s.close()
    assert data.startswith(b"HTTP/1.") and f" {status} ".encode() in data.split(b"\r\n", 1)[0], (name, data[:80])


def test_a_huge_declared_body_is_not_read_and_the_connection_is_closed(port):
    head = f"POST /api/bench/run HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nContent-Length: {_server.DRAIN_MAX + 1}\r\n\r\n"
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(head.encode()); s.settimeout(5)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk: break
            data += chunk
    finally:
        s.close()
    assert b" 413 " in data.split(b"\r\n", 1)[0]                                  # answered at once, without waiting for 64 KB+ of body
