"""Sandbox authentication using the existing Windows-DPAPI-encrypted credential file (Export-Clixml).

Security properties:
  * The secret exists only in this process's memory. It is never printed, logged, written, or sent to an LLM.
  * Decryption is performed by Windows PowerShell as the current Windows user (DPAPI); the file is useless elsewhere.
  * Credentials/AccessToken objects have redacted repr() so accidental printing cannot leak them.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import DEFAULT_CRED_FILE, SANDBOX_BASE_URL, assert_sandbox_url
from .redact import register_secret, scrub


REFUND_SCOPE = "https://uri.paypal.com/services/payments/refund"


class AuthError(RuntimeError):
    pass


@dataclass
class Credentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def __repr__(self) -> str:  # never reveal values
        return "Credentials(client_id=<hidden>, client_secret=<hidden>)"

    __str__ = __repr__

    def fingerprint(self) -> str:
        """Short hash of the Client ID only (never the ID or secret). Used to tell profiles/credential sets apart."""
        return hashlib.sha256(("sentinel-profile|" + self.client_id).encode("utf-8")).hexdigest()[:12]


@dataclass
class AccessToken:
    value: str = field(repr=False)
    expires_at: float
    scope_count: int = 0
    has_refund_scope: bool = False     # boolean only; scope names are never stored or printed

    def __repr__(self) -> str:
        return f"AccessToken(<hidden>, expires_in={int(self.expires_at - time.time())}s)"

    __str__ = __repr__

    def is_valid(self, skew: float = 60.0) -> bool:
        return time.time() + skew < self.expires_at


_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
function Plain($s) {
  if ($s -is [System.Security.SecureString]) {
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
  }
  return [string]$s
}
$o = Import-Clixml -LiteralPath $env:RW_CRED_PATH
$id = $null; $sec = $null; $names = @()
if ($o -is [System.Management.Automation.PSCredential]) {
  $id = $o.UserName; $sec = Plain $o.Password
} else {
  if ($o -is [System.Collections.IDictionary]) { $pairs = $o.GetEnumerator() | ForEach-Object { @{ N = [string]$_.Key; V = $_.Value } } }
  else { $pairs = $o.PSObject.Properties | ForEach-Object { @{ N = $_.Name; V = $_.Value } } }
  foreach ($p in $pairs) {
    $names += $p.N
    if ($p.N -match 'client.?id|^user') { $id = Plain $p.V }
    elseif ($p.N -match 'secret|pass|key') { $sec = Plain $p.V }
  }
}
if (-not $id -or -not $sec) { (@{ ok = $false; type = $o.GetType().FullName; props = $names } | ConvertTo-Json -Compress); exit 3 }
(@{ ok = $true; client_id = $id; client_secret = $sec } | ConvertTo-Json -Compress)
"""


def _encoded_ps() -> str:
    return base64.b64encode(_PS_SCRIPT.encode("utf-16-le")).decode("ascii")


def load_credentials(path: Optional[Path] = None, runner: Optional[Callable] = None) -> Credentials:
    """Load credentials. Windows: decrypt the DPAPI file via PowerShell. Fallback: env vars (explicit opt-in)."""
    if os.environ.get("ROADWORTHY_CRED_SOURCE", "").lower() == "env":
        cid, sec = os.environ.get("PAYPAL_CLIENT_ID"), os.environ.get("PAYPAL_CLIENT_SECRET")
        if not (cid and sec):
            raise AuthError("ROADWORTHY_CRED_SOURCE=env but PAYPAL_CLIENT_ID/PAYPAL_CLIENT_SECRET are not set.")
        register_secret(sec)
        return Credentials(cid, sec)

    cred_path = Path(path) if path else DEFAULT_CRED_FILE
    if not cred_path.is_file():
        raise AuthError(f"Credential file not found: {cred_path.name}")
    run = runner or subprocess.run
    last_err = "no PowerShell found"
    for exe in ("powershell.exe", "pwsh"):
        try:
            cp = run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded_ps()],
                     env={**os.environ, "RW_CRED_PATH": str(cred_path)}, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            last_err = "PowerShell timed out"
            continue
        out = (cp.stdout or "").strip()
        try:
            data = json.loads(out.splitlines()[-1]) if out else {}
        except json.JSONDecodeError:
            data = {}
        if cp.returncode == 0 and data.get("ok"):
            register_secret(data["client_secret"])
            return Credentials(data["client_id"], data["client_secret"])
        if data.get("ok") is False:   # structure problem: report NAMES only, never values
            raise AuthError(f"Credential file has an unexpected structure: type={data.get('type')}, "
                            f"properties={data.get('props')}. Re-create it with Export-Clixml of a PSCredential "
                            f"(UserName=Client ID, Password=Secret).")
        last_err = scrub((cp.stderr or "")[:200]) or f"exit code {cp.returncode}"
    raise AuthError(f"Could not decrypt the credential file ({last_err}). It can only be read by the same Windows user "
                    f"on the same machine that created it.")


def fetch_token(creds: Credentials, opener: Optional[Callable] = None, now: Callable[[], float] = time.time) -> AccessToken:
    """Client-credentials grant against the SANDBOX token endpoint."""
    url = assert_sandbox_url(f"{SANDBOX_BASE_URL}/v1/oauth2/token")
    basic = base64.b64encode(f"{creds.client_id}:{creds.client_secret}".encode()).decode()
    req = urllib.request.Request(url, data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                                 method="POST", headers={"Authorization": f"Basic {basic}", "Accept": "application/json",
                                                         "Content-Type": "application/x-www-form-urlencoded"})
    try:
        resp = (opener or urllib.request.urlopen)(req, timeout=30)
        body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise AuthError(f"Token request rejected: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise AuthError(f"Token request failed: {scrub(e)}") from None
    tok = body.get("access_token")
    if not tok:
        raise AuthError("Token response had no access_token")
    register_secret(tok)
    scopes = str(body.get("scope", "")).split()
    return AccessToken(tok, now() + float(body.get("expires_in", 0)), len(scopes), REFUND_SCOPE in scopes)


class TokenProvider:
    """Caches the token in memory and refreshes it when near expiry."""

    def __init__(self, creds: Optional[Credentials] = None, fetch: Callable = fetch_token):
        self._creds = creds
        self._fetch = fetch
        self._tok: Optional[AccessToken] = None

    def get(self) -> str:
        if self._tok is None or not self._tok.is_valid():
            if self._creds is None:
                self._creds = load_credentials()
            self._tok = self._fetch(self._creds)
        return self._tok.value

    @property
    def info(self) -> Optional[AccessToken]:
        return self._tok

    def fingerprint(self) -> Optional[str]:
        return self._creds.fingerprint() if self._creds is not None else None
