"""The 'Start here' strip on the Command Center: three labelled steps, each leads where it says. OFFLINE; skipped without Chromium."""
import os
import threading

import pytest

from roadworthy.dashboard.server import serve

HOST = "127.0.0.1"


@pytest.fixture(scope="module")
def base():
    keys = ("SENTINEL_PUBLIC", "SENTINEL_PUBLIC_HOST", "SENTINEL_RATE_POST_PER_MIN", "SENTINEL_RATE_GLOBAL_POST_PER_MIN", "SENTINEL_RATE_GET_PER_MIN")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({"SENTINEL_PUBLIC": "1", "SENTINEL_PUBLIC_HOST": "demo.example.com", "SENTINEL_RATE_POST_PER_MIN": "100000",
                       "SENTINEL_RATE_GLOBAL_POST_PER_MIN": "100000", "SENTINEL_RATE_GET_PER_MIN": "100000"})
    s, port = serve(0, bind=HOST)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://{HOST}:{port}"
    s.shutdown(); s.server_close()
    for k, v in old.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@pytest.fixture(scope="module")
def browser(base):
    pw = pytest.importorskip("playwright.sync_api")
    try:
        p = pw.sync_playwright().start(); b = p.chromium.launch()
    except Exception as e:
        pytest.skip(f"no browser available: {type(e).__name__}")
    yield b
    b.close(); p.stop()


@pytest.mark.parametrize("size", [(1366, 768), (390, 844)], ids=["laptop", "phone"])
def test_start_here_has_four_labelled_steps_and_no_sideways_scroll(browser, base, size):
    pg = browser.new_page(viewport={"width": size[0], "height": size[1]})
    try:
        pg.goto(base + "/", wait_until="networkidle"); pg.wait_for_selector("[data-t=judge-path]")
        box = pg.locator("[data-t=judge-path]")
        assert box.locator("button").count() == 4
        txt = box.inner_text()
        for must in ("SIMULATED", "LIVE-RECORDED", "0 captures", "Nothing was paid"):
            assert must in txt
        assert "LIVE-RECORDED" in txt and "captured" in txt
        assert pg.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    finally:
        pg.close()


def test_step_two_opens_the_recorded_claim_not_a_simulated_one(browser, base):
    pg = browser.new_page(viewport={"width": 1366, "height": 768})
    try:
        pg.goto(base + "/", wait_until="networkidle"); pg.wait_for_selector("[data-t=jp-2]")
        pg.click("[data-t=jp-2]"); pg.wait_for_selector("[data-t=claim-detail]")
        d = pg.locator("[data-t=claim-detail]").inner_text()
        assert "LIVE-4" in d and "LIVE-RECORDED" in d and "0 captures" in d and "CONTRADICTED" in d
    finally:
        pg.close()


def test_steps_one_and_three_navigate(browser, base):
    pg = browser.new_page(viewport={"width": 1366, "height": 768})
    try:
        pg.goto(base + "/", wait_until="networkidle"); pg.wait_for_selector("[data-t=jp-1]")
        pg.click("[data-t=jp-1]"); pg.wait_for_selector("h1:has-text('AI Agent Workflow')")
        pg.click("[data-t=nav-command]"); pg.wait_for_selector("[data-t=jp-3]")
        pg.click("[data-t=jp-3]"); pg.wait_for_selector("h1:has-text('Trust Engine')")
    finally:
        pg.close()


def test_step_four_opens_the_judge_bench(browser, base):
    pg = browser.new_page(viewport={"width": 1366, "height": 768})
    try:
        pg.goto(base + "/", wait_until="networkidle"); pg.wait_for_selector("[data-t=jp-4]")
        assert "SIMULATED" in pg.locator("[data-t=jp-4]").locator("xpath=..").inner_text()
        pg.click("[data-t=jp-4]"); pg.wait_for_selector("[data-t=bench-input]"); assert pg.locator("h1:has-text('Judge Bench')").count() == 1
    finally:
        pg.close()
