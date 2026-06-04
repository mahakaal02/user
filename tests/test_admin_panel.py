"""Local-only fleet admin panel: the URL guard accepts loopback/private and
REJECTS public hosts (kalki.bet), slug parsing, market resolution + injection,
and the runner's merge/snapshot. No network. Run: python tests/test_admin_panel.py"""
from __future__ import annotations

import collections
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.admin_panel import FleetAdminPanel, is_local_url, slug_from_url  # noqa: E402
from live.runner import FleetRunner  # noqa: E402


class _FakeClient:
    def __init__(self, markets):
        self._m = markets

    def list_markets(self):
        return self._m


def _runner_with(markets):
    r = FleetRunner.__new__(FleetRunner)          # bypass __init__ (no network/config)
    r.client = _FakeClient(markets)
    r._injected = {}
    r._inject_lock = threading.Lock()
    r._seen_markets = set()
    r.titles = {}
    r.slugs = {}
    r.hist = {}
    r.stats = {"trades": 0, "rejects": 0}
    r.bots = []
    return r


def test_is_local_url_accepts_local():
    for u in ["http://localhost:3100/markets/en/markets/x", "http://127.0.0.1:3100/m/x",
              "http://192.168.1.5/x", "http://10.0.0.2/x", "http://172.16.4.9/x",
              "http://[::1]/x", "http://dev.local/x", "http://app.localhost/x"]:
        assert is_local_url(u) is True, u


def test_is_local_url_rejects_public():
    for u in ["https://kalki.bet/markets/en/markets/x", "http://kalki.bet/x",
              "http://8.8.8.8/x", "https://example.com", "ftp://localhost/x",
              "", "not a url", "http://"]:
        assert is_local_url(u) is False, u


def test_slug_from_url():
    assert slug_from_url("http://localhost:3100/markets/en/markets/will-india-win/") == "will-india-win"
    assert slug_from_url("http://localhost/markets/en/markets/abc") == "abc"


def test_add_market_rejects_public_url_no_injection():
    r = _runner_with([{"id": "m1", "slug": "will-x", "title": "Will X?"}])
    p = FleetAdminPanel(r, "http://localhost:3100/markets")
    res = p.add_market("https://kalki.bet/markets/en/markets/will-x")
    assert res["ok"] is False and "local" in res["error"].lower()
    assert r._injected == {}, "a rejected (public) URL must inject nothing"


def test_add_market_local_resolves_and_injects():
    r = _runner_with([{"id": "m1", "slug": "will-x", "title": "Will X?", "yesPrice": 0.5}])
    p = FleetAdminPanel(r, "http://localhost:3100/markets")
    res = p.add_market("http://localhost:3100/markets/en/markets/will-x")
    assert res["ok"] is True and res["added"] is True and res["id"] == "m1"
    assert "m1" in r._injected
    merged = r._merge_injected([])              # surfaces even if list_markets omits it later
    assert any(m["id"] == "m1" for m in merged)


def test_add_market_local_no_matching_market():
    r = _runner_with([{"id": "m1", "slug": "will-x"}])
    p = FleetAdminPanel(r, "http://localhost:3100/markets")
    res = p.add_market("http://localhost:3100/markets/en/markets/does-not-exist")
    assert res["ok"] is False and "no matching" in res["error"].lower()


def test_runner_injection_merge_and_snapshot():
    r = _runner_with([])
    assert r.inject_market({"id": "b", "title": "B", "slug": "b", "yesPrice": 0.3}) is True
    merged = r._merge_injected([{"id": "a", "yesPrice": 0.5, "title": "A"}])
    assert {m["id"] for m in merged} == {"a", "b"}
    r._seen_markets = {"b"}
    r.titles = {"b": "B"}
    r.hist = {"b": collections.deque([0.3])}
    snap = r.active_markets()
    assert snap and snap[0]["id"] == "b" and snap[0]["injected"] is True


def test_market_url_template():
    p = FleetAdminPanel(_runner_with([]), "http://localhost:3100/markets")
    assert p.market_url({"slug": "will-x"}) == "http://localhost:3100/markets/en/markets/will-x"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("Admin panel (local-only): all passed")
