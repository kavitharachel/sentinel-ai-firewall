"""Dashboard tests. OFFLINE: simulated world + a temp copy of live-evidence-shaped data. No PayPal, no credentials."""
import ast
import http.client
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from roadworthy.dashboard import agents, live, service
from roadworthy.dashboard.attacks import run_all
from roadworthy.dashboard.audit import AuditLog, tampered_copy, verify_chain
from roadworthy.dashboard.server import serve
from roadworthy.dashboard.timemachine import replay
from roadworthy.dashboard.trust import timeline

DASH = Path(agents.__file__).parent
ROOT = DASH.parent.parent
FORBIDDEN_IMPORTS = {"http.client", "socket", "requests", "subprocess", "roadworthy.client", "roadworthy.auth",
                     "roadworthy.wallet_run", "roadworthy.profiles", "roadworthy.capture", "roadworthy.txsearch", "roadworthy.snapshot"}
ORDER, MERCH = "ZQ7FIXTUREORDER1", "ZQ7FIXTMERCH1"


@pytest.fixture
def evidence(tmp_path):
    d = tmp_path / "us"; d.mkdir()
    run = {"run_id": "zqfixturerun1", "state": "CREATED", "profile": "us", "order_id": ORDER, "order_status": "PAYER_ACTION_REQUIRED", "amount": "2.00",
           "currency": "USD", "payee_merchant_id": MERCH, "created_at": "2026-10-31T06:19:16Z", "capture_id": None,
           "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=" + ORDER, "nonce": "ab12cd34ef56ab78", "request_id_create": "RID-SECRET-1",
           "credential_fingerprint": "f1x7ure00000", "return_url": "http://127.0.0.1:59276/sentinel/ab12cd34ef56ab78/return"}
    (d / "wallet_run_zqfixturerun1.json").write_text(json.dumps(run))
    (d / "wallet_run_zqfixturerun1.pre-abandon.json").write_text("{}")
    (d / "profile_check.json").write_text(json.dumps({"status": "OK", "refund_scope": True, "txsearch_http": 403, "merchant_source": "none", "expected_merchant_id": MERCH}))
    return d


# ---- isolation ---------------------------------------------------------------------------------------
def _imports(path):
    out = []
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            out.append(("roadworthy." + (n.module or "")) if n.level == 2 else ("." + (n.module or "")) if n.level == 1 else (n.module or ""))
    return out


def test_dashboard_source_imports_no_paypal_client_and_confines_network_to_llm_module():
    for f in DASH.glob("*.py"):
        for m in _imports(f):
            assert m not in FORBIDDEN_IMPORTS, f"{f.name} imports {m}"
            if m in ("urllib.request", "urllib.error"):
                assert f.name == "llm.py", f"{f.name} may not use the network"


def test_importing_the_service_loads_no_network_client():
    code = "import sys, roadworthy.dashboard.service as s; bad=[m for m in ('urllib.request','roadworthy.client','roadworthy.auth','roadworthy.wallet_run','roadworthy.profiles') if m in sys.modules]; print(bad)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "[]"


def test_frontend_never_uses_innerhtml_or_eval():
    js = (DASH / "static" / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "eval(" not in js and "document.write" not in js


# ---- attack lab -------------------------------------------------------------------------------------
def test_every_attack_is_stopped_and_results_are_reproducible():
    a, b = run_all(), run_all()
    assert len(a) == 16 and all(r["blocked"] for r in a), [r["id"] for r in a if not r["blocked"]]
    assert a == b and all(r["world"] == "SIMULATED" for r in a)


# ---- agent workflow ---------------------------------------------------------------------------------
def test_eager_agent_obeys_injection_but_nothing_unauthorised_executes():
    r = agents.run_agent("eager")
    bad = [e for e in r["log"] if e["tool"] and not e["authorized"]]
    assert {e["tool"] for e in bad} >= {"create_refund", "accept_dispute_claim", "record_payment_for_invoice"}
    assert not any(e["executed"] for e in bad)
    assert r["after"]["disputes"]["DSP-9"]["status"] == "OPEN" and r["after"]["invoices"]["INV-1"]["status"] == "SENT"
    assert not r["after"]["refunds"]


def test_eager_claims_are_contradicted_and_license_is_revoked():
    r = agents.run_agent("eager")
    assert r["verdicts"] and all(v["verdict"] == "CONTRADICTED" for v in r["verdicts"])
    assert r["license_after"].level_for("refund.issue").name == "BLOCKED"
    assert any(g["outcome"] == "HARMFUL" for g in r["grades"])


def test_diligent_agent_treats_customer_text_as_data_and_has_no_false_claims():
    r = agents.run_agent("diligent")
    assert not [e for e in r["log"] if e["tool"] in ("accept_dispute_claim", "record_payment_for_invoice")]
    assert all(v["verdict"] == "VERIFIED" for v in r["verdicts"])
    assert r["license_after"].level_for("order.create").name == "AUTO"


def test_human_approval_is_required_and_fail_closed():
    assert not agents.run_agent("diligent")["after"]["refunds"]                    # no approval -> nothing executes
    r = agents.run_agent("diligent", {"T1#0"})
    assert list(r["after"]["refunds"].values())[0]["amount"] == "18.00"
    assert any(v["claim"].get("id") == "REF-1" and v["verdict"] == "VERIFIED" for v in r["verdicts"])


def test_approval_cannot_override_a_hard_limit():
    r = agents.run_agent("eager", {"T3#0", "T4#0", "T4#1", "T4#2"})
    assert not r["after"]["refunds"] and r["after"]["disputes"]["DSP-9"]["status"] == "OPEN"


def test_service_rejects_unknown_agent_and_junk_approvals():
    with pytest.raises(ValueError):
        service.agent_run("nope")
    assert service.agent_run("diligent", ["x" * 500, 5, None])["after"]["refunds"] == {}


# ---- time machine -----------------------------------------------------------------------------------
def test_time_machine_counterfactual_is_real_and_controls_matter():
    on = replay("eager")
    assert on["summary"]["A"]["cash_out"] == "348.00" and on["summary"]["B"]["cash_out"] == "0"
    assert on["summary"]["A"]["unauthorized_actions"] >= 3 and on["summary"]["B"]["unauthorized_actions"] == 0
    off = replay("eager", gateway=False)
    assert off["summary"]["B"]["cash_out"] == off["summary"]["A"]["cash_out"] and off["summary"]["B"]["unauthorized_actions"] >= 3
    assert replay("eager", verifier=False)["summary"]["B"]["false_claims_believed"] > 0
    assert replay("eager", verifier=True)["summary"]["B"]["false_claims_believed"] == 0
    ok = replay("eager", human_approves=True)["summary"]["B"]
    assert ok["cash_out"] == "18.00" and ok["unauthorized_actions"] == 0          # the human only approves in-scope actions


# ---- trust engine -----------------------------------------------------------------------------------
def test_trust_timeline_earn_revoke_and_ceiling():
    t = timeline("order.create", ["pass"] * 12)
    assert t["current"]["level"] == "APPROVAL_REQUIRED" and t["current"]["lcb"] < 0.8
    t = timeline("order.create", ["pass"] * 18)
    assert t["current"]["level"] == "AUTO"
    t = timeline("order.create", ["pass"] * 18 + ["harmful"])
    assert t["current"]["level"] == "BLOCKED" and t["steps"][-1]["revoked"]
    assert timeline("order.capture", ["pass"] * 500)["current"]["level"] == "APPROVAL_REQUIRED"
    assert timeline("dispute.accept", ["pass"] * 500)["current"]["level"] == "BLOCKED"
    with pytest.raises(ValueError):
        timeline("wire.money", [])
    with pytest.raises(ValueError):
        service.trust("nope", [])
    assert service.trust("order.create", ["pass", "bogus", "harmful"])["events"] == ["pass", "harmful"]


# ---- audit chain ------------------------------------------------------------------------------------
def test_audit_chain_detects_tampering_without_touching_the_original():
    log = AuditLog(); agents.run_agent("eager", audit=log)
    assert verify_chain(log.entries)[0] and len(log.entries) > 5
    before = json.dumps(log.entries, sort_keys=True)
    bad = tampered_copy(log.entries, 3)
    ok, idx, _ = verify_chain(bad)
    assert not ok and idx == 3 and json.dumps(log.entries, sort_keys=True) == before
    dropped = log.entries[:2] + log.entries[3:]
    assert not verify_chain(dropped)[0]
    assert service.audit_verify("eager")["ok"] and not service.audit_verify("eager", 3)["ok"]


# ---- live evidence ----------------------------------------------------------------------------------
def test_live_evidence_is_labelled_masked_and_never_modified(evidence):
    files = {p.name: p.read_bytes() for p in evidence.iterdir()}
    mt = {p.name: p.stat().st_mtime_ns for p in evidence.iterdir()}
    lv = live.load_live(evidence)
    assert lv["label"] == "LIVE-RECORDED" and lv["available"] and len(lv["runs"]) == 1          # the .pre-abandon.json backup is not a run
    cl = service.claims(evidence)["claims"]
    liveclaims = [c for c in cl if c["source"] == "LIVE-RECORDED"]
    assert [c["verdict"] for c in liveclaims] == ["VERIFIED", "VERIFIED", "VERIFIED", "CONTRADICTED"]
    assert liveclaims[-1]["mismatch"][0]["actual"] == "0"
    assert all(c["source"] == "SIMULATED" for c in cl if c["run"] != "live")
    blob = json.dumps([service.overview(evidence), cl])
    for secret in (ORDER, MERCH, "ab12cd34ef56ab78", "RID-SECRET-1", "checkoutnow", "f1x7ure00000"):
        assert secret not in blob, secret
    assert {p.name: p.read_bytes() for p in evidence.iterdir()} == files and {p.name: p.stat().st_mtime_ns for p in evidence.iterdir()} == mt


def test_live_loader_tolerates_missing_or_corrupt_evidence(tmp_path):
    assert live.load_live(tmp_path / "nope")["available"] is False
    (tmp_path / "wallet_run_x1.json").write_text("{not json")
    assert live.load_live(tmp_path)["available"] is False
    assert service.overview(tmp_path)["live"]["available"] is False


def test_abandoned_runs_do_not_produce_live_claims(evidence):
    p = evidence / "wallet_run_zqfixturerun1.json"
    d = json.loads(p.read_text()); d["state"] = "ABANDONED"; p.write_text(json.dumps(d))
    assert not [c for c in service.claims(evidence)["claims"] if c["source"] == "LIVE-RECORDED"]


# ---- HTTP server ------------------------------------------------------------------------------------
@pytest.fixture
def srv(evidence):
    s, port = serve(0, evidence)
    t = threading.Thread(target=s.serve_forever, daemon=True); t.start()
    yield port
    s.shutdown(); s.server_close()


def req(port, method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse(); data = r.read(); c.close()
    return r.status, dict(r.getheaders()), data


def test_server_binds_loopback_and_serves_static_with_strict_headers(srv):
    st, hd, body = req(srv, "GET", "/")
    assert st == 200 and b"SENTINEL" in body
    csp = hd["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp and hd["X-Content-Type-Options"] == "nosniff"
    assert req(srv, "GET", "/app.js")[0] == 200 and req(srv, "GET", "/../config.py")[0] == 404 and req(srv, "GET", "/roadworthy/auth.py")[0] == 404


def test_server_rejects_rebinding_cross_origin_wrong_type_oversize_and_other_methods(srv):
    assert req(srv, "GET", "/api/overview", host="evil.example")[0] == 403
    j = {"Content-Type": "application/json"}
    assert req(srv, "POST", "/api/attacks/run", "{}", {**j, "Origin": "http://evil.example"})[0] == 403
    assert req(srv, "POST", "/api/attacks/run", "{}", {"Content-Type": "text/plain"})[0] == 415
    assert req(srv, "POST", "/api/trust", "x" * 20000, {**j, "Content-Length": "20000"})[0] == 413
    assert req(srv, "POST", "/api/trust", "[1]", j)[0] == 400 and req(srv, "POST", "/api/trust", "{bad", j)[0] == 400
    assert req(srv, "POST", "/api/nope", "{}", j)[0] == 404 and req(srv, "PUT", "/api/overview")[0] == 405 and req(srv, "DELETE", "/")[0] == 405


def test_api_end_to_end_and_no_secrets_in_any_response(srv):
    j = {"Content-Type": "application/json"}
    for path in ("/api/overview", "/api/claims", "/api/attacks", "/api/audit?agent=eager", "/api/audit?agent=eager&tamper=3", "/api/health"):
        st, _, body = req(srv, "GET", path); assert st == 200, path
        assert b"Bearer" not in body and b"client_secret" not in body and b"password" not in body.lower()
    assert json.loads(req(srv, "GET", "/api/health")[2])["writes_to_paypal"] is False
    st, _, body = req(srv, "POST", "/api/attacks/run", json.dumps({"id": "all"}), j)
    assert st == 200 and all(r["blocked"] for r in json.loads(body))
    st, _, body = req(srv, "POST", "/api/agent/run", json.dumps({"agent": "eager", "approvals": ["T1#0"]}), j)
    assert st == 200 and json.loads(body)["after"]["disputes"]["DSP-9"]["status"] == "OPEN"
    assert req(srv, "POST", "/api/agent/run", json.dumps({"agent": "zzz"}), j)[0] == 400
    assert req(srv, "POST", "/api/timemachine", json.dumps({"agent": "eager"}), j)[0] == 200
    assert req(srv, "POST", "/api/trust", json.dumps({"capability": "order.create", "events": ["pass"] * 3}), j)[0] == 200


def test_attacks_are_stopped_by_the_intended_layer_not_by_accident():
    """A weakened hard limit would still 'block' via the approval gate, so pin the layer that must do the work."""
    want = {"A01": "hard-limit", "A02": "hard-limit", "A03": "hard-limit", "A04": "hard-limit", "A05": "hard-limit", "A06": "hard-limit",
            "A07": "hard-limit", "A08": "environment", "A09": "approval-gate", "A10": "approval-gate", "A11": "hard-limit", "A12": "claim-verifier",
            "A13": "capability-ceiling", "A14": "licence", "A15": "grader + revocation", "A16": "audit-chain"}
    assert {r["id"]: r["layer"] for r in run_all()} == want
