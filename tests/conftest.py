import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls the real PayPal SANDBOX (read-only); skipped unless ROADWORTHY_LIVE=1")

def pytest_collection_modifyitems(config, items):
    import pytest
    if os.environ.get("ROADWORTHY_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live PayPal sandbox test; set ROADWORTHY_LIVE=1 to run (read-only)")
    for it in items:
        if "live" in it.keywords:
            it.add_marker(skip)
