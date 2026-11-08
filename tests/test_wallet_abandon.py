"""OFFLINE tests for `wallet-run abandon`. All PayPal responses are MADE UP; nothing contacts PayPal.
abandon READS PayPal (GET) and then edits only a local file. It never POSTs."""
import json

import pytest

from roadworthy.client import SandboxClient
from roadworthy.wallet_run import (ABANDON_REASON, CONFIRM_PHRASE, ENV_FLAG, RunStore, WalletRunError, WalletRunner)

OID, CID = "ORDER1234567890", "CAPTURE12345678"
APPROVE = "https://www.sandbox.paypal.com/checkoutnow?token=" + OID


class FakeWrite:
    def __init__(self, handler): self.handler, self.reqs = handler, []
    def request(self, method, url, headers, body, timeout):
        b = json.loads(body.decode()) if body else None
        self.reqs.append({"method": method, "url": url, "body": b})
        s, d = self.handler(method, url, b)
        return s, json.dumps(d).encode()


class FakeRead:
    def __init__(self, routes): self.routes, self.urls = routes, []
    def request(self, method, url, headers, timeout):
        self.urls.append((method, url))
        s, d = self.routes(url)
        return s, json.dumps(d).encode()


def order_body(cid, status="PAYER_ACTION_REQUIRED", captures=None):
    u = {"custom_id": cid, "amount": {"currency_code": "USD", "value": "2.00"}}
    if captures is not None:
        u["payments"] = {"captures": captures}
    return {"id": OID, "status": status, "intent": "CAPTURE", "purchase_units": [u]}


def default_write(method, url, body):
    if url.endswith("/v2/checkout/orders"):
        cid = body["purchase_units"][0]["custom_id"]
        r = order_body(cid)
        r["links"] = [{"rel": "payer-action", "href": APPROVE}]
        return 201, r
    return 201, {"id": OID, "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": CID, "status": "COMPLETED"}]}}]}


def make(tmp_path, env=None, write=default_write):
    out, wt, rt = [], FakeWrite(write), FakeRead(lambda u: (404, {}))
    r = WalletRunner(RunStore(tmp_path), SandboxClient(lambda: "TOKEN-SECRET", transport=rt), lambda: "TOKEN-SECRET",
                     write_transport=wt, env={ENV_FLAG: "1"} if env is None else env, say=out.append)
    return r, wt, rt, out


def created(tmp_path):
    r, wt, rt, out = make(tmp_path)
    assert r.create(CONFIRM_PHRASE, callback="invalid") == 0
    run_id = RunStore(tmp_path).unfinished()[0]
    run = RunStore(tmp_path).load(run_id)
    rt.routes = lambda u: (200, order_body(run["custom_id"])) if u.endswith(OID) else (404, {})
    return r, wt, rt, out, run_id


def raw(tmp_path, run_id):
    return (tmp_path / f"wallet_run_{run_id}.json").read_bytes()


# ------------------------------------------------------------------ happy path and audit preservation
def test_abandon_marks_state_with_reason_and_preserves_every_original_field(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    before_bytes, before = raw(tmp_path, run_id), RunStore(tmp_path).load(run_id)
    posts_before = len(wt.reqs)
    assert r.abandon(run_id) == 0
    after = RunStore(tmp_path).load(run_id)
    assert after["state"] == "ABANDONED"
    assert after["abandoned"]["reason"] == ABANDON_REASON == "buyer checkout rejected currency"
    assert after["abandoned"]["previous_state"] == "CREATED" and after["abandoned"]["captures_seen"] == 0
    for k, v in before.items():                      # nothing original is removed or rewritten, except the state value
        if k != "state":
            assert after[k] == v, k
    assert after["order_id"] == OID and after["request_id_create"] == before["request_id_create"]
    assert after["request_id_capture"] == before["request_id_capture"] and after["created_at"] == before["created_at"]
    backup = tmp_path / f"wallet_run_{run_id}.pre-abandon.json"
    assert backup.read_bytes() == before_bytes                        # byte-for-byte original preserved
    assert after["history"][-1]["from"] == "CREATED" and after["history"][-1]["to"] == "ABANDONED"
    assert len(wt.reqs) == posts_before                                # no PayPal write
    assert all(m == "GET" for m, _ in rt.urls)


def test_abandon_works_on_a_run_file_that_has_no_history_field(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    run = RunStore(tmp_path).load(run_id)
    run.pop("history", None)
    RunStore(tmp_path).save(run)
    assert r.abandon(run_id) == 0 and len(RunStore(tmp_path).load(run_id)["history"]) == 1


def test_existing_history_is_appended_not_replaced(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    run = RunStore(tmp_path).load(run_id)
    run["history"] = [{"at": "2026-10-30T00:00:00Z", "from": "NEW", "to": "CREATED", "reason": "x"}]
    RunStore(tmp_path).save(run)
    r.abandon(run_id)
    h = RunStore(tmp_path).load(run_id)["history"]
    assert len(h) == 2 and h[0]["reason"] == "x" and h[1]["to"] == "ABANDONED"


def test_custom_reason_is_recorded_but_scrubbed_and_limited(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    r.abandon(run_id, reason="payer buyer@example.com " + "x" * 400)
    reason = RunStore(tmp_path).load(run_id)["abandoned"]["reason"]
    assert "buyer@example.com" not in reason and len(reason) <= 200
    assert reason.startswith("payer")


def test_abandon_needs_no_env_flag_or_phrase_and_never_uses_the_write_transport(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    boom = FakeWrite(lambda m, u, b: (_ for _ in ()).throw(AssertionError("write transport used")))
    r.env, r._wt = {}, boom
    assert r.abandon(run_id) == 0 and boom.reqs == []


# ------------------------------------------------------------------ refusals (fail closed)
def _assert_untouched(tmp_path, run_id, before):
    assert raw(tmp_path, run_id) == before
    assert not (tmp_path / f"wallet_run_{run_id}.pre-abandon.json").exists()


def test_refused_when_paypal_already_shows_a_capture(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    cid = RunStore(tmp_path).load(run_id)["custom_id"]
    before = raw(tmp_path, run_id)
    for body in (order_body(cid, "COMPLETED", [{"id": CID, "status": "COMPLETED"}]),
                 order_body(cid, "COMPLETED"),
                 order_body(cid, "APPROVED", [{"id": CID, "status": "PENDING"}])):
        rt.routes = lambda u, b=body: (200, b)
        with pytest.raises(WalletRunError) as e:
            r.abandon(run_id)
        assert "refused" in str(e.value)
        _assert_untouched(tmp_path, run_id, before)


@pytest.mark.parametrize("status", [404, 401, 429, 500, 503])
def test_refused_when_the_read_fails(tmp_path, status):
    r, wt, rt, out, run_id = created(tmp_path)
    before = raw(tmp_path, run_id)
    rt.routes = lambda u: (status, {"name": "X"})
    with pytest.raises(WalletRunError) as e:
        r.abandon(run_id)
    assert "fail closed" in str(e.value)
    _assert_untouched(tmp_path, run_id, before)


def test_refused_when_the_read_raises(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    before = raw(tmp_path, run_id)
    def boom(url): raise TimeoutError("t")
    rt.routes = boom
    with pytest.raises(Exception):
        r.abandon(run_id)
    _assert_untouched(tmp_path, run_id, before)


@pytest.mark.parametrize("state", ["CAPTURED", "CREATE_FAILED", "CAPTURE_FAILED", "MISMATCH", "NEW", "ABANDONED"])
def test_refused_for_non_unfinished_states(tmp_path, state):
    r, wt, rt, out, run_id = created(tmp_path)
    run = RunStore(tmp_path).load(run_id); run["state"] = state; RunStore(tmp_path).save(run)
    before = raw(tmp_path, run_id)
    with pytest.raises(WalletRunError):
        r.abandon(run_id)
    _assert_untouched(tmp_path, run_id, before)


def test_refused_when_a_capture_id_is_recorded_or_no_order_id_exists(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    run = RunStore(tmp_path).load(run_id); run["capture_id"] = CID; RunStore(tmp_path).save(run)
    before = raw(tmp_path, run_id)
    with pytest.raises(WalletRunError):
        r.abandon(run_id)
    run["capture_id"], run["order_id"], run["state"] = None, None, "CREATE_SENT"; RunStore(tmp_path).save(run)
    before = raw(tmp_path, run_id)
    gets = len(rt.urls)
    with pytest.raises(WalletRunError) as e:
        r.abandon(run_id)
    assert "cannot be proven" in str(e.value) and len(rt.urls) == gets          # no order id -> no read, no abandon
    _assert_untouched(tmp_path, run_id, before)


def test_existing_backup_is_never_overwritten(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    b = tmp_path / f"wallet_run_{run_id}.pre-abandon.json"
    b.write_text("precious")
    before = raw(tmp_path, run_id)
    with pytest.raises(WalletRunError):
        r.abandon(run_id)
    assert b.read_text() == "precious" and raw(tmp_path, run_id) == before


def test_invalid_run_id_is_rejected(tmp_path):
    r, *_ = make(tmp_path)
    for bad in (None, "", "../x", "a b"):
        with pytest.raises(WalletRunError):
            r.abandon(bad)


# ------------------------------------------------------------------ after abandonment
def test_abandoned_run_cannot_be_captured_resumed_or_reused(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    r.abandon(run_id)
    posts, gets = len(wt.reqs), len(rt.urls)
    with pytest.raises(WalletRunError):
        r.capture(CONFIRM_PHRASE, run_id)
    with pytest.raises(WalletRunError):
        r.create(CONFIRM_PHRASE, run_id=run_id)
    with pytest.raises(WalletRunError):
        r.abandon(run_id)
    assert len(wt.reqs) == posts and len(rt.urls) == gets            # refused before any request
    assert RunStore(tmp_path).load(run_id)["state"] == "ABANDONED"


def test_verify_still_works_on_an_abandoned_run_and_is_read_only(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    r.abandon(run_id)
    posts = len(wt.reqs)
    rep = r.verify(run_id)
    assert not rep.passed and len(wt.reqs) == posts


def test_new_run_is_allowed_after_abandon_and_leaves_the_old_file_untouched(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    assert RunStore(tmp_path).unfinished() == [run_id]
    r.abandon(run_id)
    assert RunStore(tmp_path).unfinished() == []
    old = raw(tmp_path, run_id)
    assert r.create(CONFIRM_PHRASE, callback="invalid") == 0
    new_id = RunStore(tmp_path).unfinished()[0]
    assert new_id != run_id and raw(tmp_path, run_id) == old
    assert RunStore(tmp_path).load(new_id)["request_id_create"] != RunStore(tmp_path).load(run_id)["request_id_create"]


def test_abandon_files_contain_no_credentials(tmp_path):
    r, wt, rt, out, run_id = created(tmp_path)
    r.abandon(run_id)
    for f in tmp_path.glob("*.json"):
        t = f.read_text()
        assert "TOKEN-SECRET" not in t and "Bearer" not in t and "@" not in t


# ------------------------------------------------------------------ CLI end to end (fake client, tmp evidence dir)
def test_cli_abandon_end_to_end(tmp_path, monkeypatch, capsys):
    r, wt, rt, out, run_id = created(tmp_path)
    from roadworthy import cli, wallet_run
    class TP:
        get = staticmethod(lambda: "t")
        fingerprint = staticmethod(lambda: None)
    monkeypatch.setattr(cli, "_client", lambda profile="in": (SandboxClient(lambda: "t", transport=rt), TP()))
    monkeypatch.setattr(wallet_run, "PROJECT_ROOT", tmp_path.parent)
    monkeypatch.setattr(wallet_run.RunStore, "__init__", lambda self, directory=None: setattr(self, "dir", tmp_path))
    assert cli.main(["wallet-run", "abandon", "--profile", "in", "--run", run_id]) == 0
    assert RunStore(tmp_path).load(run_id)["state"] == "ABANDONED"
    assert "ABANDONED" in capsys.readouterr().out
    assert cli.main(["wallet-run", "abandon", "--profile", "in", "--run", run_id]) == 1            # second time refused
    assert cli.main(["wallet-run", "abandon", "--profile", "in"]) == 1                              # --run required
