import json, pytest
from datetime import datetime, timezone
from roadworthy.client import SandboxClient
from roadworthy.txsearch import search_transactions


class T:
    def __init__(self, bodies): self.bodies, self.urls = bodies, []
    def request(self, m, url, h, t):
        self.urls.append(url); return 200, json.dumps(self.bodies.pop(0)).encode()


def test_offline_logic_counts_and_aggregates_only():
    """OFFLINE test of parsing logic with a made-up response. This is NOT a PayPal result."""
    body = {"transaction_details": [{"transaction_info": {"transaction_status": "S", "transaction_amount": {"currency_code": "USD", "value": "5.00"}}}], "total_pages": 1}
    c = SandboxClient(lambda: "t", transport=T([body]))
    s = search_transactions(c, end=datetime(2026, 10, 26, tzinfo=timezone.utc), days=30)
    assert s.status_code == 200 and s.count == 1 and s.statuses == {"S": 1} and s.currencies == ["USD"]
    assert not hasattr(s, "transactions")        # no ids / amounts retained in the summary


def test_window_limited_to_31_days():
    c = SandboxClient(lambda: "t", transport=T([]))
    with pytest.raises(ValueError):
        search_transactions(c, days=32)


@pytest.mark.live
def test_LIVE_transaction_search_against_real_sandbox():
    """Reproduces the verified Windows test: OAuth + Transaction Search, read-only, real PayPal Sandbox."""
    from roadworthy.auth import TokenProvider, load_credentials, fetch_token
    tp = TokenProvider(load_credentials(), fetch_token)
    s = search_transactions(SandboxClient(tp.get), days=30)
    assert s.status_code == 200, s.error
    assert s.count >= 0
    print(f"LIVE Transaction Search: http={s.status_code} count={s.count} pages={s.pages_read}")
