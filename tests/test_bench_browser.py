"""Judge Bench in a real browser (OFFLINE; skipped without Chromium)."""
import os
import re
import threading

import pytest
expect = pytest.importorskip("playwright.sync_api").expect

from roadworthy.dashboard.server import serve


@pytest.fixture(scope="module")
def base():
    keys = ("SENTINEL_PUBLIC", "SENTINEL_PUBLIC_HOST", "SENTINEL_RATE_POST_PER_MIN", "SENTINEL_RATE_GLOBAL_POST_PER_MIN", "SENTINEL_RATE_GET_PER_MIN")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({"SENTINEL_PUBLIC": "1", "SENTINEL_PUBLIC_HOST": "demo.example.com", "SENTINEL_RATE_POST_PER_MIN": "100000",
                       "SENTINEL_RATE_GLOBAL_POST_PER_MIN": "100000", "SENTINEL_RATE_GET_PER_MIN": "100000"})
    s, port = serve(0, bind="127.0.0.1")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
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


def test_bench_flow_decision_trace_receipt_tamper_forge(browser, base):
    pg = browser.new_page(viewport={"width": 1366, "height": 900}); errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    try:
        pg.goto(base); pg.wait_for_selector("[data-t=judge-path]"); pg.click("[data-t=nav-bench]"); pg.wait_for_selector("[data-t=bench-input]")
        pg.click("[data-t=bench-preset-p1]"); pg.wait_for_selector("[data-t=bench-action]")
        assert pg.inner_text("[data-t=bench-action]") == "DENY"
        tr = pg.inner_text("[data-t=bench-trace]"); assert "✕ hard-limits" in tr and "✓ environment" in tr and "not reached" in tr
        pg.click("[data-t=bench-verify]"); pg.wait_for_selector("[data-t=bench-verdict]")
        assert pg.inner_text("[data-t=bench-verdict]") == "RECEIPT VERIFIED"
        pg.click("[data-t=bench-tamper]"); expect(pg.locator('[data-t=bench-receipt-text]')).to_have_value(re.compile(r'"action": "ALLOW"')); pg.click("[data-t=bench-verify]"); pg.wait_for_selector("[data-t=bench-verdict]")
        v = pg.inner_text("[data-t=bench-verify-result]"); assert "RECEIPT REJECTED" in v and "✕ hash" in v
        pg.click("[data-t=bench-preset-p1]"); pg.wait_for_selector("[data-t=bench-receipt]")
        pg.click("[data-t=bench-forge]"); expect(pg.locator('[data-t=bench-receipt-text]')).to_have_value(re.compile(r'"action": "ALLOW"')); pg.click("[data-t=bench-verify]"); pg.wait_for_selector("[data-t=bench-verdict]")
        v = pg.inner_text("[data-t=bench-verify-result]"); assert "RECEIPT REJECTED" in v and "✓ hash" in v and "✕ re-execution" in v
        pg.click("[data-t=bench-preset-p3]"); pg.wait_for_selector("[data-t=bench-action]"); assert pg.inner_text("[data-t=bench-action]") == "DENY"
        pg.fill("[data-t=bench-args]", "{not json"); pg.click("[data-t=bench-run]"); pg.wait_for_selector("[data-t=bench-error]")
        assert not errs
    finally:
        pg.close()


def test_bench_has_no_sideways_scroll_on_a_phone(browser, base):
    pg = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        pg.goto(base); pg.wait_for_selector("[data-t=judge-path]"); pg.click("[data-t=nav-bench]"); pg.click("[data-t=bench-preset-p5]"); pg.wait_for_selector("[data-t=bench-receipt]")
        assert pg.evaluate("document.documentElement.scrollWidth") <= 392
    finally:
        pg.close()
