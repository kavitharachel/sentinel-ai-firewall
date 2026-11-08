"""OFFLINE tests for `wallet-run create --dry-run`. A tripwire fails the test immediately if anything tries to reach the network,
request an OAuth token, decrypt credentials, start a subprocess, or create/read a run store. No PayPal result is involved."""
import socket
import subprocess
import urllib.request

import pytest

from roadworthy import auth, cli, profiles, wallet_run
from roadworthy.wallet_run import (CONFIRM_PHRASE, ENV_FLAG, WalletRunError, WriteTransport, dry_run_create, format_dry_run)


@pytest.fixture
def tripwire(monkeypatch):
    hits = []

    def boom(name):
        def f(*a, **k):
            hits.append(name)
            raise AssertionError(f"dry-run attempted forbidden operation: {name}")
        return f
    for obj, attr, name in [
        (socket.socket, "connect", "socket.connect"), (socket, "create_connection", "socket.create_connection"),
        (socket, "getaddrinfo", "dns lookup"), (urllib.request, "urlopen", "urllib urlopen"),
        (subprocess, "run", "subprocess.run (credential decryption)"), (subprocess, "Popen", "subprocess.Popen"),
        (WriteTransport, "request", "WriteTransport.request"), (auth, "fetch_token", "OAuth token request"),
        (auth, "load_credentials", "credential load"), (profiles, "load_profile_credentials", "credential load"),
        (auth.TokenProvider, "get", "token provider"), (cli, "_client", "client construction"),
        (wallet_run.RunStore, "__init__", "run store created"), (wallet_run.WalletRunner, "__init__", "wallet runner created"),
        (wallet_run, "WriteGuardedClient", None)]:
        if name:
            monkeypatch.setattr(obj, attr, boom(name))
    return hits


# ------------------------------------------------------------------ the proposed $2 USD order
def test_two_usd_order_requires_human_approval_and_sends_nothing(tripwire):
    r = dry_run_create()
    assert (r.verdict, r.gateway_action, r.gateway_level) == ("REQUIRE_APPROVAL", "REQUIRE_APPROVAL", "APPROVAL_REQUIRED")
    assert r.request_guard == "OK" and not r.blocked and not r.what_if and r.requests_sent == 0
    amt = r.body["purchase_units"][0]["amount"]
    assert (amt["value"], amt["currency_code"]) == ("2.00", "USD") and r.body["intent"] == "CAPTURE"
    assert tripwire == []


def test_dry_run_is_never_allow_even_with_env_flag_and_phrase_in_the_environment(tripwire, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    assert dry_run_create().verdict == "REQUIRE_APPROVAL"          # nothing in the environment can turn a dry-run into ALLOW
    assert tripwire == []


# ------------------------------------------------------------------ EUR / NaN / other amounts can never be authorised
@pytest.mark.parametrize("cur,amt,verdict", [("EUR", None, "DENY"), ("eur", None, "DENY"), ("GBP", "2.00", "DENY"), ("", None, "DENY"),
                                             (None, "NaN", "REFUSED_BY_REQUEST_GUARD"), (None, "nan", "REFUSED_BY_REQUEST_GUARD"),
                                             (None, "Infinity", "REFUSED_BY_REQUEST_GUARD"), (None, "-2.00", "REFUSED_BY_REQUEST_GUARD"),
                                             (None, "0", "DENY"), (None, "2.01", "REFUSED_BY_REQUEST_GUARD"),
                                             (None, "", "REFUSED_BY_REQUEST_GUARD"), (None, "1e1", "REFUSED_BY_REQUEST_GUARD"),
                                             ("EUR", "NaN", "DENY")])
def test_foreign_currency_and_non_finite_or_wrong_amounts_are_blocked(tripwire, cur, amt, verdict):
    r = dry_run_create(cur, amt)
    assert r.verdict == verdict and r.blocked and r.what_if and r.requests_sent == 0
    assert r.verdict != "ALLOW" and r.gateway_action != "ALLOW"
    assert tripwire == []


# ------------------------------------------------------------------ CLI path
def run_cli(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_cli_dry_run_prints_decision_and_touches_nothing(tripwire, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for prof in ("in", "us"):
        rc, out, err = run_cli(["wallet-run", "create", "--profile", prof, "--dry-run"], capsys)
        assert rc == 0 and "VERDICT        : REQUIRE_APPROVAL" in out and "requests sent  : 0" in out and err == ""
    assert list(tmp_path.rglob("*")) == [] and tripwire == []


@pytest.mark.parametrize("extra", [["--what-if-currency", "EUR"], ["--what-if-amount", "NaN"]])
def test_cli_what_if_is_blocked_with_a_nonzero_exit(tripwire, extra, capsys):
    rc, out, _ = run_cli(["wallet-run", "create", "--profile", "us", "--dry-run", *extra], capsys)
    assert rc == 3 and "no approval can override this" in out and tripwire == []


def test_cli_rejects_misuse_without_touching_anything(tripwire, capsys):
    for argv in (["wallet-run", "create", "--profile", "us", "--what-if-currency", "EUR"],            # what-if without --dry-run
                 ["wallet-run", "capture", "--profile", "us", "--dry-run", "--run", "r1"],              # not create
                 ["wallet-run", "create", "--profile", "us", "--dry-run", "--confirm", CONFIRM_PHRASE],  # never combine with the phrase
                 ["wallet-run", "create", "--profile", "us", "--dry-run", "--run", "r1"]):
        rc, out, err = run_cli(argv, capsys)
        assert rc == 2 and out == "" and "WALLET-RUN" in err
    assert tripwire == []


def test_the_tripwire_and_guards_really_fail_fast():
    with pytest.raises(WalletRunError):
        wallet_run._NoNetwork().request("POST", "https://api-m.sandbox.paypal.com/v2/checkout/orders", {}, b"", 1.0)
    with pytest.raises(WalletRunError):
        wallet_run._no_token()


def test_formatting_names_the_sandbox_safeguards():
    txt = format_dry_run(dry_run_create())
    assert "nothing was sent" in txt and CONFIRM_PHRASE not in txt and ENV_FLAG in txt
