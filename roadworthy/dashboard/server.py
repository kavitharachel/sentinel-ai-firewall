"""Loopback-only HTTP server for the dashboard. Serves a fixed set of static files and JSON endpoints.
There is no endpoint that touches PayPal; POST endpoints only run SIMULATIONS."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import service
from .attacks import run_all, run_attack

STATIC = Path(__file__).resolve().parent / "static"
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"),
         "/app.js": ("app.js", "application/javascript; charset=utf-8")}
PERMISSIONS = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=(), clipboard-read=(), interest-cohort=()"
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
MAX_BODY = 8192


class BadRequest(Exception):
    pass


_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:\d{1,5})?$")


def _int_env(env, key: str, default: int, lo: int = 1, hi: int = 100000) -> int:
    try:
        return max(lo, min(hi, int(env.get(key, default))))
    except (TypeError, ValueError):
        return default


class PublicPolicy:
    """Everything that only applies when SENTINEL_PUBLIC=1. Fail-closed: with no SENTINEL_PUBLIC_HOST only loopback names are served.
    Rate limits are in-process sliding windows (one instance of the server; see DEPLOY.md). Nothing here logs request bodies."""

    def __init__(self, env=None, clock=time.monotonic):
        env = os.environ if env is None else env
        self.hosts = {h.strip().lower() for h in env.get("SENTINEL_PUBLIC_HOST", "").split(",") if h.strip() and _HOSTNAME.match(h.strip().lower())}
        self.trust_proxy = env.get("SENTINEL_TRUSTED_PROXY", "") == "1"
        self.trusted_hops = _int_env(env, "SENTINEL_TRUSTED_HOPS", 1, 1, 5)      # proxies in front of us that each append to X-Forwarded-For
        self.post_per_min = _int_env(env, "SENTINEL_RATE_POST_PER_MIN", 60)
        self.get_per_min = _int_env(env, "SENTINEL_RATE_GET_PER_MIN", 600)
        self.global_post_per_min = _int_env(env, "SENTINEL_RATE_GLOBAL_POST_PER_MIN", 900)
        self.max_concurrent = _int_env(env, "SENTINEL_MAX_CONCURRENT", 8, 1, 256)
        self.max_ips = 4096
        self._clock, self._lock = clock, threading.Lock()
        self._salt = os.urandom(8)
        self._hits: Dict[Tuple[str, str], deque] = {}
        self._global: deque = deque()
        self._sem = threading.BoundedSemaphore(self.max_concurrent)

    def host_ok(self, host: str, port: int) -> bool:
        h = (host or "").strip().lower()
        if h in self.hosts or h in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return True
        return False

    def origin_ok(self, origin: str, port: int) -> bool:
        o = (origin or "").strip().lower()
        for h in self.hosts:
            if o in (f"https://{h}", f"http://{h}"):
                return True
        return o in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def client_ip(self, peer: str, xff: str) -> str:
        if self.trust_proxy and xff:
            parts = [x.strip() for x in xff.split(",")]
            if len(parts) >= self.trusted_hops:
                cand = parts[-self.trusted_hops]        # counted from the right: entries to the left of our own proxies are client-controlled
                if re.match(r"^[0-9a-fA-F:.]{2,45}$", cand):
                    return cand
        return peer

    def client_key(self, ip: str) -> str:
        """Opaque per-process label of the IP the limiter keys on (salted hash, 8 hex). Lets the operator check SENTINEL_TRUSTED_HOPS from
        outside: different networks must give different keys; a spoofed X-Forwarded-For from one network must NOT change the key."""
        return hashlib.sha256(self._salt + ip.encode()).hexdigest()[:8]

    def allow(self, ip: str, kind: str) -> bool:
        now = self._clock()
        limit = self.post_per_min if kind == "post" else self.get_per_min
        with self._lock:
            if len(self._hits) > self.max_ips:
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > 60]:
                    del self._hits[k]
                if len(self._hits) > self.max_ips:
                    return False
            q = self._hits.setdefault((ip, kind), deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                return False
            if kind == "post":
                while self._global and now - self._global[0] > 60:
                    self._global.popleft()
                if len(self._global) >= self.global_post_per_min:
                    return False
                self._global.append(now)
            q.append(now)
            return True

    def acquire(self) -> bool:
        return self._sem.acquire(blocking=False)

    def release(self) -> None:
        self._sem.release()


def route_get(path: str, qs: Dict[str, list], live_dir: Optional[Path]) -> Any:
    one = lambda k, d="": (qs.get(k) or [d])[0]
    if path == "/api/overview": return service.overview(live_dir)
    if path == "/api/claims": return service.claims(live_dir)
    if path == "/api/attacks": return service.attacks()
    if path == "/api/audit":
        t = one("tamper")
        return service.audit_verify(one("agent", "eager"), int(t) if t.isdigit() else None)
    if path == "/api/llm/status": return service.llm_status()
    if path == "/api/console/info": return service.console_info()
    if path == "/api/llm-evidence": return service.llm_evidence_info()
    if path == "/api/health": return {"ok": True, "writes_to_paypal": False, "public": service.is_public()}
    raise KeyError(path)


def route_post(path: str, body: Dict[str, Any]) -> Any:
    sess = body.get("session") if isinstance(body.get("session"), str) else None
    if path == "/api/agent/run":
        return service.agent_run(str(body.get("agent", "diligent")), body.get("approvals") or [], body.get("consent") is True, sess)
    if path == "/api/console/run":
        return service.console_run(body.get("prompt"), body.get("source", "operator"), body.get("agent", "careful"), body.get("approvals") or [], body.get("consent") is True, sess)
    if path == "/api/timemachine":
        return service.timemachine(str(body.get("agent", "eager")), bool(body.get("gateway", True)), bool(body.get("verifier", True)), bool(body.get("human", False)), sess)
    if path == "/api/trust": return service.trust(str(body.get("capability", "order.create")), [str(e) for e in (body.get("events") or [])])
    if path == "/api/attacks/run":
        return run_all() if body.get("id") == "all" else run_attack(str(body.get("id")))
    raise KeyError(path)


def make_handler(port_getter, live_dir: Optional[Path], policy: Optional[PublicPolicy] = None):
    class H(BaseHTTPRequestHandler):
        server_version = "SENTINEL"
        sys_version = ""
        timeout = 10

        def log_message(self, *a): pass

        def _host_ok(self) -> bool:                      # DNS-rebinding defence: only our own name:port (loopback, or the allow-listed public host)
            h = (self.headers.get("Host") or "").lower()
            if policy is not None:
                return policy.host_ok(h, port_getter())
            return h in (f"127.0.0.1:{port_getter()}", f"localhost:{port_getter()}")

        def _origin_ok(self, origin: str) -> bool:
            if policy is not None:
                return policy.origin_ok(origin, port_getter())
            return origin in (f"http://127.0.0.1:{port_getter()}", f"http://localhost:{port_getter()}")

        def _gate(self, kind: str) -> bool:                  # public mode only: per-IP + global rate limit. Returns False after answering.
            if policy is None:
                return True
            ip = policy.client_ip(self.client_address[0], self.headers.get("X-Forwarded-For") or "")
            if not policy.allow(ip, kind):
                self._json(429, {"error": "rate limit: please slow down", "retry_after_seconds": 30}, {"Retry-After": "30"})
                return False
            return True

        def _send(self, code: int, ctype: str, data: bytes, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer")
            if policy is not None:
                self.send_header("Permissions-Policy", PERMISSIONS); self.send_header("Cross-Origin-Opener-Policy", "same-origin")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin"); self.send_header("X-Frame-Options", "DENY")
                if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" and policy.trust_proxy:
                    self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers(); self.wfile.write(data)

        def _json(self, code: int, obj: Any, extra: Optional[Dict[str, str]] = None) -> None:
            self._send(code, "application/json; charset=utf-8", json.dumps(obj, default=str).encode(), extra)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if not (policy is not None and u.path == "/api/health") and not self._host_ok():       # health stays reachable for platform probes whose Host is an internal address; it reveals nothing
                return self._json(403, {"error": "bad host"})
            if not self._gate("get"):
                return
            if u.path in FILES:
                name, ctype = FILES[u.path]
                return self._send(200, ctype, (STATIC / name).read_bytes())
            try:
                out = route_get(u.path, parse_qs(u.query), live_dir)
                if policy is not None and u.path == "/api/health" and isinstance(out, dict):
                    out = {**out, "client_key": policy.client_key(policy.client_ip(self.client_address[0], self.headers.get("X-Forwarded-For") or ""))}
                return self._json(200, out)
            except KeyError:
                return self._json(404, {"error": "not found"})
            except service.ServiceError as e:
                return self._json(e.status, {"error": e.message})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": str(e)[:120]})

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            origin = self.headers.get("Origin")
            if origin and not self._origin_ok(origin):
                return self._json(403, {"error": "bad origin"})
            if policy is not None and not origin and self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                return self._json(403, {"error": "bad origin"})
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return self._json(415, {"error": "json only"})
            if not self._gate("post"):
                return
            if policy is not None and not policy.acquire():
                return self._json(503, {"error": "busy: too many simulations running, try again shortly"}, {"Retry-After": "5"})
            try:
                return self._post_body()
            finally:
                if policy is not None:
                    policy.release()

        def _post_body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0 or n > MAX_BODY:
                    return self._json(413, {"error": "body too large"})
                body = json.loads(self.rfile.read(n).decode() or "{}")
                if not isinstance(body, dict):
                    raise BadRequest
                return self._json(200, route_post(urlparse(self.path).path, body))
            except service.ServiceError as e:
                return self._json(e.status, {"error": e.message})
            except (BadRequest, ValueError, TypeError):
                return self._json(400, {"error": "bad request"})
            except KeyError:
                return self._json(404, {"error": "not found"})

        def do_PUT(self): self._json(405, {"error": "method not allowed"})      # noqa: N802
        do_DELETE = do_PATCH = do_PUT
    return H


def serve(port: int = 0, live_dir: Optional[Path] = None, public: Optional[bool] = None, policy: Optional[PublicPolicy] = None,
          bind: Optional[str] = None) -> Tuple[ThreadingHTTPServer, int]:
    """Loopback by default. In public mode (SENTINEL_PUBLIC=1) the real LLM is off, the host/origin allow-list and limits apply,
    and the server binds 0.0.0.0 on $PORT so a container platform can reach it (put TLS in front; see DEPLOY.md)."""
    public = service.is_public() if public is None else public
    if public and policy is None:
        policy = PublicPolicy()
    if not public:
        policy = None
    holder = {"port": 0}
    host = bind or ("0.0.0.0" if public else "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), make_handler(lambda: holder["port"], live_dir, policy))
    srv.daemon_threads = True
    holder["port"] = srv.server_address[1]
    return srv, holder["port"]
