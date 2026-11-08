"""Recorded-LLM evidence: recorder, stand-alone verifier, loader. A FAKE transport plays the model; nothing here proves a real call happened,
and the tests say so: they check integrity/consistency behaviour only. No network, no key, no PayPal."""
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from roadworthy.dashboard import console, llm_evidence, service
from roadworthy.dashboard.llm import SYSTEM_PROPOSE, CallBudget, LLMError

ROOT = Path(__file__).resolve().parent.parent
KEY = "sk-test-RECORDER-CANARY-0123456789abcdef"
ENV = {"SENTINEL_LLM_ENABLE": "1", "SENTINEL_LLM_PROVIDER": "openai", "SENTINEL_LLM_MODEL": "gpt-4.1-mini", "SENTINEL_LLM_API_KEY": KEY}


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


rec, ver = load_script("record_llm_evidence"), load_script("verify_llm_evidence")


class Fake:
    """OpenAI-shaped fake model: proposes an over-limit refund for every instruction, reports plainly."""
    def __init__(self, fail=False):
        self.n, self.fail = 0, fail

    def __call__(self, url, headers, body, timeout):
        self.n += 1
        if self.fail:
            raise LLMError("fake provider down", 502)
        b = json.loads(body)
        sysmsg = b["messages"][0]["content"] if b["messages"][0]["role"] == "system" else ""
        text = json.dumps({"proposals": [{"tool": "create_refund", "args": {"capture_id": "CAP-1001", "currency_code": "USD", "amount": {"value": "900.00", "currency_code": "USD"}}, "why": "asked"}]}) \
            if SYSTEM_PROPOSE in sysmsg else "I refunded it.\nCLAIMS: []"
        return 200, json.dumps({"choices": [{"message": {"content": text}}]}).encode()


@pytest.fixture
def enabled(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SENTINEL_PUBLIC", raising=False)
    monkeypatch.setattr(service, "_BUDGET", CallBudget(6)); service._SESSIONS.clear()
    monkeypatch.setattr(service, "_TRANSPORT", Fake())
    return monkeypatch


@pytest.fixture
def doc(enabled):
    return rec.build(service, console, llm_evidence)


def reseal(d): return llm_evidence.seal(d)


# ---- recorder -------------------------------------------------------------------------------------------
def test_recorder_builds_a_sealed_document_from_what_actually_happened(doc):
    assert llm_evidence.check(doc) == [] and ver.problems(doc) == []
    assert doc["schema"] == llm_evidence.SCHEMA and doc["provider"] == "openai" and doc["model"] == "gpt-4.1-mini"
    assert [s["id"] for s in doc["scenarios"]] == ["unsafe-refund", "false-claim", "injection"]
    assert doc["total_model_calls"] == sum(len(s["model_calls"]) for s in doc["scenarios"]) and 1 <= doc["total_model_calls"] <= 6
    first = doc["scenarios"][0]
    assert any(p["tool"] == "create_refund" and p["decision"] == "DENIED" and p["executed"] is False for p in first["proposals"])
    assert all(s["ledger_unchanged"] is True and s["cash_out"] == "0" for s in doc["scenarios"])
    assert "SIMULATED" in doc["enforcement"]


def test_recorded_file_contains_no_key_no_raw_text_no_paypal_data(doc):
    blob = json.dumps(doc)
    assert KEY not in blob and "paypal" not in blob.lower().replace("no paypal", "") and "CLAIMS:" not in blob and "I refunded it" not in blob
    assert doc["endpoint_host"] == "api.openai.com"
    assert all(set(c) == {"purpose", "model", "host", "prompt_sha256", "response_sha256", "bytes_out", "bytes_in"} for s in doc["scenarios"] for c in s["model_calls"])


def test_recorder_main_writes_verifies_and_refuses_to_overwrite(enabled, tmp_path, capsys):
    out = tmp_path / "llm_evidence.json"
    assert rec.main(["--out", str(out)]) == 0 and out.is_file()
    assert ver.main(["v", str(out)]) == 0
    assert rec.main(["--out", str(out)]) == 2                     # exists
    enabled.setattr(service, "_BUDGET", CallBudget(6)); service._SESSIONS.clear()      # a real second run is a fresh process with a fresh budget
    assert rec.main(["--out", str(out), "--overwrite"]) == 0
    printed = capsys.readouterr().out
    assert KEY not in printed


def test_recorder_writes_nothing_when_the_provider_fails(enabled, tmp_path):
    enabled.setattr(service, "_TRANSPORT", Fake(fail=True))
    out = tmp_path / "x.json"
    assert rec.main(["--out", str(out)]) == 1 and not out.exists()


def test_recorder_refuses_in_public_mode_and_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTINEL_PUBLIC", "1")
    assert rec.main(["--out", str(tmp_path / "a.json")]) == 2
    monkeypatch.delenv("SENTINEL_PUBLIC")
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit) as e:
        rec.main(["--out", str(tmp_path / "b.json")])
    assert "NOT RUN" in str(e.value) and not (tmp_path / "b.json").exists()


def test_recorder_hard_caps_paid_calls_at_six(enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("SENTINEL_LLM_MAX_CALLS", "500")
    monkeypatch.setattr(service, "_BUDGET", None)
    rec.main(["--out", str(tmp_path / "c.json")])
    import os
    assert os.environ["SENTINEL_LLM_MAX_CALLS"] == "6" and service._budget().limit == 6


# ---- integrity: every tamper is caught, by BOTH the module and the stand-alone verifier ------------------
def mutations(d):
    def m(f):
        x = copy.deepcopy(d); f(x); return x
    yield "edited after sealing", m(lambda x: x["scenarios"][0]["proposals"][0].update(decision="ALLOWED"))
    yield "model renamed (unsealed)", m(lambda x: x.update(model="gpt-5-fake"))
    yield "model renamed (resealed)", reseal(m(lambda x: x.update(model="gpt-5-fake")))
    yield "denied but executed", reseal(m(lambda x: x["scenarios"][0]["proposals"][0].update(decision="DENIED", executed=True)))
    yield "ledger changed", reseal(m(lambda x: x["scenarios"][1].update(ledger_unchanged=False)))
    yield "cash out", reseal(m(lambda x: x["scenarios"][2].update(cash_out="900.00")))
    yield "call count inflated", reseal(m(lambda x: x.update(total_model_calls=x["total_model_calls"] + 1)))
    yield "no calls in a scenario", reseal(m(lambda x: x["scenarios"][0].update(model_calls=[])))
    yield "non-provider endpoint (resealed)", reseal(m(lambda x: x.update(endpoint_host="127.0.0.1")))
    yield "call host differs (resealed)", reseal(m(lambda x: x["scenarios"][0]["model_calls"][0].update(host="evil.example.com")))
    yield "bad hash", reseal(m(lambda x: x["scenarios"][0]["model_calls"][0].update(prompt_sha256="zz")))
    yield "scenario missing", reseal(m(lambda x: x["scenarios"].pop()))
    yield "unknown provider", reseal(m(lambda x: x.update(provider="made-up")))
    yield "bad time", reseal(m(lambda x: x.update(recorded_at="yesterday")))
    yield "wrong schema", reseal(m(lambda x: x.update(schema="other/1")))
    yield "key smuggled in", reseal(m(lambda x: x.update(note="sk-live-abcdefghijklmnop1234")))
    yield "over six calls", reseal(m(lambda x: x["scenarios"][0]["model_calls"].extend([x["scenarios"][0]["model_calls"][0]] * 7)))


def test_every_tamper_is_detected_by_module_and_verifier(doc):
    for name, bad in mutations(doc):
        assert llm_evidence.check(bad), f"module missed: {name}"
        assert ver.problems(bad), f"verifier missed: {name}"


def test_module_and_verifier_agree_on_valid_input(doc):
    assert (llm_evidence.check(doc) == []) == (ver.problems(doc) == [])


def test_verifier_is_standalone_and_offline():
    src = (ROOT / "scripts" / "verify_llm_evidence.py").read_text()
    import ast
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import)} | {n.module.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= {"hashlib", "json", "re", "sys"}, mods


def test_verifier_cli_exit_codes(doc, tmp_path):
    good, bad = tmp_path / "g.json", tmp_path / "b.json"
    good.write_text(json.dumps(doc)); b = copy.deepcopy(doc); b["model"] = "x"; bad.write_text(json.dumps(b))
    run = lambda *a: subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "verify_llm_evidence.py"), *a], capture_output=True, text=True, timeout=30)
    r = run(str(good)); assert r.returncode == 0 and "PASS" in r.stdout and "not tamper-proof" in r.stdout
    assert run(str(bad)).returncode == 1
    assert run(str(tmp_path / "nope.json")).returncode == 2 and run().returncode == 2
    (tmp_path / "junk.json").write_text("{not json"); assert run(str(tmp_path / "junk.json")).returncode == 2


# ---- loader / UI contract -------------------------------------------------------------------------------
def test_loader_not_recorded_by_default_and_never_invents(tmp_path):
    shipped = ROOT / "public_data" / "llm_evidence.json"
    d = llm_evidence.load(tmp_path / "missing.json")
    assert d["available"] is False and d["label"] == "LLM-RECORDED" and d["replayed"] is True and "scenarios" not in d
    if not shipped.exists():                                      # nothing is shipped until the maintainer records a genuine run
        assert llm_evidence.load()["available"] is False


def test_loader_shows_a_valid_file_and_hides_a_tampered_one(doc, tmp_path):
    p = tmp_path / "e.json"; p.write_text(json.dumps(doc))
    d = llm_evidence.load(p)
    assert d["available"] and d["verified"] and d["model"] == "gpt-4.1-mini" and len(d["scenarios"]) == 3 and d["self_sha256"] == doc["self_sha256"]
    assert "tamper-evident" in d["limits"].lower() and "not tamper-proof" in d["limits"].lower() and "replayed" in d["meaning"] or d["replayed"]
    t = copy.deepcopy(doc); t["scenarios"][0]["proposals"][0]["decision"] = "ALLOWED"; p.write_text(json.dumps(t))
    d2 = llm_evidence.load(p)
    assert d2["available"] is False and "failed its integrity" in d2["note"] and "scenarios" not in d2


def test_loader_survives_garbage_and_huge_files(tmp_path):
    p = tmp_path / "g.json"
    p.write_text("not json"); assert llm_evidence.load(p)["available"] is False
    p.write_text("[]"); assert llm_evidence.load(p)["available"] is False
    p.write_bytes(b"x" * (llm_evidence.MAX_BYTES + 1)); assert llm_evidence.load(p)["available"] is False


def test_public_endpoint_serves_the_verified_record_without_calling_a_model(doc, tmp_path, monkeypatch):
    p = tmp_path / "e.json"; p.write_text(json.dumps(doc))
    monkeypatch.setattr(llm_evidence, "DEFAULT_PATH", p)
    monkeypatch.setenv("SENTINEL_PUBLIC", "1")
    calls = []
    monkeypatch.setattr(service, "_TRANSPORT", lambda *a, **k: calls.append(1))
    d = service.llm_evidence_info()
    assert d["available"] and calls == [] and KEY not in json.dumps(d)
