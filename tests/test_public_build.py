"""The public artifact: strictly allow-listed, secret-free, self-contained, and runnable on its own. OFFLINE; no PayPal; no model."""
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_public", ROOT / "scripts" / "build_public.py")
bp = importlib.util.module_from_spec(spec); spec.loader.exec_module(bp)

CANARY = "CANARY-pub-7d1f3a9c5e2b"
ALLOWED_TOP = {"roadworthy", "data", "public_data", "LICENSE", "NOTICE", "DEPLOY.md", "MANIFEST.sha256.json"}


@pytest.fixture(scope="module")
def art(tmp_path_factory):
    out = tmp_path_factory.mktemp("art") / "public"
    mods, mf, problems = bp.build(out, [CANARY])
    return out, mods, mf, problems


def files(out): return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())


def test_build_is_clean(art):
    out, mods, mf, problems = art
    assert problems == [] and (out / "MANIFEST.sha256.json").is_file()


def test_only_allow_listed_things_are_copied(art):
    out, *_ = art
    assert {p.name for p in out.iterdir()} <= ALLOWED_TOP
    for f in files(out):
        top = f.split("/")[0]
        assert top in ALLOWED_TOP, f
        if top == "roadworthy":
            assert f.endswith((".py", ".js", ".css", ".html")), f
        if top == "data":
            assert f == "data/toolkit_manifest.json"
        if top == "public_data":
            assert re.fullmatch(r"public_data/(public_evidence|llm_evidence)\.json", f), f


def test_forbidden_material_is_absent(art):
    out, *_ = art
    names = files(out)
    bad = re.compile(r"(?i)(\.xml$|(^|/)\.env|\.pem$|\.key$|credentials|(^|/)(evidence|tests|scripts)/|__pycache__|\.pyc$|wallet_run|profile_check)")
    assert [f for f in names if bad.search(f)] == []
    mods = set(art[1])
    assert not any(set(m.split(".")) & bp.FORBIDDEN_MODULES for m in mods)
    for forbidden in ("client", "auth", "wallet_run", "capture", "profiles", "txsearch", "snapshot", "cli"):
        assert not (out / "roadworthy" / f"{forbidden}.py").exists(), forbidden


def test_no_private_identifier_or_canary_text_anywhere(art):
    out, *_ = art
    lits = bp.private_literals()
    if (ROOT / "evidence").is_dir():           # evidence/ is gitignored: a fresh clone has none, and the canary/secret checks below still run
        assert lits, "the private evidence folder should have been read for identifiers"
    blob = "".join(p.read_text(errors="replace") for p in out.rglob("*") if p.is_file())
    assert CANARY not in blob
    for lit in lits:
        assert lit not in blob, "private identifier leaked into the artifact"
    for rx in bp.SECRET_RX:
        assert not rx.search(blob), rx.pattern


def test_manifest_hashes_match_every_file(art):
    out, *_ = art
    m = json.loads((out / "MANIFEST.sha256.json").read_text())
    listed = set(m["files"])
    assert listed == set(files(out)) - {"MANIFEST.sha256.json"}
    for rel, meta in m["files"].items():
        assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == meta["sha256"], rel


def test_artifact_has_licence_notice_and_is_tamper_checked_by_rebuild(art, tmp_path):
    out, *_ = art
    assert "MIT License" in (out / "LICENSE").read_text() and "not affiliated" in (out / "NOTICE").read_text().lower()
    _, mf2, p2 = bp.build(tmp_path / "again", [CANARY])
    assert p2 == [] and {k: v["sha256"] for k, v in mf2.items()} == {k: v["sha256"] for k, v in art[2].items()}       # reproducible


# ---- the scanner really catches things --------------------------------------------------------------------
@pytest.mark.parametrize("name,content,expect", [
    ("creds.xml", "<x/>", "denied file name"),
    (".env", "A=1", "denied file name"),
    ("server.pem", "x", "denied file name"),
    ("k.py", "KEY = 'sk-" + "a" * 30 + "'", "secret-shaped"),
    ("k2.py", "-----BEGIN RSA PRIVATE KEY-----", "secret-shaped"),
    ("cid.json", json.dumps({"id": "A" + "x" * 79}), "secret-shaped"),
    ("h.py", "h = 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz'", "secret-shaped"),
    ("s.py", "client_secret = 'abcdefghijkl1234'", "secret-shaped"),
    ("c.txt", "see " + CANARY, "private identifier/canary"),
    ("blob.bin", "x", "unexpected file type"),
])
def test_scanner_flags_planted_problems(tmp_path, name, content, expect):
    (tmp_path / name).write_text(content)
    assert any(expect in p for p in bp.scan(tmp_path, [CANARY])), (name, bp.scan(tmp_path, [CANARY]))


def test_scanner_flags_denied_directories(tmp_path):
    for d in ("evidence", "tests", "scripts"):
        (tmp_path / d).mkdir(); (tmp_path / d / "a.json").write_text("{}")
    got = bp.scan(tmp_path)
    assert all(any(f"{d}/a.json" in p for p in got) for d in ("evidence", "tests", "scripts"))


def test_scanner_ignores_harmless_words(tmp_path):
    (tmp_path / "ok.py").write_text("# the client_secret and api_key words appear in docs; no values here\nSECRETISH = 'password'\n")
    assert bp.scan(tmp_path) == []


def test_build_fails_if_the_closure_ever_reaches_the_paypal_client(tmp_path, monkeypatch):
    real = bp.closure
    monkeypatch.setattr(bp, "closure", lambda entry=None: real(bp.ENTRY + ["roadworthy.client"]))
    with pytest.raises(SystemExit) as e:
        bp.build(tmp_path / "bad")
    assert "forbidden" in str(e.value) and "client" in str(e.value)


def test_build_never_leaves_a_partial_artifact_with_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "private_literals", lambda: {"static"})            # 'static' appears in the shipped code -> must be reported
    _, _, problems = bp.build(tmp_path / "x")
    assert problems


def test_import_closure_matches_the_dashboard_test_boundary():
    mods = bp.closure()
    assert {"roadworthy.dashboard.server", "roadworthy.permissions.gateway", "roadworthy.eval.verifier", "roadworthy.dashboard.llm_evidence"} <= set(mods)
    assert len(mods) < 40


# ---- docker files -----------------------------------------------------------------------------------------
def test_dockerfile_is_multistage_non_root_secretless_and_public():
    d = (ROOT / "deploy" / "Dockerfile").read_text()
    assert d.count("FROM ") == 2 and "scripts/build_public.py" in d and "COPY --from=build" in d
    assert "USER 10001" in d and "SENTINEL_PUBLIC=1" in d and "--public" in d and "HEALTHCHECK" in d
    assert not re.search(r"(?i)(api[_-]?key|secret|password|token)\s*=", d)
    final = d.split("FROM python:3.12-slim\n")[-1]
    assert "COPY . " not in final and "evidence" not in final           # the final image only receives the built artifact
    for name in (".dockerignore", "deploy/dockerignore"):
        ig = (ROOT / name).read_text()
        for pat in ("evidence", "*.xml", ".env*", "tests", "build"):
            assert pat in ig, (name, pat)
        assert "scripts" not in ig.split()                              # the builder stage needs scripts/
    y = (ROOT / "deploy" / "render.yaml").read_text()
    assert "numInstances: 1" in y and "autoDeployTrigger" in y and "plan: starter" not in y and "sync: false" in y and "healthCheckPath: /api/health" in y and "SENTINEL_PUBLIC_HOST" in y and not re.search(r"(?i)(key|secret|token|password)\w*:\s*\S*[A-Za-z0-9]{20,}", y)


def test_no_secret_shaped_text_in_the_tracked_source_tree():
    skip = {"evidence", "tests", "build", "__pycache__", ".git", ".pytest_cache", "node_modules"}
    hits = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or set(p.relative_to(ROOT).parts) & skip or p.suffix in (".pyc", ".png", ".gif"):
            continue
        try:
            t = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for rx in bp.SECRET_RX:
            if rx.search(t):
                hits.append((p.relative_to(ROOT).as_posix(), rx.pattern[:20]))
    assert hits == []


# ---- the artifact runs by itself, with decoy secrets in its environment -----------------------------------
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_artifact_runs_standalone_and_ignores_decoy_secrets(art):
    out, *_ = art
    port = free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    env.update({"SENTINEL_PUBLIC": "1", "SENTINEL_PUBLIC_HOST": "demo.example.com", "PORT": str(port), "PYTHONDONTWRITEBYTECODE": "1",
                "SENTINEL_LLM_ENABLE": "1", "SENTINEL_LLM_PROVIDER": "openai", "SENTINEL_LLM_MODEL": "gpt-4.1-mini", "SENTINEL_LLM_API_KEY": CANARY,
                "PAYPAL_CLIENT_SECRET": CANARY + "-pp"})
    imp = subprocess.run([sys.executable, "-c", "import roadworthy.client"], cwd=out, env=env, capture_output=True, text=True)
    assert imp.returncode != 0 and "No module named" in imp.stderr
    p = subprocess.Popen([sys.executable, "-m", "roadworthy.dashboard", "--public"], cwd=out, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1); break
            except Exception:
                if p.poll() is not None:
                    pytest.fail("artifact exited: " + p.stderr.read())
                time.sleep(0.25)
        get = lambda path, host=None: urllib.request.urlopen(urllib.request.Request(base + path, headers={"Host": host or f"127.0.0.1:{port}"}), timeout=5).read().decode()
        assert json.loads(get("/api/health"))["public"] is True
        blob = "".join(get(x) for x in ("/api/overview", "/api/console/info", "/api/llm/status", "/api/claims", "/api/llm-evidence"))
        assert CANARY not in blob and json.loads(get("/api/console/info"))["llm"]["enabled"] is False
        assert "WITHHELD" in blob and "PAYER_ACTION_REQUIRED" in blob
        with pytest.raises(urllib.error.HTTPError) as e:
            get("/api/overview", host="evil.test")
        assert e.value.code == 403
        req = urllib.request.Request(base + "/api/console/run", data=json.dumps({"prompt": "Refund $18.00 on CAP-1003", "source": "operator", "agent": "llm", "consent": True}).encode(),
                                     headers={"Content-Type": "application/json", "Host": "demo.example.com", "Origin": "https://demo.example.com"})
        with pytest.raises(urllib.error.HTTPError) as e2:
            urllib.request.urlopen(req, timeout=5)
        assert e2.value.code == 403
    finally:
        p.terminate()
        try:
            out_, err_ = p.communicate(timeout=5)
        except Exception:
            p.kill(); out_, err_ = p.communicate()
    assert CANARY not in (out_ or "") + (err_ or "")           # nothing logged
