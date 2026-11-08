import pytest
from roadworthy.auth import Credentials, AccessToken, load_credentials, AuthError, fetch_token
from roadworthy.config import assert_sandbox_url, NotSandboxError
from roadworthy.redact import scrub, register_secret
from roadworthy.client import SandboxClient, Response
import json, time


def test_repr_never_reveals_secrets():
    c = Credentials("ID-VALUE-123456", "SECRET-VALUE-654321")
    assert "SECRET" not in repr(c) and "SECRET" not in str(c) and "ID-VALUE" not in repr(c)
    t = AccessToken("TOKEN-VALUE-ABCDEFGH", time.time() + 100)
    assert "TOKEN-VALUE" not in repr(t) and "TOKEN-VALUE" not in str(t)


def test_scrub_removes_registered_secrets_tokens_and_emails():
    register_secret("my-registered-secret-xyz")
    out = scrub("x my-registered-secret-xyz Bearer abc.def.ghi buyer@example.com A21AAexampleexampleexampleexample")
    for bad in ("my-registered-secret-xyz", "abc.def.ghi", "buyer@example.com", "A21AAexample"):
        assert bad not in out


@pytest.mark.parametrize("url", ["https://api-m.paypal.com/v1/x", "http://api-m.sandbox.paypal.com/v1/x",
                                 "https://api-m.sandbox.paypal.com.evil.com/x", "https://evil.com/api-m.sandbox.paypal.com"])
def test_non_sandbox_urls_are_refused(url):
    with pytest.raises(NotSandboxError):
        assert_sandbox_url(url)


def test_sandbox_url_accepted():
    assert_sandbox_url("https://api-m.sandbox.paypal.com/v1/oauth2/token")


class FakeTransport:
    def __init__(self, responses): self.responses, self.requests = list(responses), []
    def request(self, method, url, headers, timeout):
        self.requests.append((method, url)); return self.responses.pop(0)


def test_client_only_issues_GET_and_is_sandbox_bound():
    t = FakeTransport([(200, b"{}")])
    c = SandboxClient(lambda: "tok", transport=t)
    c.get("/v1/reporting/transactions", {"page": 1})
    assert [m for m, _ in t.requests] == ["GET"]
    assert all(u.startswith("https://api-m.sandbox.paypal.com/") for _, u in t.requests)
    assert not any(hasattr(c, n) for n in ("post", "put", "patch", "delete", "request_write"))


def test_client_retries_transient_errors_then_returns():
    t = FakeTransport([(503, b""), (429, b""), (200, b'{"ok": true}')])
    c = SandboxClient(lambda: "tok", transport=t, sleep=lambda s: None)
    r = c.get("/v1/x")
    assert r.ok and r.body == {"ok": True} and len(t.requests) == 3


def test_credential_loader_reports_structure_names_only_never_values():
    class CP:  # fake PowerShell result: structure problem
        returncode = 3; stderr = ""; stdout = json.dumps({"ok": False, "type": "System.Object", "props": ["Foo", "Bar"]})
    import tempfile, pathlib
    f = pathlib.Path(tempfile.mkdtemp()) / "c.xml"; f.write_text("x")
    with pytest.raises(AuthError) as e:
        load_credentials(f, runner=lambda *a, **k: CP())
    assert "Foo" in str(e.value)


def test_credential_loader_success_path_with_fake_powershell(monkeypatch):
    class CP:
        returncode = 0; stderr = ""; stdout = json.dumps({"ok": True, "client_id": "cid-fake-123456", "client_secret": "sec-fake-123456"})
    import tempfile, pathlib
    f = pathlib.Path(tempfile.mkdtemp()) / "c.xml"; f.write_text("x")
    creds = load_credentials(f, runner=lambda *a, **k: CP())
    assert creds.client_id == "cid-fake-123456" and "sec-fake" not in repr(creds)


def test_token_fetch_error_does_not_leak_credentials():
    import urllib.error
    def boom(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    with pytest.raises(AuthError) as e:
        fetch_token(Credentials("cid-xxxxxx", "sec-xxxxxx"), opener=boom)
    assert "sec-xxxxxx" not in str(e.value) and "cid-xxxxxx" not in str(e.value) and "401" in str(e.value)


def test_token_fetch_success_parses_expiry_without_exposing_token():
    class R:
        def read(self): return json.dumps({"access_token": "A21AA-fake-token-value-123456789", "expires_in": 32400, "scope": "a b c"}).encode()
    tok = fetch_token(Credentials("cid", "sec"), opener=lambda req, timeout=30: R(), now=lambda: 1000.0)
    assert tok.expires_at == 1000.0 + 32400 and tok.scope_count == 3 and "A21AA" not in repr(tok)
