"""Public-fleet facade: trending markets normalize (with reserves reconstructed
from price+liquidity), trade responses adapt to the runner's shape (balanceAfter
+ trade.shares), and the local account store round-trips. No network — urlopen is
stubbed. Run: python tests/test_public_fleet.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.public_fleet import (  # noqa: E402
    PublicBotClient,
    PublicExchangeFacade,
    _load_accounts,
    _new_account,
    _save_accounts,
)
import random  # noqa: E402


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._b = body

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_urlopen(body: dict):
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: _FakeResp(json.dumps(body).encode())
    return orig


def test_list_markets_normalizes_and_reconstructs_reserves():
    body = {"markets": [
        {"id": "m1", "slug": "s1", "title": "Will X happen?", "yesCents": 70,
         "noCents": 30, "liquidityCoins": 1000, "volumeCoins": 250, "endsAt": "2026-01-01T00:00:00Z"},
    ]}
    orig = _stub_urlopen(body)
    try:
        f = PublicExchangeFacade("https://kalki.bet/markets", bots=[])
        ms = f.list_markets()
    finally:
        urllib.request.urlopen = orig
    assert len(ms) == 1
    m = ms[0]
    assert m["id"] == "m1" and m["title"] == "Will X happen?"
    assert abs(m["yesPrice"] - 0.70) < 1e-9
    # priceYes = noShares/(yes+no) → noShares = 0.7*1000, yesShares = 0.3*1000
    assert abs(m["noShares"] - 700.0) < 1e-6
    assert abs(m["yesShares"] - 300.0) < 1e-6
    # reconstructed reserves reproduce the price
    assert abs(m["noShares"] / (m["yesShares"] + m["noShares"]) - m["yesPrice"]) < 1e-9


def test_trade_buy_adapts_response_shape_and_decrements_balance():
    bot = PublicBotClient("https://kalki.bet/markets", "a@sim.kalki.local", "a", "pw12345678")
    bot.balance = 10_000.0
    bot.trade = lambda *a, **k: (200, {"ok": True, "trade": {"shares": 66.6, "cost": 50, "avgPrice": 0.7}})
    f = PublicExchangeFacade("https://kalki.bet/markets", bots=[bot])
    status, body = f.trade("a@sim.kalki.local", "m1", "BUY", "YES", coins=50)
    assert status == 200
    assert body["balanceAfter"] == 9950.0           # 10000 - cost(50)
    assert body["trade"]["shares"] == 66.6          # runner reads resp["trade"]["shares"]


def test_trade_sell_credits_balance():
    bot = PublicBotClient("https://kalki.bet/markets", "b@sim.kalki.local", "b", "pw12345678")
    bot.balance = 9000.0
    bot.trade = lambda *a, **k: (200, {"ok": True, "trade": {"shares": 40, "coinsReceived": 120, "avgPrice": 0.3}})
    f = PublicExchangeFacade("https://kalki.bet/markets", bots=[bot])
    status, body = f.trade("b@sim.kalki.local", "m1", "SELL", "NO", shares=40)
    assert status == 200
    assert body["balanceAfter"] == 9120.0           # 9000 + coinsReceived(120)


def test_trade_failure_passes_status_through():
    bot = PublicBotClient("https://kalki.bet/markets", "c@sim.kalki.local", "c", "pw12345678")
    bot.balance = 100.0
    bot.trade = lambda *a, **k: (400, {"error": "insufficient_coins"})
    f = PublicExchangeFacade("https://kalki.bet/markets", bots=[bot])
    status, body = f.trade("c@sim.kalki.local", "m1", "BUY", "YES", coins=999)
    assert status == 400 and body["error"] == "insufficient_coins"
    assert bot.balance == 100.0                      # unchanged on reject


def test_list_bot_users_shape():
    bot = PublicBotClient("https://kalki.bet/markets", "d@sim.kalki.local", "dee", "pw12345678")
    bot.balance = 7777.0
    f = PublicExchangeFacade("https://kalki.bet/markets", bots=[bot])
    users = f.list_bot_users()
    assert users == [{"id": "d@sim.kalki.local", "username": "dee", "balance": 7777.0}]


def test_account_store_roundtrip_and_format():
    acct = _new_account(random.Random(1))
    assert acct["email"].endswith("@sim.kalki.local")
    import re
    assert re.match(r"^[a-zA-Z0-9_]{3,20}$", acct["username"])   # passes the server regex
    assert len(acct["password"]) >= 8
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sub", "kalki_accounts.json")
        _save_accounts(p, [acct])
        assert _load_accounts(p) == [acct]
    assert _load_accounts("/nonexistent/path.json") == []        # tolerant of missing file


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("Public fleet: all passed")
