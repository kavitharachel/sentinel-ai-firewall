"""Judge Bench: raw tool calls through the REAL gateway, layer traces, deterministic receipts. OFFLINE, pure (nothing is executed)."""
import json
import os
import random
import threading
import urllib.request
import urllib.error

import pytest

from roadworthy.dashboard import bench
from roadworthy.dashboard.server import serve
from roadworthy.dashboard.sim import SimLedger

EXPECT = {"p1": ("DENY", "hard-limits"), "p2": ("REQUIRE_APPROVAL", "approval"), "p3": ("DENY", "hard-limits"), "p4": ("DENY", "hard-limits"),
          "p5": ("DENY", "hard-limits"), "p6": ("DENY", "hard-limits"), "p7": ("DENY", "known-tool"), "p8": ("DENY", "known-tool"),
          "p9": ("REQUIRE_APPROVAL", "approval"), "p10": ("REQUIRE_APPROVAL", "approval"), "p11": ("DENY", "hard-limits"), "p12": ("DENY", "hard-limits")}


@pytest.mark.parametrize("p", bench.PRESETS, ids=lambda p: p["id"])
@pytest.mark.parametrize("lic", ["veteran", "new"])
def test_presets_decide_as_documented_and_never_execute(p, lic):
    r = bench.evaluate(p["tool"], p["args"], lic)
    assert (r["decision"]["action"], r["decision"]["stopped_by"]) == EXPECT[p["id"]]
    assert r["executed"] is False
    assert bench.verify_receipt(r)["ok"]


def test_live_environment_is_denied_first():
    r = bench.evaluate("create_refund", bench.PRESETS[1]["args"], "veteran", "LIVE")
    assert r["decision"]["action"] == "DENY" and r["decision"]["stopped_by"] == "environment"
    assert [t["status"] for t in r["decision"]["trace"]][1:] == ["not reached"] * 5


def test_trace_is_ordered_and_consistent():
    for p in bench.PRESETS:
        tr = bench.evaluate(p["tool"], p["args"])["decision"]["trace"]
        assert [t["layer"] for t in tr] == [n for n, _ in bench.LAYERS]
        sts = [t["status"] for t in tr]
        assert sts.count("stopped") <= 1
        if "stopped" in sts:
            i = sts.index("stopped"); assert set(sts[:i]) == {"passed"} and set(sts[i + 1:]) <= {"not reached"}


def test_fuzz_no_call_beyond_hard_limits_is_ever_allowed():
    rnd = random.Random(20261110)
    amounts = ["0", "-1", "25", "25.00", "25.01", "1e2", "1e400", "NaN", "Infinity", "-Infinity", " 5 ", "５", "5,00", "0x10", None, True, [], {}, {"value": "30"}, {"value": "5", "currency_code": "EUR"}, 12, 12.5, 1e308, -0.0, "٣"]
    curs = ["USD", "usd", "EUR", "USＤ", "US D", "", None, 5, ["USD"]]
    for _ in range(600):
        args = {"capture_id": "CAP-1001"}
        if rnd.random() < .9: args["amount"] = rnd.choice(amounts)
        if rnd.random() < .8: args["currency_code"] = rnd.choice(curs)
        try:
            r = bench.evaluate("create_refund", args, rnd.choice(["veteran", "new"]), "SANDBOX")
        except bench.BenchError:
            continue
        if r["decision"]["action"] != "DENY":
            amt = args.get("amount"); amt = amt.get("value") if isinstance(amt, dict) else amt
            assert amt is not None and 0 < float(str(amt)) <= 25.0, args
            cc = args.get("currency_code")
            inner = args.get("amount").get("currency_code") if isinstance(args.get("amount"), dict) else None
            # fail closed: something other than DENY needs a STATED USD currency (top-level, and for a dict amount its own), never null/absent
            assert isinstance(cc, str) and cc.upper() == "USD", args
            assert r["decision"]["action"] == "REQUIRE_APPROVAL"
            if isinstance(args.get("amount"), dict):
                assert isinstance(inner, str) and inner.upper() == "USD", args
        assert bench.verify_receipt(r)["ok"]


@pytest.mark.parametrize("bad", [None, [], "x", 5])
def test_bad_args_are_rejected(bad):
    with pytest.raises(bench.BenchError): bench.evaluate("create_refund", bad)


def test_bounds():
    for t in ["", "x" * 65, None, 5]:
        with pytest.raises(bench.BenchError): bench.evaluate(t, {})
    with pytest.raises(bench.BenchError): bench.evaluate("get_order", {"a": "x" * 5000})
    with pytest.raises(bench.BenchError): bench.evaluate("get_order", {"a": "x" * 201})
    deep = cur = {}
    for _ in range(12): cur["a"] = {}; cur = cur["a"]
    with pytest.raises(bench.BenchError): bench.evaluate("get_order", deep)
    with pytest.raises(bench.BenchError): bench.evaluate("get_order", {"l": list(range(500))})
    with pytest.raises(bench.BenchError): bench.evaluate("get_order", {}, "godmode")
    with pytest.raises(bench.BenchError): bench.evaluate("get_order", {}, "veteran", "PRODUCTION")


def test_receipt_tamper_is_detected_and_hash_alone_is_not_enough():
    r = bench.evaluate("create_refund", bench.PRESETS[0]["args"])
    assert bench.verify_receipt(r)["ok"]
    t = json.loads(json.dumps(r)); t["decision"]["action"] = "ALLOW"
    v = bench.verify_receipt(t); assert not v["ok"] and not {c["check"]: c["ok"] for c in v["checks"]}["hash"]
    f = bench.forge_demo(r)                                    # decision flipped AND hash recomputed: the hash check passes...
    v = bench.verify_receipt(f); by = {c["check"]: c["ok"] for c in v["checks"]}
    assert by["hash"] and not by["re-execution"] and not v["ok"]   # ...but re-running the gateway catches it
    i = json.loads(json.dumps(r)); i["input"]["args"]["amount"]["value"] = "1.00"
    assert not bench.verify_receipt(i)["ok"]
    p = json.loads(json.dumps(r)); p["policy_digest"] = "0" * 64
    assert not bench.verify_receipt(p)["ok"]
    for junk in [None, 5, "x", [], {}, {"hash": "x"}]:
        assert not bench.verify_receipt(junk)["ok"]


def test_receipts_are_deterministic_and_bench_does_not_touch_the_ledger():
    before = SimLedger().snapshot()
    a = bench.evaluate("create_refund", bench.PRESETS[1]["args"]); b = bench.evaluate("create_refund", bench.PRESETS[1]["args"])
    assert a == b and SimLedger().snapshot() == before


# ---- over HTTP, in public mode ------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def base():
    keys = ("SENTINEL_PUBLIC", "SENTINEL_PUBLIC_HOST", "SENTINEL_RATE_POST_PER_MIN", "SENTINEL_RATE_GLOBAL_POST_PER_MIN", "SENTINEL_RATE_GET_PER_MIN")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({"SENTINEL_PUBLIC": "1", "SENTINEL_PUBLIC_HOST": "demo.example.com", "SENTINEL_RATE_POST_PER_MIN": "100000",
                       "SENTINEL_RATE_GLOBAL_POST_PER_MIN": "100000", "SENTINEL_RATE_GET_PER_MIN": "100000"})
    s, port = serve(0, bind="127.0.0.1")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    s.shutdown(); s.server_close()
    for k, v in old.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def post(base, path, raw, ctype="application/json"):
    req = urllib.request.Request(base + path, data=raw if isinstance(raw, bytes) else json.dumps(raw).encode(), headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_http_info_run_verify_forge(base):
    info = json.load(urllib.request.urlopen(base + "/api/bench/info"))        # must be strict JSON (no NaN tokens)
    assert len(info["presets"]) == 12 and info["tools"]
    s, r = post(base, "/api/bench/run", {"tool": "create_refund", "args": info["presets"][0]["args"]})
    assert s == 200 and r["decision"]["action"] == "DENY"
    assert post(base, "/api/bench/verify", {"receipt": r})[1]["ok"] is True
    s, f = post(base, "/api/bench/forge", {"receipt": r}); assert s == 200
    assert post(base, "/api/bench/verify", {"receipt": f})[1]["ok"] is False


def test_http_adversarial_bodies(base):
    s, r = post(base, "/api/bench/run", b'{"tool":"create_refund","args":{"amount":{"value":NaN,"currency_code":"USD"},"capture_id":"x"}}')
    assert s == 200 and r["decision"]["action"] == "DENY"                  # a literal NaN token is accepted by the parser and denied by the gateway
    json.dumps(r, allow_nan=False)                                          # and the response is strict JSON
    assert post(base, "/api/bench/run", b'{"tool":"x","args":' + b"[" * 3000 + b"]" * 3000 + b"}")[0] == 400   # deep nesting: clean 400, no crash
    assert post(base, "/api/bench/run", b"[" * 5000)[0] == 400
    assert post(base, "/api/bench/run", b"x" * 9000)[0] == 413
    assert post(base, "/api/bench/run", b"{not json")[0] == 400
    assert post(base, "/api/bench/run", {"tool": "t", "args": {"a": "x" * 500}})[0] == 400
    assert post(base, "/api/bench/run", {"tool": "t", "args": {}}, "text/plain")[0] == 415
    assert post(base, "/api/bench/verify", {"receipt": {"a": 1}})[1]["ok"] is False
    assert post(base, "/api/bench/forge", {"receipt": "x"})[0] == 400
    assert post(base, "/api/bench/run", {"tool": "get_order", "args": {}})[0] == 200                  # server still healthy
    assert json.load(urllib.request.urlopen(base + "/api/health"))["writes_to_paypal"] is False


def test_http_unsafe_request_cannot_cross_origin(base):
    req = urllib.request.Request(base + "/api/bench/run", data=b'{"tool":"get_order","args":{}}', headers={"Content-Type": "application/json", "Origin": "https://evil.example"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e: urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 403
