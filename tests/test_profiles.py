"""OFFLINE tests for credential profiles (India legacy vs US). All data is MADE UP; nothing contacts PayPal."""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from roadworthy import profiles
from roadworthy.auth import AccessToken, AuthError, Credentials, TokenProvider
from roadworthy.client import SandboxClient
from roadworthy.config import DEFAULT_CRED_FILE
from roadworthy.profiles import PROFILES, ProfileError, ProfileSpec, get_profile, load_profile_credentials, run_profile_check
from roadworthy.wallet_run import CONFIRM_PHRASE, ENV_FLAG, RunStore, WalletRunError, WalletRunner

ROOT = Path(__file__).resolve().parent.parent
MERCH_US, MERCH_IN = "USMERCHANT12345", "INMERCHANT12345"
OID = "ORDER1234567890"
APPROVE = "https://www.sandbox.paypal.com/checkoutnow?token=" + OID
NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


# ------------------------------------------------------------------ helpers
def specs_in(tmp_path):
    return {"in": ProfileSpec("in", "India", tmp_path / "in.xml", tmp_path / "ev", True),
            "us": ProfileSpec("us", "US", tmp_path / "us.xml", tmp_path / "ev" / "us", False)}


class CP:
    returncode, stderr = 0, ""
    def __init__(self, cid): self.stdout = json.dumps({"ok": True, "client_id": cid, "client_secret": "sec-" + cid})


def fake_runner(ids):
    """ids: {filename: client_id}. Reads which file PowerShell would be asked to decrypt."""
    calls = []
    def run(cmd, env=None, **k):
        name = Path(env["RW_CRED_PATH"]).name
        calls.append(name)
        return CP(ids[name])
    run.calls = calls
    return run


def make_files(tmp_path, us=True, inn=True):
    s = specs_in(tmp_path)
    if inn: s["in"].cred_file.write_text("INDIA-ORIGINAL")
    if us: s["us"].cred_file.write_text("US-ORIGINAL")
    return s


class FakeRead:
    def __init__(self, routes): self.routes, self.urls = routes, []
    def request(self, method, url, headers, timeout):
        self.urls.append((method, url))
        s, d = self.routes(url)
        return s, json.dumps(d).encode()


def tp_for(cid="us-client-id-0001", scope=True):
    return TokenProvider(Credentials(cid, "sec-" + cid), fetch=lambda c: AccessToken("tok", time.time() + 1000, 3, scope))


def routes(account_id=MERCH_US, balances=("USD",), bal_status=200):
    def r(url):
        if "/v1/reporting/transactions" in url:
            return 200, {"transaction_details": [], "total_pages": 1}
        if "/v1/reporting/balances" in url:
            if bal_status != 200:
                return bal_status, {"name": "NOT_AUTHORIZED"}
            body = {"balances": [{"currency": c, "total_balance": {"currency_code": c, "value": "100.00"}} for c in balances]}
            if account_id: body["account_id"] = account_id
            return 200, body
        return 404, {}
    return r


# ------------------------------------------------------------------ registry and loading
def test_registry_keeps_india_file_and_gives_us_its_own_file_and_evidence_folder():
    assert PROFILES["in"].cred_file == DEFAULT_CRED_FILE and PROFILES["in"].legacy
    assert PROFILES["us"].cred_file.name == "paypal-sandbox-credentials-us.xml" and not PROFILES["us"].legacy
    assert PROFILES["us"].cred_file != PROFILES["in"].cred_file
    assert PROFILES["us"].evidence_dir != PROFILES["in"].evidence_dir and PROFILES["us"].evidence_dir.parent == PROFILES["in"].evidence_dir


def test_both_credential_files_are_git_ignored():
    gi = (ROOT / ".gitignore").read_text()
    assert "*.xml" in gi and "evidence/" in gi


def test_unknown_profile_is_refused():
    with pytest.raises(ProfileError):
        get_profile("eu")


def test_us_loads_only_its_own_file_with_the_existing_loader(tmp_path):
    s = make_files(tmp_path)
    run = fake_runner({"us.xml": "us-client-id-0001", "in.xml": "in-client-id-0001"})
    creds = load_profile_credentials("us", runner=run, env={}, specs=s)
    assert run.calls == ["us.xml"] and creds.client_id == "us-client-id-0001" and "sec-us" not in repr(creds)
    assert s["in"].cred_file.read_text() == "INDIA-ORIGINAL"             # India file never touched


def test_us_fails_closed_when_its_file_is_missing_and_never_falls_back_to_india(tmp_path):
    s = make_files(tmp_path, us=False)
    run = fake_runner({"in.xml": "in-client-id-0001"})
    with pytest.raises(ProfileError) as e:
        load_profile_credentials("us", runner=run, env={}, specs=s)
    assert "set-us-credentials" in str(e.value) and run.calls == []     # PowerShell never even invoked


def test_us_refuses_the_environment_variable_source(tmp_path):
    s = make_files(tmp_path)
    run = fake_runner({"us.xml": "x" * 12})
    with pytest.raises(ProfileError):
        load_profile_credentials("us", runner=run, env={"ROADWORTHY_CRED_SOURCE": "env", "PAYPAL_CLIENT_ID": "a", "PAYPAL_CLIENT_SECRET": "b"}, specs=s)
    assert run.calls == []


def test_a_non_legacy_profile_cannot_point_at_the_legacy_file(tmp_path):
    s = make_files(tmp_path)
    s["us"] = ProfileSpec("us", "US", s["in"].cred_file, tmp_path / "ev" / "us", False)
    with pytest.raises(ProfileError):
        load_profile_credentials("us", runner=fake_runner({}), env={}, specs=s)


def test_legacy_profile_still_uses_the_original_loader_and_path(monkeypatch):
    seen = {}
    monkeypatch.setattr(profiles, "load_credentials", lambda path, runner=None: seen.setdefault("path", path) or Credentials("c" * 10, "s" * 10))
    load_profile_credentials("in")
    assert seen["path"] == DEFAULT_CRED_FILE


def test_fingerprint_is_a_short_hash_that_hides_the_id_and_secret():
    c = Credentials("my-client-id-ABCDEFG", "my-secret-value-123")
    fp = c.fingerprint()
    assert re.fullmatch(r"[0-9a-f]{12}", fp) and "ABCDEFG" not in fp
    assert fp == Credentials("my-client-id-ABCDEFG", "other").fingerprint() != Credentials("different-client-id", "x").fingerprint()
    assert "my-secret" not in repr(c) and TokenProvider(c).fingerprint() == fp


# ------------------------------------------------------------------ profile-check
def check(tmp_path, expect=MERCH_US, rt=None, ids=None, cid="us-client-id-0001", scope=True, **rk):
    s = make_files(tmp_path)
    out = []
    rt = rt or FakeRead(routes(**rk))
    ids = ids or {"us.xml": cid, "in.xml": "in-client-id-0001"}
    ok = run_profile_check(SandboxClient(lambda: "tok", transport=rt), tp_for(cid, scope), s["us"], expect, say=out.append,
                           specs=s, runner=fake_runner(ids), env={}, now=NOW)
    return ok, out, s, rt


def test_profile_check_passes_and_records_no_secrets(tmp_path):
    ok, out, s, rt = check(tmp_path)
    assert ok
    rec = json.loads((s["us"].evidence_dir / "profile_check.json").read_text())
    assert rec["status"] == "OK" and rec["expected_merchant_id"] == MERCH_US and rec["merchant_source"] == "balances.account_id"
    assert rec["merchant_match"] is True and rec["currencies_held"] == ["USD"] and rec["profile"] == "us"
    text = json.dumps(rec) + "\n".join(out)
    assert "sec-" not in text and "tok" not in rec.values() and "us-client-id-0001" not in text
    assert all(m == "GET" for m, _ in rt.urls)                              # only reads (token POST is in the auth module)


def test_profile_check_fails_on_merchant_mismatch(tmp_path):
    ok, out, s, _ = check(tmp_path, account_id=MERCH_IN)
    assert not ok and json.loads((s["us"].evidence_dir / "profile_check.json").read_text())["status"] == "FAILED"
    assert any("FAIL" in l and "Merchant ID" in l for l in out)


def test_profile_check_needs_an_expected_merchant_id_for_us(tmp_path):
    ok, out, s, _ = check(tmp_path, expect=None)
    assert not ok


def test_profile_check_without_account_id_defers_to_the_order_payee(tmp_path):
    ok, out, s, _ = check(tmp_path, account_id=None)
    rec = json.loads((s["us"].evidence_dir / "profile_check.json").read_text())
    assert ok and rec["merchant_source"] == "none" and rec["merchant_match"] is None
    assert any("payee" in l for l in out)


def test_profile_check_tolerates_unavailable_balances(tmp_path):
    ok, out, s, _ = check(tmp_path, bal_status=403)
    assert ok and any("WARN" in l and "balances" in l for l in out)


def test_profile_check_detects_the_same_client_id_in_both_profiles(tmp_path):
    ok, out, s, _ = check(tmp_path, ids={"us.xml": "shared-client-id-1", "in.xml": "shared-client-id-1"}, cid="shared-client-id-1")
    assert not ok and any("FAIL" in l and "differs from every other" in l for l in out)


def test_refund_scope_is_reported_as_a_warning_not_a_failure(tmp_path):
    ok, out, s, _ = check(tmp_path, scope=False)
    assert ok and any("WARN" in l and "refund scope" in l for l in out)


# ------------------------------------------------------------------ wallet-run isolation
ENV_ON = {ENV_FLAG: "1"}


class FakeWrite:
    def __init__(self, merchant=MERCH_US): self.reqs, self.merchant = [], merchant
    def request(self, method, url, headers, body, timeout):
        b = json.loads(body.decode()) if body else None
        self.reqs.append((method, url))
        if url.endswith("/v2/checkout/orders"):
            u = {"custom_id": b["purchase_units"][0]["custom_id"], "amount": {"currency_code": "USD", "value": "2.00"},
                 "payee": {"merchant_id": self.merchant}}
            return 201, json.dumps({"id": OID, "status": "PAYER_ACTION_REQUIRED", "purchase_units": [u],
                                    "links": [{"rel": "payer-action", "href": APPROVE}]}).encode()
        return 201, json.dumps({"purchase_units": [{"payments": {"captures": [{"id": "CAPTURE12345678", "status": "COMPLETED"}]}}]}).encode()


def good_check(fp="fp-us-000001", **kw):
    d = {"profile": "us", "status": "OK", "fingerprint": fp, "expected_merchant_id": MERCH_US}
    d.update(kw)
    return d


def runner(tmp_path, profile="us", fp="fp-us-000001", pc="default", merchant=MERCH_US, order_merchant=MERCH_US, sub="us"):
    wt, out = FakeWrite(merchant), []
    state = {}
    def routes_(url):
        if url.endswith(OID):
            u = {"custom_id": state.get("cid", "?"), "amount": {"currency_code": "USD", "value": "2.00"}}
            if order_merchant: u["payee"] = {"merchant_id": order_merchant}
            return 200, {"id": OID, "status": "APPROVED", "intent": "CAPTURE", "purchase_units": [u]}
        return 404, {}
    rt = FakeRead(routes_)
    store = RunStore(tmp_path / sub)
    r = WalletRunner(store, SandboxClient(lambda: "t", transport=rt), lambda: "t", write_transport=wt, env=ENV_ON, say=out.append,
                     profile=profile, fingerprint=fp, profile_check=good_check(fp) if pc == "default" else pc)
    return r, wt, rt, store, state, out


def test_us_create_is_refused_without_a_passing_profile_check_and_sends_nothing(tmp_path):
    bad = [None, {}, good_check(status="FAILED"), good_check(fp="other-fp"), good_check(expected_merchant_id=""), good_check(profile="in")]
    for pc in bad:
        r, wt, rt, store, state, out = runner(tmp_path, pc=pc)
        with pytest.raises(WalletRunError):
            r.create(CONFIRM_PHRASE, callback="invalid")
        assert wt.reqs == []
        assert not (tmp_path / "us").exists() or list((tmp_path / "us").glob("*")) == []     # no run file was created either


def test_us_run_records_profile_and_fingerprint_and_lives_in_its_own_folder(tmp_path):
    r, wt, rt, store, state, out = runner(tmp_path)
    assert r.create(CONFIRM_PHRASE, callback="invalid") == 0
    rid = store.unfinished()[0]
    run = store.load(rid)
    assert run["profile"] == "us" and run["credential_fingerprint"] == "fp-us-000001" and run["payee_merchant_id"] == MERCH_US
    assert (tmp_path / "us" / f"wallet_run_{rid}.json").exists()
    assert RunStore(tmp_path / "in").unfinished() == []                      # the India folder sees nothing


def test_unfinished_runs_never_cross_profiles(tmp_path):
    r, *_ = runner(tmp_path)
    r.create(CONFIRM_PHRASE, callback="invalid")
    legacy, wt2, *_ = runner(tmp_path, profile="in", fp="fp-in-000001", pc=None, sub="in")
    assert legacy.create(CONFIRM_PHRASE, callback="invalid") == 0            # not blocked by the unfinished US run


def test_a_us_run_cannot_be_used_by_the_india_profile_and_vice_versa(tmp_path):
    us, wt, rt, store, state, out = runner(tmp_path)
    us.create(CONFIRM_PHRASE, callback="invalid")
    rid = store.unfinished()[0]
    india = WalletRunner(store, SandboxClient(lambda: "t", transport=rt), lambda: "t", write_transport=wt, env=ENV_ON, say=lambda s: None,
                         profile="in", fingerprint="fp-in-000001")
    posts, gets = len(wt.reqs), len(rt.urls)
    for call in (lambda: india.capture(CONFIRM_PHRASE, rid), lambda: india.abandon(rid), lambda: india.verify(rid),
                 lambda: india.create(CONFIRM_PHRASE, run_id=rid)):
        with pytest.raises(WalletRunError) as e:
            call()
        assert "belongs to profile 'us'" in str(e.value)
    assert (len(wt.reqs), len(rt.urls)) == (posts, gets)
    # and a legacy run file (no profile field) is an India run: refused under 'us'
    legacy = {"run_id": "rlegacy000001", "state": "CREATED", "order_id": OID, "custom_id": "SENTINEL-x"}
    store.save(legacy)
    with pytest.raises(WalletRunError):
        us.abandon("rlegacy000001")


def test_changed_credentials_are_refused_for_an_existing_run(tmp_path):
    us, wt, rt, store, state, out = runner(tmp_path)
    us.create(CONFIRM_PHRASE, callback="invalid")
    rid = store.unfinished()[0]
    swapped = WalletRunner(store, SandboxClient(lambda: "t", transport=rt), lambda: "t", write_transport=wt, env=ENV_ON, say=lambda s: None,
                           profile="us", fingerprint="fp-us-DIFFERENT", profile_check=good_check("fp-us-DIFFERENT"))
    with pytest.raises(WalletRunError) as e:
        swapped.capture(CONFIRM_PHRASE, rid)
    assert "credentials now loaded differ" in str(e.value) and len(wt.reqs) == 1


def test_create_with_the_wrong_payee_merchant_stops_and_marks_mismatch(tmp_path):
    r, wt, rt, store, state, out = runner(tmp_path, merchant=MERCH_IN)
    with pytest.raises(WalletRunError) as e:
        r.create(CONFIRM_PHRASE, callback="invalid")
    assert "NOT captured" in str(e.value)
    rid = next((tmp_path / "us").glob("wallet_run_*.json")).stem.replace("wallet_run_", "")
    assert store.load(rid)["state"] == "MISMATCH" and store.unfinished() == []


def test_capture_refuses_when_the_order_payee_is_wrong_or_missing(tmp_path):
    for order_merchant in (MERCH_IN, None):
        r, wt, rt, store, state, out = runner(tmp_path / str(order_merchant), order_merchant=order_merchant)
        r.create(CONFIRM_PHRASE, callback="invalid")
        rid = store.unfinished()[0]
        state["cid"] = store.load(rid)["custom_id"]
        with pytest.raises(WalletRunError) as e:
            r.capture(CONFIRM_PHRASE, rid)
        assert "payee merchant id" in str(e.value) and len(wt.reqs) == 1        # only the create POST


def test_capture_succeeds_when_the_payee_matches_the_profile_merchant(tmp_path):
    r, wt, rt, store, state, out = runner(tmp_path)
    r.create(CONFIRM_PHRASE, callback="invalid")
    rid = store.unfinished()[0]
    state["cid"] = store.load(rid)["custom_id"]
    assert r.capture(CONFIRM_PHRASE, rid) == 0 and len(wt.reqs) == 2
    assert not any("refund" in u for _, u in wt.reqs)


def test_india_legacy_behaviour_is_unchanged(tmp_path):
    r, wt, rt, store, state, out = runner(tmp_path, profile="in", fp=None, pc=None, merchant="ANYTHING", order_merchant="ANYTHING", sub="in")
    assert r.create(CONFIRM_PHRASE, callback="invalid") == 0                     # no profile-check or merchant needed for legacy
    rid = store.unfinished()[0]
    state["cid"] = store.load(rid)["custom_id"]
    assert r.capture(CONFIRM_PHRASE, rid) == 0


# ------------------------------------------------------------------ CLI and setup script
def test_wallet_run_requires_an_explicit_profile():
    from roadworthy import cli
    import contextlib, io
    for argv in (["wallet-run", "create"], ["wallet-run", "verify", "--run", "r1"], ["profile-check"]):
        with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
            cli.main(argv)
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        cli.main(["wallet-run", "create", "--profile", "eu"])


def test_cli_fails_closed_for_us_when_the_credential_file_is_missing(tmp_path, monkeypatch, capsys):
    from roadworthy import cli
    s = make_files(tmp_path, us=False)
    monkeypatch.setitem(PROFILES, "us", s["us"])
    monkeypatch.delenv("ROADWORTHY_CRED_SOURCE", raising=False)
    assert cli.main(["wallet-run", "create", "--profile", "us", "--confirm", CONFIRM_PHRASE]) == 2
    assert "no credential file" in capsys.readouterr().err
    assert cli.main(["profile-check", "--profile", "us", "--expect-merchant-id", MERCH_US]) == 2


def test_existing_read_only_commands_default_to_the_india_profile(monkeypatch):
    from roadworthy import cli
    seen = []
    monkeypatch.setattr(cli, "_client", lambda profile="in": (seen.append(profile), (_ for _ in ()).throw(AuthError("stop")))[1])
    for argv in (["check-auth"], ["txsearch"], ["snapshot"], ["capture", "--id", "CAPTURE12345678"]):
        assert cli.main(argv) == 2
    assert seen == ["in"] * 4


def test_setup_script_keeps_secrets_hidden_encrypted_and_off_the_india_file():
    ps = (ROOT / "scripts" / "set-us-credentials.ps1").read_text(encoding="utf-8")
    code = re.sub(r"<#.*?#>", "", ps, flags=re.S)                           # ignore the help block
    assert code.count("Read-Host") == 2 and code.count("-AsSecureString") == 2
    assert "Export-Clixml" in code and "paypal-sandbox-credentials-us.xml" in code
    assert "paypal-sandbox-credentials.xml'" not in code                    # never opens or writes the India file
    for forbidden in ("AsPlainText", "ConvertFrom-SecureString", "ConvertTo-SecureString", "Write-Output", "Out-File", "Set-Content",
                      "Add-Content", "Start-Transcript", "$env:", "BSTR"):
        assert forbidden not in code, forbidden
    assert not re.search(r"Write-Host[^\n]*\$(id|sec)\b", code)             # never prints the values
    assert "-not $Force" in code and "exit 1" in code                       # will not overwrite silently
    assert not re.search(r"[A-Za-z0-9_-]{30,}", re.sub(r"https?://\S+", "", code).replace("paypal-sandbox-credentials-us.xml", ""))   # no embedded secrets


def test_setup_script_output_format_is_loadable_by_the_existing_loader_logic():
    """The loader decrypts properties named like client.?id and secret/pass/key. The script writes ClientId / ClientSecret."""
    from roadworthy import auth
    ps_loader = auth._PS_SCRIPT
    assert "client.?id|^user" in ps_loader and "secret|pass|key" in ps_loader
    assert re.search(r"client.?id|^user", "ClientId", re.I) and re.search(r"secret|pass|key", "ClientSecret", re.I)
    assert not re.search(r"client.?id|^user", "ClientSecret", re.I)
