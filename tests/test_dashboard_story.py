"""Command Center story data and the guided 90-second demo. OFFLINE. The browser test is skipped if Playwright/Chromium is absent."""
import json
import re
import threading
from pathlib import Path

import pytest

from roadworthy.dashboard import service
from roadworthy.dashboard.server import serve
from roadworthy.dashboard.timemachine import replay

STATIC = Path(service.__file__).parent / "static"
ORDER, MERCH = "ZQ7FIXTUREORDER1", "ZQ7FIXTMERCH1"


@pytest.fixture
def evidence(tmp_path):
    d = tmp_path / "us"; d.mkdir()
    (d / "wallet_run_zqfixturerun1.json").write_text(json.dumps({"run_id": "zqfixturerun1", "state": "CREATED", "profile": "us", "order_id": ORDER, "order_status": "PAYER_ACTION_REQUIRED",
                                                                  "amount": "2.00", "currency": "USD", "payee_merchant_id": MERCH, "created_at": "2026-10-31T06:19:16Z", "capture_id": None}))
    return d


def test_story_tells_claim_evidence_verdict_action_from_real_engine_results(evidence):
    st = service.story(evidence)
    c = st["case"]
    assert c["source"] == "SIMULATED" and c["said"] == "All requested actions completed."
    assert c["counts"] == {"VERIFIED": 0, "CONTRADICTED": 6, "UNVERIFIABLE": 0} and len(c["claims"]) == 6
    assert len(c["evidence"]) == 3 and all("exists in the ledger" in e or "is " in e for e in c["evidence"])
    assert {b["tool"] for b in c["blocked"]} == {"create_refund", "accept_dispute_claim", "record_payment_for_invoice"} and len(c["blocked"]) == 4
    assert c["revoked"] == [{"capability": "refund.issue", "from": "APPROVAL_REQUIRED", "to": "BLOCKED"}]
    assert c["needs_human"][0]["tool"] == "create_refund" and "amount=18.00" in c["needs_human"][0]["args"]
    ct = st["contrast"]
    assert ct["counts"]["CONTRADICTED"] == 0 and ct["counts"]["VERIFIED"] == len(ct["claims"]) == 3 and ct["revoked"] == []
    assert [a["tool"] for a in ct["autonomous"]] == ["create_order"]                    # earned autonomy ran on its own; money still waits
    live = st["live"]
    assert live["source"] == "LIVE-RECORDED" and live["claim"] == "Payment was captured" and live["actual"] == "0" and (live["verified"], live["of"]) == (3, 4)


def test_story_without_live_evidence_degrades_cleanly(tmp_path):
    assert service.story(tmp_path)["live"] is None
    assert service.overview(tmp_path)["live"]["available"] is False


def test_headline_numbers_are_computed_not_hard_coded(evidence):
    o = service.overview(evidence)
    proof = {p["label"]: p for p in o["proof"]}
    assert proof["Loss prevented"]["value"] == "$" + format(float(replay("eager")["summary"]["prevented"]), ".2f") == "$348.00"
    assert proof["Claims that were false"]["value"] == f"{o['kpi']['contradicted']} of {o['kpi']['claims']}"
    assert proof["Unsafe actions executed"]["value"] == "0" and proof["Unsafe actions executed"]["tone"] == "ok"
    assert proof["Attacks stopped"]["value"] == "16/16"
    assert o["kpi"]["attacks_stopped"] == 16


def test_alerts_are_grouped_not_repeated(evidence):
    o = service.overview(evidence)
    keys = [(a["sev"], a["title"], a["agent"]) for a in o["alerts"]]
    assert len(keys) == len(set(keys)) and len(o["alerts"]) <= 6
    fc = next(a for a in o["alerts"] if a["title"] == "False claims detected")
    assert fc["count"] == 6
    assert [a["sev"] for a in o["alerts"]] == sorted((a["sev"] for a in o["alerts"]), key=["critical", "warn", "info"].index)


def test_live_claim_text_does_not_leak_the_answer(evidence):
    texts = [c["text"] for c in service.claims(evidence)["claims"] if c["source"] == "LIVE-RECORDED"]
    assert "Payment was captured" in texts and not any("FALSE" in t for t in texts)


# ---- guided demo script (static structure) -----------------------------------------------------------
def test_demo_script_is_five_steps_totalling_ninety_seconds_in_the_requested_order():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    block = js[js.index("const DEMO = ["):js.index("const DEMO_TOTAL")]
    ids = re.findall(r"\{ id: '(\w+)', ms: (\d+)", block)
    assert [i for i, _ in ids] == ["claim", "verify", "revoke", "attack", "time"]
    assert sum(int(ms) for _, ms in ids) == 90000
    titles = re.findall(r"title: '([^']+)'", block)
    assert titles == ["1 · A false claim", "2 · Independent verification", "3 · Permission revoked", "4 · A blocked attack", "5 · Financial Time Machine"]
    assert "?demo=1" not in js and "demo') === '1'" in js and "Escape" in js and "aria-live" in js


def test_every_demo_target_exists_in_the_ui_source():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for t in ("claim-detail", "mismatch", "trust-decision", "attack-", "tm-summary", "demo-panel", "demo-next", "demo-prev", "demo-pause", "demo-exit"):
        assert t in js, t


def test_demo_data_dependencies_exist_in_the_api(evidence):
    cl = service.claims(evidence)["claims"]
    assert any(c["run"] == "eager" and c["text"].startswith("Dispute DSP-9") and c["verdict"] == "CONTRADICTED" for c in cl)          # step 1
    assert any(c["source"] == "LIVE-RECORDED" and c["verdict"] == "CONTRADICTED" and c["mismatch"] for c in cl)                           # step 2
    from roadworthy.dashboard.attacks import run_attack
    assert run_attack("A11")["blocked"] and run_attack("A05")["blocked"]                                                                 # step 4
    t = service.trust("order.create", ["pass"] * 18)
    assert t["current"]["level"] == "AUTO" and service.trust("order.create", ["pass"] * 18 + ["harmful"])["steps"][-1]["revoked"]        # step 3
    assert len(service.timemachine("eager", True, True, False)["frames"]) == 8                                                           # step 5 scrubs 8 frames


# ---- real browser walk-through -----------------------------------------------------------------------
@pytest.fixture
def page(evidence):
    pw = pytest.importorskip("playwright.sync_api")
    s, port = serve(0, evidence)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        p = pw.sync_playwright().start()
        b = p.chromium.launch()
    except Exception as e:
        s.shutdown(); s.server_close()
        pytest.skip(f"no browser available: {type(e).__name__}")
    pg = b.new_page(viewport={"width": 1300, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    pg.base = f"http://127.0.0.1:{port}"; pg.errors = errors
    yield pg
    b.close(); p.stop(); s.shutdown(); s.server_close()


def test_browser_command_center_shows_the_four_stage_story(page):
    page.goto(page.base + "/")
    page.wait_for_selector("[data-t=story-verdict]")
    assert "Agent claim" in page.inner_text("[data-t=story-claim]")
    assert "6 of 6" in page.inner_text("[data-t=story-verdict]") and "CONTRADICTED" in page.inner_text("[data-t=story-verdict]")
    assert "4 blocked" in page.inner_text("[data-t=story-action]") and "BLOCKED" in page.inner_text("[data-t=story-action]")
    body = page.inner_text("body")
    assert "LIVE-RECORDED" in body and "SIMULATED" in body and ORDER not in body and MERCH not in body
    assert page.errors == []


def test_browser_guided_demo_walks_all_five_beats(page):
    page.goto(page.base + "/?demo=1")
    page.wait_for_selector("[data-t=demo-panel]")
    assert "Step 1 of 5" in page.inner_text("[data-t=demo-panel]")
    page.click("[data-t=demo-pause]")                                                     # deterministic: step manually
    expect = [("CONTRADICTED", "DSP-9"), ("LIVE-RECORDED", "captures"), ("REVOKED", None), ("STOPPED", None), ("prevented", None)]
    for i, (needle, extra) in enumerate(expect):
        title = page.inner_text("[data-t=demo-title]")
        assert title.startswith(str(i + 1))
        if i == 0:
            page.wait_for_selector("[data-t=claim-detail]"); assert needle in page.inner_text("[data-t=claim-detail]") and extra in page.inner_text("[data-t=claim-detail]")
        elif i == 1:
            page.wait_for_selector("[data-t=mismatch]"); assert needle in page.inner_text("[data-t=claim-detail]") and extra in page.inner_text("[data-t=mismatch]")
        elif i == 2:
            page.locator("[data-t=trust-decision]").filter(has_text="BLOCKED").wait_for(timeout=12000)
            assert "PERMISSION REVOKED" in page.inner_text("body")
        elif i == 3:
            page.wait_for_selector("[data-t=attack-A11] .badge.b-ok", timeout=8000); assert needle in page.inner_text("[data-t=attack-A11]")
        elif i == 4:
            page.wait_for_selector("[data-t=tm-summary]", timeout=15000); assert needle in page.inner_text("[data-t=tm-summary]") and "$348.00" in page.inner_text("[data-t=tm-summary]")
        if i < 4:
            page.click("[data-t=demo-next]")
    page.click("[data-t=demo-next]")                                                      # Finish
    page.wait_for_selector("[data-t=demo-done]")
    assert page.errors == []


def test_browser_demo_exit_and_keyboard_controls(page):
    page.goto(page.base + "/?demo=1"); page.wait_for_selector("[data-t=demo-panel]")
    page.keyboard.press("ArrowRight"); page.locator("[data-t=demo-title]").filter(has_text="2 ·").wait_for()
    page.keyboard.press("ArrowLeft"); page.locator("[data-t=demo-title]").filter(has_text="1 ·").wait_for()
    page.keyboard.press("Escape")
    assert page.query_selector("[data-t=demo-panel]") is None


def test_browser_llm_panel_offers_no_credential_field(page, monkeypatch):
    for k in ("SENTINEL_LLM_ENABLE", "SENTINEL_LLM_PROVIDER", "SENTINEL_LLM_MODEL", "SENTINEL_LLM_API_KEY", "SENTINEL_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    page.goto(page.base + "/?v=agent&tab=workflow"); page.wait_for_selector("[data-t=agent-select]")
    page.select_option("[data-t=agent-select]", "llm"); page.wait_for_selector("[data-t=llm-disabled]")
    assert "SENTINEL_LLM_ENABLE=1" in page.inner_text("[data-t=llm-disabled]")
    assert page.query_selector("input[type=password]") is None and page.query_selector("input[type=text]") is None and page.query_selector("[data-t=llm-run]") is None
    assert page.errors == []


def test_browser_llm_run_with_fake_provider_lists_model_calls_and_never_shows_the_key(page, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("tl", Path(__file__).parent / "test_dashboard_llm.py")
    tl = importlib.util.module_from_spec(spec); spec.loader.exec_module(tl)
    for k, v in tl.ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(service, "_TRANSPORT", tl.FakeLLM(tl.INJECTED)); monkeypatch.setattr(service, "_BUDGET", tl.CallBudget(100)); service._SESSIONS.clear()
    page.goto(page.base + "/?v=agent&tab=workflow"); page.wait_for_selector("[data-t=agent-select]")
    page.select_option("[data-t=agent-select]", "llm"); page.wait_for_selector("[data-t=llm-consent]")
    assert page.is_disabled("[data-t=llm-run]")                                              # consent gate
    page.check("[data-t=llm-consent]"); page.click("[data-t=llm-run]")
    page.wait_for_selector("text=Model calls")
    body = page.inner_text("body")
    assert "LLM agent · anthropic/test-model-1" in body and "propose T4" in body and tl.KEY not in body
    assert "NOT AUTHORIZED BY OPERATOR" in body and "unknown tool" in body
    assert page.errors == []
