"""Dry-run wrapper (Step 5): READS pass through; trade/comment WRITES are
suppressed (no inner call), logged as DRY RUN, and return synthetic success so
the runner's metrics still update. No network, no exchange state change.
Run: python tests/test_dry_run.py"""
from __future__ import annotations

import contextlib
import io
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.dry_run import DryRunClient  # noqa: E402
from live.runner import FleetRunner, LiveBot  # noqa: E402


class _RecInner:
    """Records every call so we can assert WRITES never reach the real client."""

    def __init__(self):
        self.calls = []
        self._markets = [{"id": "m1", "yesPrice": 0.7, "slug": "x", "title": "X"}]
        self._users = [{"id": "u1", "username": "a", "balance": 1000}]

    def list_markets(self):
        self.calls.append("list_markets")
        return self._markets

    def list_bot_users(self):
        self.calls.append("list_bot_users")
        return self._users

    def trade(self, *a, **k):
        self.calls.append(("trade", a, k))           # MUST never happen in dry-run
        return 200, {"trade": {"shares": 1}, "balanceAfter": 1}

    def comment(self, *a, **k):
        self.calls.append(("comment", a, k))         # MUST never happen in dry-run
        return 200, {"ok": True}

    def _req(self, *a, **k):
        self.calls.append("_req")
        return 200, {}


def _wrote(inner, kind):
    return any(isinstance(x, tuple) and x[0] == kind for x in inner.calls)


def test_reads_pass_through():
    inner = _RecInner()
    c = DryRunClient(inner)
    assert c.list_markets() == inner._markets
    assert c.list_bot_users() == inner._users
    assert "list_markets" in inner.calls and "list_bot_users" in inner.calls


def test_buy_suppressed_logged_and_synthetic():
    inner = _RecInner()
    c = DryRunClient(inner)
    c.list_markets(); c.list_bot_users()             # seed price + balance
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        st, resp = c.trade("u1", "m1", "BUY", "YES", coins=70)
    assert st == 200
    assert isinstance(resp["trade"]["shares"], (int, float)) and isinstance(resp["balanceAfter"], (int, float))
    assert "DRY RUN BUY" in buf.getvalue()
    assert not _wrote(inner, "trade"), "no real trade POST may occur in dry-run"
    assert abs(resp["trade"]["shares"] - 100) < 1.0          # 70 / 0.7
    assert resp["balanceAfter"] == 930                       # 1000 - 70


def test_sell_suppressed_and_logged():
    inner = _RecInner()
    c = DryRunClient(inner)
    c.list_markets(); c.list_bot_users()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        st, resp = c.trade("u1", "m1", "SELL", "YES", shares=10)
    assert st == 200 and "DRY RUN SELL" in buf.getvalue()
    assert resp["trade"]["coinsReceived"] > 0 and "balanceAfter" in resp
    assert not _wrote(inner, "trade")


def test_comment_suppressed_and_logged():
    inner = _RecInner()
    c = DryRunClient(inner)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        st, resp = c.comment("u1", "m1", "buying yes here")
    assert st == 200 and "DRY RUN COMMENT" in buf.getvalue()
    assert not _wrote(inner, "comment")


def test_getattr_delegates_to_inner():
    inner = _RecInner()
    c = DryRunClient(inner)
    st, _ = c._req("GET", "/api/internal/bot-users")   # startup-validation probe path
    assert st == 200 and "_req" in inner.calls


def test_runner_metrics_update_but_no_write():
    """Full _execute path through the dry client: stats.trades increments, all
    safety gates run, and the real client receives NO trade call."""
    inner = _RecInner()
    c = DryRunClient(inner)
    c.list_markets(); c.list_bot_users()
    r = FleetRunner.__new__(FleetRunner)               # bypass __init__ (no net)
    r.client = c
    r.stats = {"trades": 0, "comments": 0, "rejects": 0, "cycles": 0, "news_markets": 0}
    r.tps_limit = 100; r.max_exposure = 1.0
    r.breaker_threshold = 5; r.breaker_pause_s = 0.0
    r._consec_fail = 0; r._breaker_armed = False
    bot = LiveBot("u1", "a", "momentum", random.Random(1), 1000,
                  {"aggressiveness": 0.05, "max_trade_coins": 120, "min_trade_coins": 10, "reaction_delay": 1})

    class _Pick:
        def random(self):
            return 1.0                                 # never trigger the comment branch

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r._execute(bot, {"id": "m1", "title": "X"}, ("BUY", "YES", 50), 0.0, _Pick())
    assert r.stats["trades"] == 1, "metrics must still update in dry-run"
    assert r.stats["rejects"] == 0
    assert "DRY RUN BUY" in buf.getvalue()
    assert not _wrote(inner, "trade"), "the real exchange client must receive no trade"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("Dry-run: all passed")
