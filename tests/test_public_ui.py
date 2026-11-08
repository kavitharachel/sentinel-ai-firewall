"""Browser checks for the PUBLIC demo at five sizes: no 'null'/'undefined'/'NaN' text, no sideways scroll, readable text, banner, no LLM option,
guided demo not hiding what it spotlights. Skipped without Chromium. OFFLINE."""
import json
import re
import threading

import pytest

from roadworthy.dashboard import console, llm_evidence
from roadworthy.dashboard.server import serve

SIZES = [(1920, 1080), (1366, 768), (1024, 768), (768, 1024), (390, 844)]
VIEWS = ["command", "audit", "time", "trust", "attack", "agent"]
BAD_TEXT = re.compile(r"\b(null|undefined|NaN)\b|\[object|\bObject\]")
HOST = "127.0.0.1"


@pytest.fixture(scope="module")
def pub_server(tmp_path_factory):
    import os
    old = {k: os.environ.get(k) for k in ("SENTINEL_PUBLIC", "SENTINEL_PUBLIC_HOST", "SENTINEL_RATE_POST_PER_MIN", "SENTINEL_RATE_GLOBAL_POST_PER_MIN", "SENTINEL_RATE_GET_PER_MIN")}
    os.environ["SENTINEL_PUBLIC"] = "1"; os.environ["SENTINEL_PUBLIC_HOST"] = "demo.example.com"
    os.environ["SENTINEL_RATE_POST_PER_MIN"] = os.environ["SENTINEL_RATE_GLOBAL_POST_PER_MIN"] = "100000"; os.environ["SENTINEL_RATE_GET_PER_MIN"] = "100000"   # one test client, hundreds of page loads
    s, port = serve(0, bind="127.0.0.1")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield port
    s.shutdown(); s.server_close()
    for k, v in old.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@pytest.fixture(scope="module")
def browser(pub_server):
    pw = pytest.importorskip("playwright.sync_api")
    try:
        p = pw.sync_playwright().start(); b = p.chromium.launch()
    except Exception as e:
        pytest.skip(f"no browser available: {type(e).__name__}")
    yield b, f"http://{HOST}:{pub_server}"
    b.close(); p.stop()


@pytest.fixture(params=SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def pg(request, browser):
    b, base = browser
    w, h = request.param
    page = b.new_page(viewport={"width": w, "height": h})
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e))); page.on("console", lambda m: m.type == "error" and errs.append(m.text))
    page.base, page.errors, page.size = base, errs, (w, h)
    yield page
    page.close()


VISIBLE_TEXT = r"""() => { const out = []; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) { const n = w.currentNode; const t = n.nodeValue.trim(); if (!t) continue; const e = n.parentElement;
    if (!e || e.closest('script,style,pre,code,.mono,.hash')) continue; const r = e.getBoundingClientRect(); if (r.width && r.height) out.push(t); }
  return out; }"""
MIN_FONT = r"""() => { let min = 99, who = ''; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) { const n = w.currentNode; if (!n.nodeValue.trim()) continue; const e = n.parentElement; if (!e || e.closest('svg,script,style,pre,code,.mono,.hash')) continue;
    const r = e.getBoundingClientRect(); if (!r.width || !r.height) continue; const f = parseFloat(getComputedStyle(e).fontSize); if (f < min) { min = f; who = n.nodeValue.trim().slice(0, 30); } }
  return [min, who]; }"""


def settle(page, view, extra=""):
    page.goto(f"{page.base}/?v={view}{extra}"); page.wait_for_selector("#view > *:not(p.mut)", timeout=15000); page.wait_for_timeout(300)


def hscroll(page): return page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")


def assert_clean(page, label):
    bad = [t for t in page.evaluate(VISIBLE_TEXT) if BAD_TEXT.search(t)]
    assert bad == [], f"{label}: visible placeholder text {bad[:3]}"
    assert hscroll(page) <= 1, f"{label}: scrolls sideways at {page.size}"
    size, who = page.evaluate(MIN_FONT)
    assert size >= 12, f"{label}: {size}px text '{who}'"
    assert page.errors == [], page.errors


@pytest.mark.parametrize("view", VIEWS)
def test_every_public_view_is_clean_at_every_size(pg, view):
    settle(pg, view)
    if view == "attack":
        pg.click("text=Run all 16 attacks"); pg.wait_for_timeout(900)
    assert_clean(pg, view)
    body = pg.inner_text("body")
    assert "PUBLIC DEMO" in body and "does not protect any live PayPal transaction" in body
    assert "SIMULATED" in body and "HS7EQP" not in body and "6NN049" not in body


def test_banner_and_nav_are_usable_on_small_screens(pg):
    settle(pg, "command")
    assert pg.is_visible("[data-t=public-banner]")
    for v in VIEWS:
        b = pg.locator(f"[data-t=nav-{v}]")
        b.scroll_into_view_if_needed(); assert b.is_visible()
    pg.locator("[data-t=nav-time]").click(); pg.wait_for_selector("[data-t=tm-summary]")
    assert_clean(pg, "after nav")


def test_console_does_not_render_null_and_has_no_llm_option(pg):
    settle(pg, "agent")
    assert_clean(pg, "console empty")
    opts = pg.locator("[data-t=console-agent] option").all_inner_texts()
    assert opts and not any("Real LLM" in o for o in opts)
    assert pg.locator("[data-t=llm-recorded]").is_visible() and "LLM-RECORDED" in pg.inner_text("[data-t=llm-recorded]")
    assert "No recorded run" in pg.inner_text("[data-t=llm-recorded]") or "recorded" in pg.inner_text("[data-t=llm-recorded]").lower()
    assert "enable_with" not in pg.content() and "SENTINEL_LLM" not in pg.inner_text("body")


@pytest.mark.parametrize("scenario", ["unsafe-refund", "false-claim", "injection"])
def test_three_scenarios_work_clean_and_labelled(pg, scenario):
    settle(pg, "agent")
    pg.click(f"[data-t=scenario-{scenario}]"); pg.wait_for_selector("[data-t=console-lifecycle]")
    assert_clean(pg, scenario)
    t = pg.inner_text("[data-t=console-lifecycle]")
    assert "SIMULATED" in t and "not an AI model" in pg.inner_text("[data-t=console-mode-label]").lower() or "SCRIPTED" in pg.inner_text("[data-t=console-mode-label]")
    assert pg.is_visible("[data-t=console-verdict]")
    low = t.lower()
    assert "asked for by the operator" not in low and "customer message (untrusted data)" not in low
    expect = {"unsafe-refund": "DENIED", "false-claim": "CONTRADICTED", "injection": "DENIED"}[scenario]
    assert expect in t


def test_second_turn_with_history_renders_no_null(pg):
    settle(pg, "agent")
    for s in ("unsafe-refund", "injection"):
        pg.click(f"[data-t=scenario-{s}]"); pg.wait_for_selector("[data-t=console-lifecycle]"); pg.wait_for_timeout(200)
    assert pg.locator("[data-t=console-thread]").is_visible()
    assert_clean(pg, "history")


def test_trust_labels_use_merchant_staff_wording(pg):
    settle(pg, "agent", "&tab=workflow")
    pg.wait_for_timeout(600)
    t = pg.inner_text("body").lower()
    assert "asked for by the operator" not in t and "customer message (untrusted data)" not in t


def test_guided_demo_never_hides_what_it_spotlights(pg):
    pg.goto(pg.base + "/?demo=1"); pg.wait_for_selector("[data-t=demo-panel]"); pg.click("[data-t=demo-pause]")
    targets = ["[data-t=claim-detail]", "[data-t=mismatch]", "[data-t=trust-decision]", "[data-t=attack-A11]", "[data-t=tm-summary]"]
    for i, sel in enumerate(targets):
        pg.locator(f"{sel}.spot").wait_for(timeout=20000); pg.wait_for_timeout(900)
        d = pg.locator("[data-t=demo-panel]").bounding_box(); t = pg.locator(f"{sel}.spot").bounding_box()
        ix = max(0, min(d["x"] + d["width"], t["x"] + t["width"]) - max(d["x"], t["x"])); iy = max(0, min(d["y"] + d["height"], t["y"] + t["height"]) - max(d["y"], t["y"]))
        covered = ix * iy / max(1, t["width"] * t["height"])
        assert covered < 0.25 or t["height"] > pg.size[1] * 0.6, f"step {i + 1}: strip covers {covered:.0%} of the spotlight at {pg.size}"
        assert d["height"] < pg.size[1] * 0.5 and d["y"] >= -1 and d["y"] + d["height"] <= pg.size[1] + 1, f"step {i + 1}: strip geometry {d}"
        assert hscroll(pg) <= 1
        if i < 4:
            pg.click("[data-t=demo-next]")
    assert pg.errors == []


def test_recorded_llm_panel_shows_a_valid_file_and_labels_it(pg, tmp_path, monkeypatch):
    """UI contract only: the file here is built from a fake transport in test_llm_evidence-style data, not a real model run."""
    sc = [{"id": i, "label": i, "prompt": "p", "source": "operator", "model_calls": [{"purpose": "x", "model": "gpt-4.1-mini", "host": "api.openai.com", "prompt_sha256": "a" * 16, "response_sha256": "b" * 16, "bytes_out": 1, "bytes_in": 1}],
           "proposals": [{"tool": "create_refund", "args_short": "CAP-1001 $900.00", "decision": "DENIED", "outcome": "blocked", "executed": False, "layer": "limits"}],
           "verdict": "NO CLAIMS", "ledger_unchanged": True, "cash_out": "0"} for i in ("unsafe-refund", "false-claim", "injection")]
    doc = llm_evidence.seal({"schema": llm_evidence.SCHEMA, "recorded_at": "2026-10-31T10:00:00Z", "provider": "openai", "model": "gpt-4.1-mini", "endpoint_host": "api.openai.com", "total_model_calls": 3, "scenarios": sc})
    p = tmp_path / "e.json"; p.write_text(json.dumps(doc))
    monkeypatch.setattr(llm_evidence, "DEFAULT_PATH", p)
    settle(pg, "agent")
    t = pg.inner_text("[data-t=llm-recorded]")
    assert "LLM-RECORDED" in t and "gpt-4.1-mini" in t and "api.openai.com" in t and "replayed" in t.lower() and "not tamper-proof" in t.lower() and "verify_llm_evidence.py" in t
    assert_clean(pg, "recorded panel")
