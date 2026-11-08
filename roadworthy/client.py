"""Read-only PayPal Sandbox REST client. It exposes GET only: there is no method that can change PayPal state."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Tuple

from .config import SANDBOX_BASE_URL, assert_sandbox_url
from .redact import scrub


class Transport(Protocol):
    def request(self, method: str, url: str, headers: dict, timeout: float) -> Tuple[int, bytes]: ...


class UrllibTransport:
    def request(self, method: str, url: str, headers: dict, timeout: float = 30.0) -> Tuple[int, bytes]:
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() or b""


@dataclass
class Response:
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error_name(self) -> str:
        if isinstance(self.body, dict):
            return scrub(self.body.get("name") or self.body.get("error") or "", 80)
        return ""


class SandboxClient:
    RETRY_STATUS = (429, 500, 502, 503, 504)

    def __init__(self, token_getter: Callable[[], str], transport: Optional[Transport] = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 2):
        self._token = token_getter
        self._t = transport or UrllibTransport()
        self._sleep = sleep
        self._max_retries = max_retries
        self.calls = 0          # number of requests issued (useful for audit/tests)

    def get(self, path: str, params: Optional[dict] = None) -> Response:
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        url = SANDBOX_BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        assert_sandbox_url(url)
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        for attempt in range(self._max_retries + 1):
            self.calls += 1
            status, raw = self._t.request("GET", url, headers, 30.0)
            if status in self.RETRY_STATUS and attempt < self._max_retries:
                self._sleep(0.5 * (2 ** attempt))
                continue
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = None
            return Response(status, body)
        raise RuntimeError("unreachable")
