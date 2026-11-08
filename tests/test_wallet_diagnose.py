"""OFFLINE tests for `wallet-run diagnose` (read-only). All PayPal data is MADE UP."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from roadworthy.client import SandboxClient
from roadworthy.wallet_run import CONFIRM_PHRASE, ENV_FLAG, RunStore, WalletRunError, WalletRunner

OID, MERCH = "ORDER1234567890", "USMERCHANT12345"
APPROVE = "https://www.sandbox.paypal.com/checkoutnow?token=" + OID


class FakeWrite:
    def __init__(self): self.reqs = []
    def request(self, method, url, headers, body, timeout):
        b = json.loads(body.decode())
        self.reqs.append(url)
        u = {"custom_id": b["purchase_units"][0]["custom_id"], "amount": {"currency_code": "USD", "value": "2.00"}, "payee": {"merchant_id": MERCH}}
        return 201, json.dumps({"id": OID, "status": "PAYER_ACTION_REQUIRED", "purchase_units": [u],
                                "links": [{"rel": "payer-action", "href": APPROVE}, {"rel": "self", "href": "x"}]}).encode()


class FakeRead:
    def __init__(self): self.urls, self.body, self.status = [], None, 200
    def request(self, method, url, headers, timeout):
        self.urls.append((method, url))
        return self.status, json.dumps(self.body if self.body is not None else {"name": "X"}).encode()


def setup(tmp_path, profile="us"):
    out, wt, rt = [], FakeWrite(), FakeRead()
    pc = {"profile": "us", "status": "OK", "fingerprint": "fp1", "expected_merchant_id": MERCH} if profile == "us" else None
    r = WalletRunner(RunStore(tmp_path), SandboxClient(lambda: "TOKEN-SECRET", transport=rt), lambda: "TOKEN-SECRET", write_transport=wt,
                     env={ENV_FLAG: "1"}, say=out.append, profile=profile, fingerprint="fp1" if pc else None, profile_check=pc)
    r.create(CONFIRM_PHRASE, callback="invalid")
    out.clear()
    rid = RunStore(tmp_path).unfinished()[0]
    return r, wt, rt, out, rid


def order(rid, tmp_path, status="PAYER_ACTION_REQUIRED", merchant=MERCH, created=None, **extra):
    run = RunStore(tmp_path).load(rid)
    u = {"custom_id": run["custom_id"], "amount": {"currency_code": "USD", "value": "2.00"}, "payee": {"merchant_id": merchant}}
    b = {"id": OID, "status": status, "intent": "CAPTURE", "purchase_units": [u],
         "create_time": (created or datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "links": [{"rel": "payer-action", "href": APPROVE}, {"rel": "self", "href": "x"}]}
    b.update(extra)
    return b


def test_diagnose_is_get_only_changes_no_file_and_reports_the_key_facts(tmp_path):
    r, wt, rt, out, rid = setup(tmp_path)
    rt.body = order(rid, tmp_path)
    f = tmp_path / f"wallet_run_{rid}.json"
    before, posts = f.read_bytes(), len(wt.reqs)
    assert r.diagnose(rid) == 0
    text = "\n".join(out)
    assert f.read_bytes() == before and len(wt.reqs) == posts and all(m == "GET" for m, _ in rt.urls)
    assert "status: PAYER_ACTION_REQUIRED" in text and "[PASS] exactly 2.00 USD" in text and "[PASS] vs expected" in text
    assert "buyer-approval link on paypal.com: yes" in text and "carries this order's id: yes" in text
    assert "payer attached to order: no" in text and "captures on order: 0" in text and "min left" in text
    assert "browser-side" in text
    assert OID not in text and "TOKEN-SECRET" not in text                       # masked id, no secrets


def test_diagnose_flags_wrong_merchant_expired_and_attached_payer(tmp_path):
    r, wt, rt, out, rid = setup(tmp_path)
    rt.body = order(rid, tmp_path, merchant="OTHERMERCH9999", created=datetime.now(timezone.utc) - timedelta(hours=4), payer={"payer_id": "P1"})
    r.diagnose(rid)
    text = "\n".join(out)
    assert "[FAIL] vs expected" in text and "EXPIRED" in text and "payer attached to order: yes" in text


def test_diagnose_reports_captures_and_unreadable_orders(tmp_path):
    r, wt, rt, out, rid = setup(tmp_path)
    rt.body = order(rid, tmp_path, status="COMPLETED")
    rt.body["purchase_units"][0]["payments"] = {"captures": [{"id": "CAP1234567"}]}
    r.diagnose(rid)
    assert "captures on order: 1" in "\n".join(out)
    out.clear(); rt.status, rt.body = 404, {"name": "RESOURCE_NOT_FOUND"}
    assert r.diagnose(rid) == 1 and "could not be read" in "\n".join(out)


def test_diagnose_refuses_a_run_of_another_profile(tmp_path):
    r, wt, rt, out, rid = setup(tmp_path)
    india = WalletRunner(RunStore(tmp_path), SandboxClient(lambda: "t", transport=rt), lambda: "t", write_transport=wt, env={}, say=out.append, profile="in")
    gets = len(rt.urls)
    with pytest.raises(WalletRunError):
        india.diagnose(rid)
    assert len(rt.urls) == gets


def test_cli_exposes_diagnose():
    from roadworthy import cli
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        cli.main(["wallet-run", "--help"])
    assert "diagnose" in buf.getvalue()
