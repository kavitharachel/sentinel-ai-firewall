"""Layout, readability and label checks for every view at 1366x768 and 1920x1080, including the guided demo. Skipped without Chromium."""
import json
import threading

import pytest

from roadworthy.dashboard.server import serve

ORDER, MERCH = "ZQ7FIXTUREORDER1", "ZQ7FIXTMERCH1"
SIZES = [(1366, 768), (1920, 1080)]
VIEWS = ["command", "audit", "time", "trust", "attack", "agent"]


@pytest.fixture(scope="module")
def evidence(tmp_path_factory):
    d = tmp_path_factory.mktemp("ev") / "us"; d.mkdir()
    (d / "wallet_run_zqfixturerun1.json").write_text(json.dumps({"run_id": "zqfixturerun1", "state": "CREATED", "profile": "us", "order_id": ORDER, "order_status": "PAYER_ACTION_REQUIRED",
                                                                  "amount": "2.00", "currency": "USD", "payee_merchant_id": MERCH, "created_at": "2026-10-31T06:19:16Z", "capture_id": None}))
    return d


@pytest.fixture(scope="module")
def pw_browser(evidence):
    pw = pytest.importorskip("playwright.sync_api")
    s, port = serve(0, evidence)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        p = pw.sync_playwright().start(); b = p.chromium.launch()
    except Exception as e:
        s.shutdown(); s.server_close(); pytest.skip(f"no browser available: {type(e).__name__}")
    yield b, f"http://127.0.0.1:{port}"
    b.close(); p.stop(); s.shutdown(); s.server_close()


@pytest.fixture(params=SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def pg(request, pw_browser):
    b, base = pw_browser
    w, h = request.param
    page = b.new_page(viewport={"width": w, "height": h})
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e))); page.on("console", lambda m: m.type == "error" and errs.append(m.text))
    page.base, page.errors, page.size = base, errs, (w, h)
    yield page
    page.close()


MIN_FONT = r"""() => {
  let min = 99, who = '';
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) { const n = w.currentNode; if (!n.nodeValue.trim()) continue; const e = n.parentElement; if (!e || e.closest('svg,script,style,pre,code,.mono,.hash')) continue;
    const r = e.getBoundingClientRect(); if (!r.width || !r.height) continue; const f = parseFloat(getComputedStyle(e).fontSize); if (f < min) { min = f; who = n.nodeValue.trim().slice(0, 30); } }
  return [min, who]; }"""
SVG_FONT = r"""() => { const s = document.querySelector('svg.tmchart'); if (!s) return null; const k = s.getBoundingClientRect().width / s.viewBox.baseVal.width;
  return Math.min(...[...s.querySelectorAll('text')].map(t => parseFloat(t.getAttribute('font-size')) * k)); }"""


def settle(page, view, extra=""):
    page.goto(f"{page.base}/?v={view}{extra}"); page.wait_for_selector("#view > *:not(p.mut)", timeout=15000); page.wait_for_timeout(300)


def no_hscroll(page):
    return page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")


@pytest.mark.parametrize("view", VIEWS)
def test_every_view_has_no_horizontal_overflow_and_readable_text(pg, view):
    if view == "attack":
        settle(pg, view); pg.click("text=Run all 16 attacks"); pg.wait_for_timeout(900)
    else:
        settle(pg, view)
    assert no_hscroll(pg) <= 1, f"{view} scrolls sideways at {pg.size}"
    size, who = pg.evaluate(MIN_FONT)
    assert size >= 12, f"{view}: {size}px text '{who}'"
    body = pg.inner_text("body")
    assert "SIMULATED" in body and ORDER not in body and MERCH not in body
    assert pg.errors == []


def test_command_center_labels_and_story_fit(pg):
    settle(pg, "command")
    assert "LIVE-RECORDED" in pg.inner_text("body")
    box = pg.locator("[data-t=story]").bounding_box()
    assert box["y"] + 60 < pg.size[1]                                      # the story starts above the fold


def test_claim_audit_hero_is_above_the_fold(pg):
    settle(pg, "audit")
    for sel in ("[data-t=claim-detail] .faceoff", "[data-t=claim-detail] .verdict-bar", "[data-t=enforcement]"):
        b = pg.locator(sel).bounding_box()
        assert b["y"] + b["height"] <= pg.size[1], f"{sel} bottom {b['y'] + b['height']} below {pg.size[1]}"
    t = pg.inner_text("[data-t=claim-detail]")
    assert "AGENT CLAIM" in t and "INDEPENDENT EVIDENCE" in t and "CONTRADICTED" in t and "SIMULATED" in t
    assert "{" not in pg.inner_text("[data-t=claim-detail] .faceoff")                          # no raw JSON in the hero
    assert pg.locator("[data-t=claim-tech]").evaluate("e => e.open") is False                   # technical evidence is collapsed by default
    pg.click("[data-t=claim-tech] summary"); assert '"type"' in pg.inner_text("[data-t=claim-tech]")
    pg.click(".pill:has-text('LIVE-4')"); pg.locator("[data-t=claim-detail]").filter(has_text="Claim LIVE-4").wait_for()
    t = pg.inner_text("[data-t=claim-detail]")
    assert "LIVE-RECORDED" in t and "recorded paypal sandbox" in t.lower() and "captures" in pg.inner_text("[data-t=mismatch]") and "No real payment" in t


def test_time_machine_chart_is_legible_and_labelled(pg):
    settle(pg, "time")
    assert pg.evaluate(SVG_FONT) >= 12
    assert "SENTINEL prevented $348.00" in pg.inner_text("[data-t=tm-summary]")
    t = pg.inner_text("[data-t=tm-world-A]") + pg.inner_text("[data-t=tm-world-B]")
    assert t.count("SIMULATED") == 2 and "play money" in t
    assert pg.locator(".tm-split").is_visible()
    b = pg.locator("[data-t=tm-summary]").bounding_box()
    assert b["y"] + b["height"] <= pg.size[1] + 40


def test_agent_console_fits_and_labels_the_fallback(pg):
    settle(pg, "agent")
    assert no_hscroll(pg) <= 1 and "SIMULATED" in pg.inner_text("[data-t=console-mode-label]")
    pg.click("[data-t=console-suggest-injection]"); pg.click("[data-t=console-send]"); pg.wait_for_selector("[data-t=console-lifecycle]")
    assert no_hscroll(pg) <= 1 and pg.evaluate(MIN_FONT)[0] >= 12
    assert pg.is_visible("[data-t=console-verdict]") and pg.errors == []


def test_demo_controls_never_cover_the_spotlighted_content(pg):
    pg.goto(pg.base + "/?demo=1"); pg.wait_for_selector("[data-t=demo-panel]"); pg.click("[data-t=demo-pause]")
    targets = ["[data-t=claim-detail]", "[data-t=mismatch]", "[data-t=trust-decision]", "[data-t=attack-A11]", "[data-t=tm-summary]"]
    for i, sel in enumerate(targets):
        pg.locator(f"{sel}.spot").wait_for(timeout=20000); pg.wait_for_timeout(900)
        d = pg.locator("[data-t=demo-panel]").bounding_box(); t = pg.locator(f"{sel}.spot").bounding_box()
        assert d["height"] < pg.size[1] * 0.3, f"step {i + 1}: tour strip is {d['height']}px tall"
        assert t["y"] >= d["y"] + d["height"] - 1, f"step {i + 1}: strip ({d['y'] + d['height']}) overlaps the spotlight (top {t['y']})"
        assert t["y"] < pg.size[1] - 80, f"step {i + 1}: spotlight is scrolled out of view"
        assert no_hscroll(pg) <= 1
        # the strip itself must stay readable and fully on screen
        assert d["y"] >= -1 and d["y"] + d["height"] <= pg.size[1]
        if i < 4:
            pg.click("[data-t=demo-next]")
    assert pg.errors == []


@pytest.mark.parametrize("scenario", ["unsafe-refund", "false-claim", "injection"])
def test_one_click_scenarios_fit_and_stay_readable(pg, scenario):
    settle(pg, "agent")
    assert pg.is_visible(f"[data-t=scenario-{scenario}]")
    b = pg.locator("[data-t=console-scenarios]").bounding_box()
    assert b["y"] + b["height"] <= pg.size[1] + 40, "the three scenario buttons must be reachable without scrolling far"
    pg.click(f"[data-t=scenario-{scenario}]"); pg.wait_for_selector("[data-t=console-lifecycle]")
    assert no_hscroll(pg) <= 1 and pg.evaluate(MIN_FONT)[0] >= 12 and "CONTRADICTED" in pg.inner_text("[data-t=console-verdict]")
    assert pg.errors == []
