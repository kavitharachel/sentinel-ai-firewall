"""Command line. All commands are read-only; none can change PayPal state."""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from . import __version__
from .auth import AuthError, TokenProvider, load_credentials, fetch_token
from .capabilities import HARD_CEILING, TOOL_CAPABILITY, load_manifest_tools
from .client import SandboxClient
from .redact import scrub


def _client(profile: str = "in") -> tuple[SandboxClient, TokenProvider]:
    """Credentials for ONE named profile only (default: the legacy India profile, unchanged behaviour)."""
    from .profiles import load_profile_credentials
    tp = TokenProvider(load_profile_credentials(profile), fetch_token)
    return SandboxClient(tp.get), tp


def _prof(a) -> str:
    return getattr(a, "profile", None) or "in"


def cmd_check_auth(a) -> int:
    client, tp = _client(_prof(a))
    tp.get()
    info = tp.info
    print("credentials loaded: yes (values not shown)")
    print(f"sandbox OAuth: PASS  expires_in~{int(info.expires_at - __import__('time').time())}s  scopes={info.scope_count}")
    print(f"refund scope present in token: {'YES' if info.has_refund_scope else 'NO'}  (boolean only; no scope names shown)")
    return 0


def cmd_capture(a) -> int:
    """Read-only: GET one capture, report status/amount/refund-candidate. Also reports the refund-scope boolean."""
    from .capture import lookup_capture
    client, tp = _client(_prof(a))
    tp.get()
    c = lookup_capture(client, a.id)
    print(f"capture lookup: {'FOUND' if c.found else 'NOT FOUND'}  http={c.http_status}  id={c.masked_id}")
    if c.error:
        print(f"error: {c.error}")
    if c.found:
        print(f"capture status: {c.capture_status}   currency: {c.currency}   gross: {c.gross_amount}")
        rem = "UNKNOWN" if c.remaining_refundable is None else str(c.remaining_refundable)
        print(f"remaining refundable: {rem}   refund link offered by PayPal: {'yes' if c.refund_link_present else 'no'}   age_days: {c.age_days}")
    print(f"refund scope present in token: {'YES' if tp.info.has_refund_scope else 'NO'}  (boolean only; no scope names shown)")
    if c.refund_experiment_ok:
        print(f"candidate for first controlled refund experiment: YES (max experiment amount {c.experiment_amount} {c.currency}; NOTHING executed)")
    else:
        print("candidate for first controlled refund experiment: NO")
        for r in c.reasons:
            print(f"  - {r}")
    return 0 if c.found else 1


def cmd_txsearch(a) -> int:
    from .txsearch import search_transactions
    client, _ = _client(_prof(a))
    s = search_transactions(client, days=a.days)
    print(f"Transaction Search: {'PASS' if s.status_code == 200 else 'FAIL'}  http={s.status_code}")
    print(f"window: {s.window_start} .. {s.window_end}")
    print(f"transactions found: {s.count}  pages_read={s.pages_read}  statuses={s.statuses}  currencies={s.currencies}")
    if s.error:
        print(f"error: {s.error}")
    return 0 if s.status_code == 200 else 1


def _wallet_dry_run(a) -> int:
    """`wallet-run create --dry-run`: gateway decision on the proposed $2 sandbox order. No credentials, token, network, run file."""
    from .wallet_run import dry_run_create, format_dry_run
    if not a.dry_run:
        print("WALLET-RUN: --what-if-* options are only valid together with --dry-run", file=sys.stderr)
        return 2
    if a.action != "create" or a.confirm or a.run:
        print("WALLET-RUN: --dry-run applies to `create` only and takes no --confirm / --run", file=sys.stderr)
        return 2
    rep = dry_run_create(a.what_if_currency, a.what_if_amount)
    print(format_dry_run(rep))
    return 3 if rep.blocked else 0


def cmd_wallet_run(a) -> int:
    """$2.00 USD sandbox wallet checkout. create/capture are the ONLY write commands in Roadworthy and are gated
    (env flag + typed phrase + ToolGateway approval + a passing profile-check for non-legacy profiles).
    --profile is REQUIRED, so the merchant is always an explicit choice. verify is read-only. There is no refund command."""
    if a.dry_run or a.what_if_currency or a.what_if_amount:
        return _wallet_dry_run(a)            # BEFORE any profile/credential/token/network code is touched
    from .profiles import get_profile, load_check
    from .wallet_run import RunStore, WalletRunError, WalletRunner
    spec = get_profile(a.profile)
    client, tp = _client(a.profile)
    runner = WalletRunner(RunStore(spec.evidence_dir), client, tp.get, profile=a.profile, fingerprint=tp.fingerprint(),
                          profile_check=load_check(spec))
    try:
        if a.action == "create":
            return runner.create(a.confirm, run_id=a.run, callback=a.callback, wait_seconds=a.wait_seconds)
        if a.action == "capture":
            return runner.capture(a.confirm, a.run)
        if a.action == "abandon":
            return runner.abandon(a.run, a.reason)
        if a.action == "diagnose":
            return runner.diagnose(a.run)
        rep = runner.verify(a.run, a.expect_merchant_id)
        for name, status, detail in rep.checks:
            print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
        print(f"verify: {'PASS' if rep.passed else 'FAIL'}  (read-only; no refund issued)")
        return 0 if rep.passed else 1
    except WalletRunError as e:
        print(f"WALLET-RUN: {scrub(e)}", file=sys.stderr)
        return 1


def cmd_profile_check(a) -> int:
    """Read-only: OAuth + merchant checks for one profile; records profile_check.json (no secrets)."""
    from .profiles import get_profile, run_profile_check
    spec = get_profile(a.profile)
    client, tp = _client(a.profile)
    return 0 if run_profile_check(client, tp, spec, a.expect_merchant_id) else 1


def cmd_snapshot(a) -> int:
    from .snapshot import counts, digest, take_snapshot
    client, _ = _client(_prof(a))
    snap = take_snapshot(client)
    print(f"snapshot counts: {counts(snap)}  digest={digest(snap)}  (read-only; http calls={client.calls})")
    return 0


def cmd_manifest(_a) -> int:
    tools = load_manifest_tools()
    unmapped = [t for t in tools if t not in TOOL_CAPABILITY]
    print(f"official toolkit tools (static manifest): {len(tools)}   unmapped: {unmapped or 'none'}")
    per_cap = Counter(TOOL_CAPABILITY[t] for t in tools if t in TOOL_CAPABILITY)
    for cap in sorted(per_cap):
        print(f"  {cap:26s} tools={per_cap[cap]:2d}  hard ceiling={HARD_CEILING[cap].name}")
    return 0 if not unmapped else 1


def cmd_gateway_demo(_a) -> int:
    """Policy-logic demonstration only. The executor is a stub: NO PayPal call is made."""
    from .eval.models import Grade
    from .permissions.engine import issue_license
    from .permissions.gateway import ToolGateway

    def g(cap, outcome="PASS"):
        return Grade("demo", "r", "demo-agent", "scripted_baseline", cap, outcome == "PASS", 0, 0, 1,
                     int(outcome == "HARMFUL"), 0, 0, 0, False, outcome, "x", True)
    grades = [g("invoice.draft") for _ in range(20)] + [g("refund.issue", "HARMFUL")] + [g("invoice.send") for _ in range(20)]
    lic = issue_license("demo-agent", "scripted_baseline", grades)
    print("DEMO ONLY - license built from SYNTHETIC grades; executor is a stub; no PayPal calls.\n")
    gw = ToolGateway(lic, approver=lambda t, a, r: False)
    for tool, args in [("list_invoices", {}), ("create_invoice", {"currency_code": "USD"}), ("send_invoice", {"invoice_id": "X"}),
                       ("create_refund", {"capture_id": "C", "amount": {"value": "5.00", "currency_code": "USD"}}),
                       ("create_refund", {"capture_id": "C"}), ("accept_dispute_claim", {"dispute_id": "D", "note": "n"}),
                       ("delete_everything", {})]:
        d = gw.authorize(tool, args)
        print(f"{tool:22s} -> {d.action:17s} [{d.level}] {d.reason}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="roadworthy", description=f"Roadworthy {__version__} (sandbox-only, read-only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    ca = sub.add_parser("check-auth"); ca.add_argument("--profile", choices=["in", "us"], default="in"); ca.set_defaults(fn=cmd_check_auth)
    t = sub.add_parser("txsearch"); t.add_argument("--days", type=int, default=30); t.add_argument("--profile", choices=["in", "us"], default="in"); t.set_defaults(fn=cmd_txsearch)
    c = sub.add_parser("capture"); c.add_argument("--id", required=True, help="capture / transaction id (read-only lookup)"); c.add_argument("--profile", choices=["in", "us"], default="in"); c.set_defaults(fn=cmd_capture)
    w = sub.add_parser("wallet-run", help="2.00 USD sandbox wallet checkout: create | capture | verify (gated writes)")
    w.add_argument("action", choices=["create", "capture", "verify", "abandon", "diagnose"])
    w.add_argument("--profile", required=True, choices=["in", "us"], help="credential profile (REQUIRED): in = legacy India, us = US merchant")
    w.add_argument("--reason", help="abandon: reason text (default: buyer checkout rejected currency)")
    w.add_argument("--run", help="run id (required for capture/verify; optional resume for create)")
    w.add_argument("--confirm", help="typed approval phrase (create/capture only)")
    w.add_argument("--callback", choices=["loopback", "invalid"], default="loopback")
    w.add_argument("--wait-seconds", type=float, default=900)
    w.add_argument("--expect-merchant-id", help="verify: compare the order's payee merchant id with this value")
    w.add_argument("--dry-run", action="store_true", help="create only: show the gateway's decision on the proposed order. Sends nothing, uses no credentials")
    w.add_argument("--what-if-currency", help="with --dry-run: evaluate a different currency code (never sent)")
    w.add_argument("--what-if-amount", help="with --dry-run: evaluate a different amount string (never sent)")
    w.set_defaults(fn=cmd_wallet_run)
    pc = sub.add_parser("profile-check", help="read-only OAuth + merchant checks for a credential profile")
    pc.add_argument("--profile", required=True, choices=["in", "us"])
    pc.add_argument("--expect-merchant-id", help="the Merchant ID shown in that Business sandbox account's profile (required for 'us')")
    pc.set_defaults(fn=cmd_profile_check)
    sn = sub.add_parser("snapshot"); sn.add_argument("--profile", choices=["in", "us"], default="in"); sn.set_defaults(fn=cmd_snapshot)
    sub.add_parser("manifest").set_defaults(fn=cmd_manifest)
    sub.add_parser("gateway-demo").set_defaults(fn=cmd_gateway_demo)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except AuthError as e:
        print(f"AUTH ERROR: {scrub(e)}", file=sys.stderr)
        return 2
    except Exception as e:    # never dump a traceback that could contain request details
        print(f"ERROR: {type(e).__name__}: {scrub(e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
