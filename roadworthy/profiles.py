"""Named sandbox credential profiles, so two merchants can never be mixed by accident.

  in  = the original India merchant (legacy). File paypal-sandbox-credentials.xml, evidence in evidence\\
  us  = the US merchant.                       File paypal-sandbox-credentials-us.xml, evidence in evidence\\us\\

Rules (fail closed, all tested):
  * A profile reads ONLY its own encrypted file, using the SAME DPAPI loader as before. There is never a fallback
    to another profile's file, and named non-legacy profiles refuse the environment-variable credential source.
  * Each run file records its profile and a fingerprint of the Client ID (a hash, never the ID or secret). A run can
    only be used with the profile and credentials that created it.
  * Evidence lives in a different folder per profile, so unfinished-run checks never cross profiles.
  * A non-legacy profile must pass `profile-check` (OAuth + read-only merchant checks + the Merchant ID you
    supply) before any wallet-run write is allowed, and the order's payee must equal that Merchant ID.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .auth import AuthError, Credentials, load_credentials
from .config import DEFAULT_CRED_FILE, PROJECT_ROOT


class ProfileError(AuthError):
    pass


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    label: str
    cred_file: Path
    evidence_dir: Path
    legacy: bool


PROFILES: Dict[str, ProfileSpec] = {
    "in": ProfileSpec("in", "India merchant (legacy)", DEFAULT_CRED_FILE, PROJECT_ROOT / "evidence", True),
    "us": ProfileSpec("us", "US merchant", PROJECT_ROOT / "paypal-sandbox-credentials-us.xml", PROJECT_ROOT / "evidence" / "us", False),
}


def get_profile(name: str, specs: Optional[Dict[str, ProfileSpec]] = None) -> ProfileSpec:
    specs = PROFILES if specs is None else specs
    if name not in specs:
        raise ProfileError(f"unknown profile {name!r}; choose one of {sorted(specs)}")
    return specs[name]


def load_profile_credentials(name: str, runner: Optional[Callable] = None, env: Optional[Dict[str, str]] = None,
                             specs: Optional[Dict[str, ProfileSpec]] = None) -> Credentials:
    specs = PROFILES if specs is None else specs
    spec = get_profile(name, specs)
    env = os.environ if env is None else env
    if not spec.legacy:
        if env.get("ROADWORTHY_CRED_SOURCE", "").lower() == "env":
            raise ProfileError(f"profile '{name}' does not accept the environment-variable credential source "
                               "(it could silently supply another merchant's keys). Unset ROADWORTHY_CRED_SOURCE.")
        legacy_files = {s.cred_file.resolve() for s in specs.values() if s.legacy}
        if spec.cred_file.resolve() in legacy_files:
            raise ProfileError("a non-legacy profile may not point at a legacy profile's credential file")
        if not spec.cred_file.is_file():
            raise ProfileError(f"profile '{name}' has no credential file ({spec.cred_file.name}). "
                               "Create it with scripts\\set-us-credentials.ps1. There is no fallback to another profile.")
    return load_credentials(spec.cred_file, runner)


# ---------------------------------------------------------------------------------- profile check state
def check_path(spec: ProfileSpec) -> Path:
    return spec.evidence_dir / "profile_check.json"


def load_check(spec: ProfileSpec) -> Optional[Dict[str, Any]]:
    if spec.legacy:
        return None
    p = check_path(spec)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_check(spec: ProfileSpec, data: Dict[str, Any]) -> None:
    spec.evidence_dir.mkdir(parents=True, exist_ok=True)
    p = check_path(spec)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def mask(value: str) -> str:
    return "***" + value[-4:] if value and len(value) > 4 else "***"


def run_profile_check(client, tp, spec: ProfileSpec, expect_merchant_id: Optional[str], say: Callable[[str], None] = print,
                      specs: Optional[Dict[str, ProfileSpec]] = None, runner: Optional[Callable] = None,
                      env: Optional[Dict[str, str]] = None, now: Optional[datetime] = None) -> bool:
    """Read-only checks (one OAuth token request plus GETs). Prints masked values only. Writes profile_check.json."""
    from .txsearch import search_transactions
    specs = PROFILES if specs is None else specs
    results: List[tuple] = []
    add = lambda name, status, detail="": (results.append((name, status, detail)), say(f"  [{status}] {name}" + (f"  ({detail})" if detail else "")))
    say(f"profile-check: {spec.name} ({spec.label})")

    fp = tp.fingerprint()
    tp.get()
    add("sandbox OAuth token obtained", "PASS", f"scopes={tp.info.scope_count}")
    add("refund scope present in token (needed only for a later refund experiment)", "PASS" if tp.info.has_refund_scope else "WARN",
        "YES" if tp.info.has_refund_scope else "NO")

    # mixing guard: this profile must not hold the same Client ID as any other profile
    clash = False
    for other in specs.values():
        if other.name == spec.name:
            continue
        try:
            ofp = load_profile_credentials(other.name, runner=runner, env=env, specs=specs).fingerprint()
        except AuthError:
            continue
        if ofp == fp:
            clash = True
    add("Client ID differs from every other profile's", "FAIL" if clash else "PASS",
        "same Client ID found in another profile - you saved the wrong app's keys" if clash else "")

    ts = search_transactions(client, end=now, days=1)
    add("Transaction Search (read-only) reachable", "PASS" if ts.status_code == 200 else "WARN", f"http {ts.status_code}")

    # balances: currencies held and, where PayPal returns it, the account (merchant) id
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    bal = None
    for cur in ("*", "USD"):
        r = client.get("/v1/reporting/balances", {"as_of_time": stamp, "currency_code": cur})
        if r.ok and isinstance(r.body, dict):
            bal = r
            break
    currencies: List[str] = []
    account_id: Optional[str] = None
    if bal is not None:
        account_id = bal.body.get("account_id")
        for b in bal.body.get("balances") or []:
            if not isinstance(b, dict):
                continue
            c = b.get("currency") or (b.get("total_balance") or {}).get("currency_code")
            if c:
                currencies.append(str(c))
        currencies = sorted(set(currencies))
        add("balances report (read-only)", "PASS", f"http {bal.status}; currencies held: {currencies or 'none reported'}")
    else:
        add("balances report (read-only)", "WARN", "not available to this app; merchant id will be enforced on the order payee instead")

    merchant_source, merchant_match = "none", None
    if account_id and expect_merchant_id:
        merchant_source, merchant_match = "balances.account_id", account_id == expect_merchant_id
        add("Merchant ID matches the one you supplied (balances.account_id)", "PASS" if merchant_match else "FAIL",
            f"reported {mask(str(account_id))}, supplied {mask(expect_merchant_id)}")
    else:
        add("Merchant ID comparison against balances", "NOT_AVAILABLE",
            "no account_id returned" if not account_id else "no expected id supplied")

    if not spec.legacy and not expect_merchant_id:
        add("expected Merchant ID supplied", "FAIL", "pass --expect-merchant-id (from the Business account profile)")
    ok = all(s != "FAIL" for _, s, _ in results)
    data = {"profile": spec.name, "fingerprint": fp, "checked_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "OK" if ok else "FAILED", "expected_merchant_id": expect_merchant_id, "merchant_source": merchant_source,
            "merchant_match": merchant_match, "currencies_held": currencies, "refund_scope": bool(tp.info.has_refund_scope),
            "txsearch_http": ts.status_code}
    if not spec.legacy:
        save_check(spec, data)
    say(f"profile-check: {'OK' if ok else 'FAILED'}" + ("" if spec.legacy else f"  (recorded in {check_path(spec).name}; no secrets stored)"))
    if ok and not spec.legacy and merchant_source == "none":
        say("note: PayPal did not return the account id, so the Merchant ID is enforced on each order's payee (create and capture).")
    return ok
