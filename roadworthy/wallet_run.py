"""SENTINEL wallet-run: ONE sandbox PayPal wallet checkout of exactly 2.00 USD, in three separate commands.

    create   -> POST /v2/checkout/orders              (buyer approves manually in a browser)
    capture  -> GET order, then POST .../capture      (only if PayPal says APPROVED)
    verify   -> GET order + GET capture only          (read-only, independent confirmation)

There is NO refund, void, authorize, vault or any other write anywhere in this module.

Safety design
  * Writes need ALL of: env SENTINEL_ALLOW_SANDBOX_WRITE=1, the typed --confirm phrase, and ToolGateway
    approval (create_order / pay_order are never earned-AUTO here: the license holds no evidence).
  * WriteGuardedClient is the ONLY code with POST. It requires a ToolGateway at construction, exposes only
    call(), and its private _post() refuses to run unless the gateway's executor armed it with a one-time permit.
    (Python cannot stop deliberate monkey-patching; this prevents accidental or code-path bypass, and tests pin it.)
  * Exactly two requests are allow-listed, and the request must agree with the gateway arguments.
  * State is written to evidence/wallet_run_<id>.json BEFORE each POST, with a stored PayPal-Request-Id, so a
    retry after a crash/timeout replays the same idempotency key instead of creating a second order/capture.
  * No automatic POST retries. Ambiguous results leave the run in a *_SENT state and need a deliberate resume.
  * Callback: a loopback listener on 127.0.0.1 (random port, random path nonce) that never reads or stores the
    redirect's query string. Approval is never trusted from the callback: capture re-checks with an independent GET.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .capture import lookup_capture, mask_id, validate_capture_id
from .client import Response, SandboxClient
from .config import PROJECT_ROOT, SANDBOX_BASE_URL, assert_sandbox_url
from .eval.models import Grade
from .permissions.engine import issue_license
from .permissions.gateway import ToolGateway
from .redact import scrub

CONFIRM_PHRASE = "I-APPROVE-SANDBOX-2-USD"
ENV_FLAG = "SENTINEL_ALLOW_SANDBOX_WRITE"
AMOUNT = Decimal("2.00")
CURRENCY = "USD"
ITEM_NAME = "SENTINEL sandbox test"
ORDERS_PATH = "/v2/checkout/orders"
_CAPTURE_PATH_RE = re.compile(r"^/v2/checkout/orders/([A-Za-z0-9_-]{5,64})/capture$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")

UNFINISHED = {"CREATE_SENT", "CREATED", "CAPTURE_SENT"}
ABANDON_REASON = "buyer checkout rejected currency"


class WalletRunError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------- run state
class RunStore:
    def __init__(self, directory: Optional[Path] = None):
        self.dir = Path(directory) if directory else PROJECT_ROOT / "evidence"

    def path(self, run_id: str) -> Path:
        if not re.match(r"^[a-z0-9]{6,32}$", run_id or ""):
            raise WalletRunError("invalid run id")
        return self.dir / f"wallet_run_{run_id}.json"

    def load(self, run_id: str) -> Dict[str, Any]:
        p = self.path(run_id)
        if not p.is_file():
            raise WalletRunError("run file not found")
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, run: Dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(run["run_id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(run, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)          # best effort; limited on Windows
        except OSError:
            pass
        os.replace(tmp, p)

    def unfinished(self) -> List[str]:
        out = []
        if self.dir.is_dir():
            for f in sorted(self.dir.glob("wallet_run_*.json")):
                if not re.match(r"^wallet_run_[a-z0-9]{6,32}\.json$", f.name):
                    continue                  # skips wallet_run_<id>.pre-abandon.json audit backups
                try:
                    r = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if r.get("state") in UNFINISHED:
                    out.append(r.get("run_id", "?"))
        return out


# --------------------------------------------------------------------------------------------- write client
class WriteTransport:
    def request(self, method: str, url: str, headers: dict, body: bytes, timeout: float) -> Tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() or b""


@dataclass
class PreparedRequest:
    method: str
    path: str
    body: Dict[str, Any]
    request_id: str


def _dec(v: object) -> Optional[Decimal]:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


class WriteGuardedClient:
    """The only POST-capable code. Requires a ToolGateway; the gateway decides, this class only executes."""

    def __init__(self, token_getter: Callable[[], str], gateway: ToolGateway, transport: Optional[WriteTransport] = None):
        if not isinstance(gateway, ToolGateway):
            raise TypeError("WriteGuardedClient requires a ToolGateway")
        self._token = token_getter
        self._gateway = gateway
        self._t = transport or WriteTransport()
        self._permit: Optional[str] = None
        self.sent = 0

    # public surface: call() only
    def call(self, tool: str, args: Dict[str, Any], prepared: PreparedRequest) -> Dict[str, Any]:
        self._assert_allowed(tool, args, prepared)          # fail before the gateway sees a mismatching request
        return self._gateway.call(tool, args, lambda t, a: self._gateway_executor(t, a, prepared))

    def _gateway_executor(self, tool: str, args: Dict[str, Any], prepared: PreparedRequest) -> Response:
        permit = secrets.token_hex(16)
        self._permit = permit              # armed only for the duration of this gateway-approved execution
        try:
            return self._post(permit, tool, args, prepared)
        finally:
            self._permit = None

    def _assert_allowed(self, tool: str, args: Dict[str, Any], p: PreparedRequest) -> None:
        if self._gateway.environment != "SANDBOX":
            raise WalletRunError("sandbox only")
        if p.method != "POST" or not p.request_id:
            raise WalletRunError("only POST with a PayPal-Request-Id is allowed")
        if tool == "create_order":
            if p.path != ORDERS_PATH:
                raise WalletRunError("create_order may only POST " + ORDERS_PATH)
            items = args.get("items")
            if not (isinstance(items, list) and len(items) == 1 and _dec(items[0].get("quantity")) == 1
                    and _dec((items[0].get("itemCost") or {}).get("value")) == AMOUNT) or args.get("currencyCode") != CURRENCY:
                raise WalletRunError("gateway arguments must be exactly one item of 2.00 USD")
            pu = p.body.get("purchase_units") or []
            if p.body.get("intent") != "CAPTURE" or len(pu) != 1:
                raise WalletRunError("order must be intent CAPTURE with one purchase unit")
            amt = pu[0].get("amount") or {}
            if amt.get("currency_code") != CURRENCY or _dec(amt.get("value")) != AMOUNT:
                raise WalletRunError("order amount must be exactly 2.00 USD")
        elif tool == "pay_order":
            m = _CAPTURE_PATH_RE.match(p.path)
            if not m or m.group(1) != args.get("id"):
                raise WalletRunError("pay_order may only POST /v2/checkout/orders/{id}/capture for its own id")
            if p.body != {}:
                raise WalletRunError("capture body must be empty")
        else:
            raise WalletRunError(f"tool {tool!r} is not allowed")

    def _post(self, permit: str, tool: str, args: Dict[str, Any], p: PreparedRequest) -> Response:
        if not self._permit or permit != self._permit:
            raise WalletRunError("write refused: no gateway permit")
        self._permit = None                 # one-time
        self._assert_allowed(tool, args, p)
        url = SANDBOX_BASE_URL + p.path
        assert_sandbox_url(url)
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json",
                   "Content-Type": "application/json", "PayPal-Request-Id": p.request_id}
        if tool == "create_order":
            headers["Prefer"] = "return=representation"
        self.sent += 1
        status, raw = self._t.request("POST", url, headers, json.dumps(p.body).encode("utf-8"), 30.0)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        return Response(status, body)


def make_gateway(confirm: Optional[str], env: Dict[str, str]) -> ToolGateway:
    """Empty-evidence license: create_order and pay_order both need approval. The approver is the typed phrase."""
    lic = issue_license("sentinel-wallet-run", "scripted_baseline", [])

    def approver(tool: str, args: Dict[str, Any], reason: str) -> bool:
        return confirm == CONFIRM_PHRASE and env.get(ENV_FLAG) == "1"
    return ToolGateway(lic, approver=approver, environment="SANDBOX")


# --------------------------------------------------------------------------------------------- callback
class CallbackListener:
    """127.0.0.1-only, random port, random path nonce. Never reads/stores/logs the query string.
    Threaded with per-connection timeouts: browsers open idle speculative connections, which used to block a
    single-threaded listener so the real redirect was never served (reproduced and fixed)."""

    def __init__(self, nonce: str):
        self.nonce = nonce
        self.result: Optional[str] = None
        self._event = threading.Event()
        outer = self

        class H(BaseHTTPRequestHandler):
            timeout = 5                     # an idle connection is dropped after 5 s and never blocks others

            def do_GET(self):          # noqa: N802
                path = self.path.split("?", 1)[0]
                kind = {f"/sentinel/{outer.nonce}/return": "return", f"/sentinel/{outer.nonce}/cancel": "cancel"}.get(path)
                if kind is None:
                    self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
                msg = ("Approval received. You can close this tab and run the capture command."
                       if kind == "return" else "Checkout cancelled. Nothing was paid.")
                data = f"<html><body><p>{msg}</p></body></html>".encode()
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close"); self.end_headers(); self.wfile.write(data)
                outer.result = kind
                outer._event.set()

            def log_message(self, *a):  # silence request logging
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()

    @property
    def return_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/sentinel/{self.nonce}/return"

    @property
    def cancel_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/sentinel/{self.nonce}/cancel"

    def wait(self, seconds: float) -> Optional[str]:
        self._event.wait(seconds)
        return self.result

    def close(self) -> None:
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()


# --------------------------------------------------------------------------------------------- helpers
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_approve_url(links: object) -> Optional[str]:
    for rel in ("payer-action", "approve"):
        for l in links or []:
            if isinstance(l, dict) and l.get("rel") == rel:
                u = urlparse(str(l.get("href", "")))
                if u.scheme == "https" and u.hostname and (u.hostname == "paypal.com" or u.hostname.endswith(".paypal.com")):
                    return str(l["href"])
    return None


def _unit(body: Any) -> Dict[str, Any]:
    pu = (body or {}).get("purchase_units") if isinstance(body, dict) else None
    return pu[0] if isinstance(pu, list) and pu and isinstance(pu[0], dict) else {}


def _unit_matches(unit: Dict[str, Any], custom_id: str) -> bool:
    amt = unit.get("amount") or {}
    return amt.get("currency_code") == CURRENCY and _dec(amt.get("value")) == AMOUNT and unit.get("custom_id") == custom_id


def build_create_body(run: Dict[str, Any]) -> Dict[str, Any]:
    return {"intent": "CAPTURE",
            "purchase_units": [{"custom_id": run["custom_id"], "description": ITEM_NAME,
                                "amount": {"currency_code": CURRENCY, "value": str(AMOUNT)}}],
            "payment_source": {"paypal": {"experience_context": {
                "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
                "return_url": run["return_url"], "cancel_url": run["cancel_url"]}}}}


GATEWAY_CREATE_ARGS = {"currencyCode": CURRENCY,
                       "items": [{"name": ITEM_NAME, "quantity": 1, "itemCost": {"value": str(AMOUNT), "currencyCode": CURRENCY}}]}


@dataclass
class DryRunReport:
    """Result of `wallet-run create --dry-run`: the gateway's decision on the proposed sandbox order. Nothing was sent."""
    verdict: str                 # ALLOW | REQUIRE_APPROVAL | DENY | REFUSED_BY_REQUEST_GUARD
    gateway_action: str
    gateway_level: str
    gateway_reason: str
    capability: str
    request_guard: str           # "OK" or why the write client would refuse this exact request
    gateway_args: Dict[str, Any]
    body: Dict[str, Any]
    what_if: bool
    requests_sent: int = 0

    @property
    def blocked(self) -> bool:
        return self.verdict in ("DENY", "REFUSED_BY_REQUEST_GUARD")


class _NoNetwork:
    def request(self, *a: Any, **k: Any) -> Tuple[int, bytes]:
        raise WalletRunError("dry-run: a network request was attempted (this must never happen)")


def _no_token() -> str:
    raise WalletRunError("dry-run: an OAuth token was requested (this must never happen)")


def dry_run_create(what_if_currency: Optional[str] = None, what_if_amount: Optional[str] = None) -> DryRunReport:
    """Build the proposed sandbox order and evaluate it through the real ToolGateway and the write client's request guard,
    WITHOUT credentials, a token, a network connection, a run file or any PayPal state change. `what_if_*` lets you see how the
    gateway treats a different currency or amount; a real `create` can only ever send exactly 2.00 USD."""
    args = copy.deepcopy(GATEWAY_CREATE_ARGS)
    cost = args["items"][0]["itemCost"]
    if what_if_currency is not None:
        args["currencyCode"] = cost["currencyCode"] = what_if_currency
    if what_if_amount is not None:
        cost["value"] = what_if_amount
    body = build_create_body({"custom_id": "SENTINEL-dry-run", "return_url": "https://sentinel.invalid/dry-run/return",
                              "cancel_url": "https://sentinel.invalid/dry-run/cancel"})
    amt = body["purchase_units"][0]["amount"]
    if what_if_currency is not None:
        amt["currency_code"] = what_if_currency
    if what_if_amount is not None:
        amt["value"] = what_if_amount
    gw = make_gateway(None, {})                                   # no approval phrase, no env flag: nothing can be approved
    dec = gw.authorize("create_order", args)                      # pure decision: no side effects
    client = WriteGuardedClient(_no_token, gw, _NoNetwork())      # type: ignore[arg-type]
    try:
        client._assert_allowed("create_order", args, PreparedRequest("POST", ORDERS_PATH, body, "dry-run"))
        guard = "OK"
    except WalletRunError as e:
        guard = str(e)
    verdict = dec.action if dec.action == "DENY" or guard == "OK" else "REFUSED_BY_REQUEST_GUARD"
    return DryRunReport(verdict, dec.action, dec.level, dec.reason, dec.capability, guard, args, body,
                        what_if_currency is not None or what_if_amount is not None, client.sent)


def format_dry_run(r: DryRunReport) -> str:
    lines = ["WALLET-RUN CREATE --DRY-RUN  (nothing was sent; no credentials, token, network or run file used)",
             f"  proposed order : {r.body['purchase_units'][0]['amount']['value']} {r.body['purchase_units'][0]['amount']['currency_code']}"
             + ("   [WHAT-IF values, not what a real create sends]" if r.what_if else ""),
             f"  tool/capability: create_order / {r.capability}",
             f"  gateway        : {r.gateway_action} ({r.gateway_level}): {r.gateway_reason}",
             f"  request guard  : {r.request_guard}",
             f"  VERDICT        : {r.verdict}"]
    if r.verdict == "REQUIRE_APPROVAL":
        lines.append("  A real create would need ALL of: SENTINEL_ALLOW_SANDBOX_WRITE=1, the typed --confirm phrase, and this gateway approval.")
    elif r.blocked:
        lines.append("  A real create would be refused; no approval can override this.")
    lines.append(f"  requests sent  : {r.requests_sent}")
    return "\n".join(lines)


@dataclass
class VerifyReport:
    checks: List[Tuple[str, str, str]] = field(default_factory=list)      # (name, PASS|FAIL|NOT_COMPARED, detail)

    @property
    def passed(self) -> bool:
        return all(s != "FAIL" for _, s, _ in self.checks) and any(s == "PASS" for _, s, _ in self.checks)


# --------------------------------------------------------------------------------------------- runner
class WalletRunner:
    def __init__(self, store: RunStore, reader: SandboxClient, token_getter: Callable[[], str],
                 write_transport: Optional[WriteTransport] = None, env: Optional[Dict[str, str]] = None,
                 say: Callable[[str], None] = print, listener_factory: Callable[[str], CallbackListener] = CallbackListener,
                 profile: str = "in", fingerprint: Optional[str] = None, profile_check: Optional[Dict[str, Any]] = None):
        self.store, self.reader, self._token = store, reader, token_getter
        self._wt, self.env, self.say, self._lf = write_transport, (os.environ if env is None else env), say, listener_factory
        self.profile, self.fingerprint, self.profile_check = profile, fingerprint, profile_check

    # ---- credential-profile isolation ----
    def _expected_merchant(self) -> Optional[str]:
        if self.profile == "in":                      # legacy India profile: behaviour unchanged
            return None
        return (self.profile_check or {}).get("expected_merchant_id")

    def _gate_profile(self) -> None:
        """Non-legacy profiles may write only after a passing profile-check for THESE credentials."""
        if self.profile == "in":
            return
        pc = self.profile_check or {}
        if (pc.get("status") != "OK" or pc.get("profile") != self.profile or not self.fingerprint
                or pc.get("fingerprint") != self.fingerprint or not pc.get("expected_merchant_id")):
            raise WalletRunError(f"profile '{self.profile}' has no passing profile-check for the current credentials. Run: "
                                 f"profile-check --profile {self.profile} --expect-merchant-id <Merchant ID>. Nothing was sent.")

    def _assert_run_profile(self, run: Dict[str, Any]) -> None:
        run_profile = run.get("profile", "in")        # runs created before profiles existed are India runs
        if run_profile != self.profile:
            raise WalletRunError(f"this run belongs to profile '{run_profile}', not '{self.profile}'. Nothing was done.")
        rf = run.get("credential_fingerprint")
        if rf and self.fingerprint and rf != self.fingerprint:
            raise WalletRunError("the credentials now loaded differ from those that created this run. Nothing was done.")

    # ---- gates ----
    def _require_write_enabled(self, confirm: Optional[str]) -> None:
        if self.env.get(ENV_FLAG) != "1":
            raise WalletRunError(f"writes are disabled: set {ENV_FLAG}=1 in your own shell")
        if confirm != CONFIRM_PHRASE:
            raise WalletRunError(f"missing or wrong --confirm phrase (expected {CONFIRM_PHRASE})")

    def _writer(self, confirm: Optional[str]) -> WriteGuardedClient:
        return WriteGuardedClient(self._token, make_gateway(confirm, self.env), self._wt)

    # ---- create ----
    def create(self, confirm: Optional[str], run_id: Optional[str] = None, callback: str = "loopback", wait_seconds: float = 900) -> int:
        self._require_write_enabled(confirm)
        self._gate_profile()
        if callback not in ("loopback", "invalid"):
            raise WalletRunError("callback must be loopback or invalid")
        listener: Optional[CallbackListener] = None
        if run_id:
            run = self.store.load(run_id)
            self._assert_run_profile(run)
            if run["state"] != "CREATE_SENT":
                raise WalletRunError(f"run is {run['state']}; create can only resume a CREATE_SENT run")
            self.say("resuming CREATE_SENT run with the SAME PayPal-Request-Id (idempotent replay)")
        else:
            pending = self.store.unfinished()
            if pending:
                raise WalletRunError(f"unfinished run(s) exist: {pending}. Resume with --run, or move the file away.")
            rid = "r" + secrets.token_hex(6)
            nonce = secrets.token_hex(8)
            if callback == "loopback":
                listener = self._lf(nonce)
                ret, can = listener.return_url, listener.cancel_url
            else:
                ret, can = f"https://sentinel.invalid/{nonce}/return", f"https://sentinel.invalid/{nonce}/cancel"
            run = {"run_id": rid, "custom_id": "SENTINEL-" + rid, "state": "NEW", "created_at": _now(), "amount": str(AMOUNT),
                   "currency": CURRENCY, "callback_mode": callback, "nonce": nonce, "return_url": ret, "cancel_url": can,
                   "profile": self.profile, "credential_fingerprint": self.fingerprint,
                   "request_id_create": str(uuid.uuid4()), "request_id_capture": str(uuid.uuid4()),
                   "order_id": None, "capture_id": None, "approve_url": None, "last_error": None, "gateway_audit": []}
        try:
            run["state"] = "CREATE_SENT"
            self.store.save(run)             # persisted BEFORE the POST, so a retry replays the same key
            writer = self._writer(confirm)
            prepared = PreparedRequest("POST", ORDERS_PATH, build_create_body(run), run["request_id_create"])
            try:
                out = writer.call("create_order", GATEWAY_CREATE_ARGS, prepared)
            except Exception as e:           # transport failure: ambiguous, never retried automatically
                run["last_error"] = type(e).__name__
                self.store.save(run)
                raise WalletRunError(f"create result unknown ({type(e).__name__}). Resume with: wallet-run create --run {run['run_id']}")
            run["gateway_audit"] += writer._gateway.audit
            if not out["executed"]:
                run["state"] = "NEW"; run["last_error"] = "gateway refused: " + out["decision"].reason
                self.store.save(run)
                raise WalletRunError(run["last_error"])
            resp: Response = out["result"]
            if resp.status in (200, 201) and isinstance(resp.body, dict) and resp.body.get("id") and _ID_RE.match(str(resp.body["id"])):
                unit = _unit(resp.body)
                if unit and not _unit_matches(unit, run["custom_id"]):
                    run["state"] = "MISMATCH"; run["last_error"] = "PayPal echoed a different amount/custom_id"
                    self.store.save(run)
                    raise WalletRunError(run["last_error"])
                payee = (unit.get("payee") or {}).get("merchant_id")
                if payee:
                    run["payee_merchant_id"] = payee
                exp = self._expected_merchant()
                if exp and payee and payee != exp:
                    run["order_id"] = resp.body["id"]
                    run["state"] = "MISMATCH"; run["last_error"] = "order payee merchant id differs from the profile's expected Merchant ID"
                    self.store.save(run)
                    raise WalletRunError(run["last_error"] + ". The order is NOT captured; do not approve it.")
                run["order_id"] = resp.body["id"]
                run["order_status"] = resp.body.get("status")
                run["approve_url"] = _safe_approve_url(resp.body.get("links"))
                run["state"] = "CREATED"
                self.store.save(run)
            elif 400 <= resp.status < 500:
                run["state"] = "CREATE_FAILED"; run["last_error"] = resp.error_name() or f"HTTP {resp.status}"
                self.store.save(run)
                raise WalletRunError(f"order not created: {run['last_error']} (http {resp.status}). No order exists.")
            else:
                run["last_error"] = f"HTTP {resp.status}"
                self.store.save(run)
                raise WalletRunError(f"create result unknown (http {resp.status}). Resume with: wallet-run create --run {run['run_id']}")
            self.say(f"run: {run['run_id']}   order: {mask_id(run['order_id'])}   status: {run.get('order_status')}   amount: {AMOUNT} {CURRENCY}")
            if not run["approve_url"]:
                raise WalletRunError("PayPal returned no usable buyer-approval link; do not proceed")
            self.say("OPEN THIS URL in a private browser window and approve as the sandbox PERSONAL buyer (you enter the buyer login):")
            self.say(run["approve_url"])
            if listener and wait_seconds > 0:
                self.say(f"waiting up to {int(wait_seconds)}s for the local callback (Ctrl+C is safe; the run is saved)...")
                got = listener.wait(wait_seconds)
                self.say({"return": "callback received. Next: wallet-run capture --run " + run["run_id"] + " --confirm ...",
                          "cancel": "buyer cancelled. Nothing was paid.", None: "no callback seen; capture still checks PayPal itself"}[got])
            else:
                self.say("next: after approving, run: wallet-run capture --run " + run["run_id"] + " --confirm " + CONFIRM_PHRASE)
            return 0
        finally:
            if listener:
                listener.close()

    # ---- capture ----
    def capture(self, confirm: Optional[str], run_id: str) -> int:
        self._require_write_enabled(confirm)
        self._gate_profile()
        run = self.store.load(run_id)
        self._assert_run_profile(run)
        if run["state"] not in ("CREATED", "CAPTURE_SENT"):
            raise WalletRunError(f"run is {run['state']}; capture needs CREATED (or a CAPTURE_SENT resume). At most one capture per run.")
        oid = run["order_id"]
        validate_capture_id(oid)
        r = self.reader.get(f"{ORDERS_PATH}/{oid}")
        if not r.ok or not isinstance(r.body, dict):
            raise WalletRunError(f"could not read the order (http {r.status}); nothing was sent")
        status, unit = str(r.body.get("status")), _unit(r.body)
        if r.body.get("intent") != "CAPTURE" or not _unit_matches(unit, run["custom_id"]):
            raise WalletRunError("order on PayPal does not match this run (intent/amount/currency/custom_id); nothing was sent")
        exp = self._expected_merchant()
        if exp and (unit.get("payee") or {}).get("merchant_id") != exp:
            raise WalletRunError("order payee merchant id is missing or differs from the profile's expected Merchant ID; nothing was sent")
        if status == "COMPLETED":                       # an earlier ambiguous capture actually succeeded
            caps = (unit.get("payments") or {}).get("captures") or []
            if len(caps) != 1 or not _ID_RE.match(str(caps[0].get("id", ""))):
                raise WalletRunError("order is COMPLETED but its capture could not be identified; inspect manually")
            run["capture_id"], run["state"] = caps[0]["id"], "CAPTURED"
            self.store.save(run)
            self.say(f"order already COMPLETED; recorded capture {mask_id(run['capture_id'])}. No new capture was sent.")
            return 0
        if status != "APPROVED":
            self.say(f"order status is {status}, not APPROVED. Approve it in the browser first. Nothing was sent.")
            return 1
        run["state"] = "CAPTURE_SENT"
        self.store.save(run)                            # persisted BEFORE the POST
        writer = self._writer(confirm)
        prepared = PreparedRequest("POST", f"{ORDERS_PATH}/{oid}/capture", {}, run["request_id_capture"])
        try:
            out = writer.call("pay_order", {"id": oid}, prepared)
        except Exception as e:
            run["last_error"] = type(e).__name__
            self.store.save(run)
            raise WalletRunError(f"capture result unknown ({type(e).__name__}). Re-run capture to replay the same request id, or run verify.")
        run["gateway_audit"] += writer._gateway.audit
        if not out["executed"]:
            run["state"] = "CREATED"; run["last_error"] = "gateway refused: " + out["decision"].reason
            self.store.save(run)
            raise WalletRunError(run["last_error"])
        resp: Response = out["result"]
        caps = (_unit(resp.body).get("payments") or {}).get("captures") or [] if isinstance(resp.body, dict) else []
        if resp.status in (200, 201) and len(caps) == 1 and _ID_RE.match(str(caps[0].get("id", ""))):
            run["capture_id"], run["capture_status_at_capture"], run["state"] = caps[0]["id"], caps[0].get("status"), "CAPTURED"
            self.store.save(run)
            self.say(f"captured: {mask_id(run['capture_id'])}  status: {caps[0].get('status')}. STOP: no refund is issued. Next: wallet-run verify --run {run['run_id']}")
            return 0
        if 400 <= resp.status < 500:
            run["state"] = "CAPTURE_FAILED"; run["last_error"] = resp.error_name() or f"HTTP {resp.status}"
            self.store.save(run)
            raise WalletRunError(f"capture rejected: {run['last_error']} (http {resp.status}). Nothing was captured.")
        run["last_error"] = f"HTTP {resp.status}"
        self.store.save(run)
        raise WalletRunError(f"capture result unknown (http {resp.status}). Re-run capture (same request id) or run verify.")

    # ---- abandon (local bookkeeping; PayPal is only READ) ----
    def abandon(self, run_id: str, reason: Optional[str] = None) -> int:
        """Mark an unfinished run ABANDONED so it can never be captured or reused and a new run is allowed.
        Fail closed: PayPal is read with GET first and the abandon is refused if a capture exists or the read fails.
        The original file is copied byte-for-byte to wallet_run_<id>.pre-abandon.json (never overwritten) and the
        run file only gains fields; no existing field, id, timestamp or history entry is removed or rewritten
        (only `state` changes, and its previous value is recorded in `history` and `abandoned.previous_state`)."""
        reason = (scrub(reason or ABANDON_REASON, 200).strip()) or ABANDON_REASON
        run = self.store.load(run_id)
        self._assert_run_profile(run)
        prev = run.get("state")
        if prev == "ABANDONED":
            raise WalletRunError("run is already ABANDONED; nothing changed")
        if prev not in UNFINISHED:
            raise WalletRunError(f"run is {prev}; only unfinished runs ({sorted(UNFINISHED)}) can be abandoned")
        if run.get("capture_id"):
            raise WalletRunError("this run recorded a capture id; abandon refused")
        oid = run.get("order_id")
        if not oid:
            raise WalletRunError("no order id is recorded (create result unknown), so 'no order/capture exists' cannot be proven; "
                                 "resume create or inspect PayPal manually. Abandon refused")
        validate_capture_id(oid)
        r = self.reader.get(f"{ORDERS_PATH}/{oid}")
        if not r.ok or not isinstance(r.body, dict):
            raise WalletRunError(f"could not read the order from PayPal (http {r.status}); abandon refused (fail closed)")
        status = str(r.body.get("status"))
        caps = (_unit(r.body).get("payments") or {}).get("captures") or []
        if status == "COMPLETED" or caps:
            raise WalletRunError(f"PayPal shows order status {status} with {len(caps)} capture(s); abandon refused. "
                                 "Run capture (it records the existing capture) and verify instead")
        src = self.store.path(run_id)
        backup = src.with_name(f"wallet_run_{run_id}.pre-abandon.json")
        try:
            with open(src, "rb") as f, open(backup, "xb") as g:       # "x": never overwrite an existing backup
                g.write(f.read())
        except FileExistsError:
            raise WalletRunError("a pre-abandon backup already exists; refusing to overwrite audit history")
        try:
            os.chmod(backup, 0o600)
        except OSError:
            pass
        at = _now()
        run["history"] = list(run.get("history") or []) + [{"at": at, "from": prev, "to": "ABANDONED", "reason": reason}]
        run["abandoned"] = {"at": at, "reason": reason, "previous_state": prev, "order_status_at_abandon": status,
                            "captures_seen": 0, "backup_file": backup.name}
        run["state"] = "ABANDONED"
        self.store.save(run)
        self.say(f"run {run_id} ABANDONED (order {mask_id(oid)} was {status}, 0 captures). Reason: {reason}")
        self.say(f"original file preserved as {backup.name}. A new run may now be created.")
        if status == "APPROVED":
            self.say("note: PayPal shows the buyer already APPROVED this order. It is not captured, but it remains approvable "
                     "until it expires; this tool will never capture it.")
        return 0

    # ---- diagnose (GET only, never writes anything, not even the run file) ----
    def diagnose(self, run_id: str) -> int:
        """Read-only report on the order behind a run: status, payee, amount, links, age and local callback config.
        PayPal's API exposes nothing about the buyer's browser session, login or funding sources; this says so."""
        run = self.store.load(run_id)
        self._assert_run_profile(run)
        oid = run.get("order_id")
        if not oid:
            raise WalletRunError("run has no order id")
        validate_capture_id(oid)
        r = self.reader.get(f"{ORDERS_PATH}/{oid}")
        say = self.say
        say(f"run {run_id}  profile {run.get('profile', 'in')}  local state {run.get('state')}  order {mask_id(oid)}")
        say(f"order GET: http {r.status}" + ("" if r.ok else f"  error {r.error_name() or 'n/a'}"))
        if not r.ok or not isinstance(r.body, dict):
            say("DIAGNOSE: the order could not be read; nothing else can be said")
            return 1
        b, unit = r.body, _unit(r.body)
        status = str(b.get("status"))
        say(f"status: {status}   intent: {b.get('intent')}")
        amt = unit.get("amount") or {}
        say(f"amount: {amt.get('value')} {amt.get('currency_code')}   [{'PASS' if _unit_matches(unit, run['custom_id']) else 'FAIL'}] exactly 2.00 USD and this run's custom_id")
        payee = (unit.get("payee") or {}).get("merchant_id")
        expected = self._expected_merchant() or run.get("payee_merchant_id")
        verdict = "NOT_COMPARED" if not expected else ("PASS" if payee == expected else "FAIL")
        say(f"payee merchant id: {mask_id(str(payee))}   [{verdict}] vs expected {mask_id(str(expected)) if expected else 'n/a'}")
        rels = sorted({str(l.get("rel")) for l in (b.get("links") or []) if isinstance(l, dict)})
        link = _safe_approve_url(b.get("links"))
        say(f"links offered: {rels}   buyer-approval link on paypal.com: {'yes' if link else 'no'}")
        if link:
            say(f"  approval link carries this order's id: {'yes' if oid in link else 'NO'}")
        say(f"payment_source present: {sorted((b.get('payment_source') or {}).keys()) or 'none'}   payer attached to order: {'yes' if b.get('payer') else 'no'}")
        caps = (unit.get("payments") or {}).get("captures") or []
        say(f"captures on order: {len(caps)}")
        ct = b.get("create_time") or run.get("created_at")
        try:
            created = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
            left = 180 - age_min
            say(f"age: {int(age_min)} min   approval window (about 3 h): " + (f"~{int(left)} min left" if left > 0 else "EXPIRED"))
        except ValueError:
            say("age: unknown")
        host = urlparse(run.get("return_url", "")).netloc
        say(f"local callback config: mode {run.get('callback_mode')}, return host {host or 'n/a'} "
            "(the loopback listener only runs while the create command is waiting; if it has ended, PayPal's redirect shows a "
            "'cannot connect' page. Harmless: capture always trusts PayPal's own GET, not the callback)")
        say("not visible through the API: the buyer's login session, which email was typed, funding sources, cookies. Those are browser-side.")
        return 0

    # ---- verify (GET only) ----
    def verify(self, run_id: str, expect_merchant_id: Optional[str] = None) -> VerifyReport:
        run = self.store.load(run_id)
        self._assert_run_profile(run)
        if expect_merchant_id is None:
            expect_merchant_id = self._expected_merchant()
        rep = VerifyReport()
        add = lambda n, ok, d="": rep.checks.append((n, "PASS" if ok else "FAIL", d))
        oid = run.get("order_id")
        if not oid:
            add("order exists", False, "no order id in run file")
            return rep
        o = self.reader.get(f"{ORDERS_PATH}/{oid}")
        ob = o.body if o.ok and isinstance(o.body, dict) else {}
        unit = _unit(ob)
        add("order readable by independent GET", o.ok, f"http {o.status}")
        add("order status COMPLETED", ob.get("status") == "COMPLETED", str(ob.get("status")))
        add("order amount 2.00 USD and custom_id match", _unit_matches(unit, run["custom_id"]))
        caps = (unit.get("payments") or {}).get("captures") or []
        cid = run.get("capture_id") or (caps[0].get("id") if len(caps) == 1 else None)
        add("exactly one capture on the order", len(caps) == 1, f"{len(caps)} found")
        if run.get("capture_id") and caps:
            add("recorded capture id matches PayPal's order", caps[0].get("id") == run["capture_id"])
        if cid and _ID_RE.match(str(cid)):
            c = self.reader.get(f"/v2/payments/captures/{cid}")
            cb = c.body if c.ok and isinstance(c.body, dict) else {}
            add("capture readable by independent GET", c.ok, f"http {c.status} id {mask_id(cid)}")
            add("capture status COMPLETED", cb.get("status") == "COMPLETED", str(cb.get("status")))
            camt = cb.get("amount") or {}
            add("capture amount 2.00 USD", camt.get("currency_code") == CURRENCY and _dec(camt.get("value")) == AMOUNT)
            add("capture custom_id matches this run", cb.get("custom_id") == run["custom_id"])
            info = lookup_capture(self.reader, cid)
            add("refund link offered and 2.00 remains refundable (NOT refunded)",
                info.refund_link_present and info.remaining_refundable == AMOUNT, f"remaining {info.remaining_refundable}")
        else:
            add("capture identified", False, "no capture id")
        merchant = (unit.get("payee") or {}).get("merchant_id")
        if expect_merchant_id:
            add("payee merchant id matches the one you supplied", bool(merchant) and merchant == expect_merchant_id, f"payee {mask_id(str(merchant))}")
        else:
            rep.checks.append(("payee merchant id compared", "NOT_COMPARED", f"payee {mask_id(str(merchant))}; pass --expect-merchant-id to compare"))
        return rep
