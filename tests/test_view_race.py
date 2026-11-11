"""Regression: a slow Command Center render must never overwrite the view the user has since selected. OFFLINE; skipped without Chromium."""
import os
import threading
import time

import pytest

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


@pytest.mark.parametrize("nav,marker", [("nav-attack", "Run all 16 attacks"), ("nav-trust", "Earn or lose a permission")])
def test_slow_command_center_cannot_overwrite_a_newer_view(browser, base, nav, marker):
    pg = browser.new_page(viewport={"width": 1366, "height": 800})
    try:
        def slow(route):
            time.sleep(1.5)                      # the Command Center's data arrives AFTER the user has already clicked another view
            route.continue_()
        pg.route("**/api/overview", slow)
        pg.goto(base, wait_until="commit")
        pg.wait_for_selector(f"[data-t={nav}]")
        pg.click(f"[data-t={nav}]")
        pg.get_by_text(marker, exact=False).first.wait_for()
        pg.wait_for_timeout(3000)                # long enough for the slow Command Center render to finish
        assert pg.get_by_text(marker, exact=False).count() >= 1, "selected view was overwritten"
        assert pg.query_selector("[data-t=judge-path]") is None, "Command Center rendered over the selected view"
    finally:
        pg.close()
