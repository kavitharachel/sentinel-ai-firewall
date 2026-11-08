"""Fixed configuration. The sandbox host is a constant; there is deliberately no setting that selects Live."""
from __future__ import annotations
from pathlib import Path
from urllib.parse import urlparse

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
SANDBOX_HOST = "api-m.sandbox.paypal.com"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CRED_FILE = PROJECT_ROOT / "paypal-sandbox-credentials.xml"
MANIFEST_PATH = PROJECT_ROOT / "data" / "toolkit_manifest.json"


class NotSandboxError(RuntimeError):
    pass


def assert_sandbox_url(url: str) -> str:
    """Raise unless url is https on the sandbox host. Every outbound request passes through this."""
    p = urlparse(url)
    if p.scheme != "https" or p.hostname != SANDBOX_HOST:
        raise NotSandboxError(f"Refusing non-sandbox URL host: {p.hostname!r}")
    return url
