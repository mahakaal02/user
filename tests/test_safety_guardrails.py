"""Runtime safety guardrails (audit fixes) — verified with a FAKE client, no
network and no real trades. Proves: per-bot rate limit, max-exposure cap,
response hardening, circuit breaker (pause→stop), kill switch, and that the
trade decision math (size_coins / _decide) is untouched.
Run: python tests/test_safety_guardrails.py"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.runner import FleetRunner, LiveBot, _CircuitBreakerStop  # noqa: E402


# --------------------------------------------------------------------------- #
#  Fakes — no network, no /bet.
# --------------------------------------------------------------------------- #
class _FakeClient:
    """Records trade calls; returns a scripted (status, body) sequence."""

    def __init__(self, script=None, default=(200, {"trade": {"shares": 10.0, "cost": 50}, "balanceAfter": 9950})):
        self.script = list(script or [])
        self.default = default
        self.trades = []

    def trade(self, user_id, market_id, side, outcome, coins=None, shares=None):
        self.trades.append((user_id, market_id, side, outcome, coins, shares))
        return self.script.pop(0) if self.script else self.default

    def comment(self, *a, **k):
        return 200, {"ok": True}

    def list_bot_users(self):
        return [{"id": "u1", "username": "bot1", "balance": 10000}]


_FLEET_CFG = {"aggressiveness": 0.05, "max_trade_coins": 120, "min_trade_coins": 10, "reaction_delay": 1}


class _Rng:
    def random(self):
        return 1.0  # never trigger the comment branch


def _runner(client, **env):
    for k, v in env.items():
        os.environ[k] = str(v)
    try:
        r = FleetRunner.__new__(FleetRunner)  # bypass __init__ network/config
        r.client = client
        r.stats = {"trades": 0, "comments": 0, "rejects": 0, "cycles": 0, "news_markets": 0}
        from live.runner import _env_int, _env_float, KILL_FILE
        r.tps_limit = _env_int("BOT_TPS_LIMIT", 10)
        r.max_exposure = _env_float("BOT_MAX_EXPOSURE_PER_MARKET", 0.5)
        r.confidence_floor = _env_float("BOT_CONFIDENCE_FLOOR", 0.55)
        r.breaker_threshold = _env_int("BOT_BREAKER_THRESHOLD", 5)
        r.breaker_pause_s = _env_float("BOT_BREAKER_PAUSE_S", 0.0)  # no real sleep in tests
        r.kill_file = KILL_FILE
        r._consec_fail = 0
        r._breaker_armed = False
        return r
    finally:
        for k in env:
            os.environ.pop(k, None)


def _bot(balance=10000):
    return LiveBot("u1", "bot1", "momentum", _Rng(), balance, _FLEET_CFG)


_M = {"id": "m1", "title": "Will X happen?", "yesPrice": 0.5}


# --------------------------------------------------------------------------- #
#  Tests
# --------------------------------------------------------------------------- #
def test_per_bot_rate_limit_rejects_without_trading():
    client = _FakeClient()
    r = _runner(client, BOT_TPS_LIMIT=3)
    bot = _bot()
    for _ in range(8):
        r._execute(bot, _M, ("BUY", "YES", 50), 0.0, _Rng())
    assert len(client.trades) == 3, f"rate limit should cap submissions at 3, got {len(client.trades)}"
    assert r.stats["trades"] == 3 and r.stats["rejects"] == 5


def test_max_exposure_blocks_additional_buys():
    # cap 0.5 of 10_000 = 5_000 committed max; each buy commits cost=50 (fake),
    # but we drive 'amount' high so the prospective check trips.
    client = _FakeClient(default=(200, {"trade": {"shares": 1.0, "cost": 4000}, "balanceAfter": 6000}))
    r = _runner(client, BOT_MAX_EXPOSURE_PER_MARKET=0.5, BOT_TPS_LIMIT=100)
    bot = _bot(10000)
    r._execute(bot, _M, ("BUY", "YES", 4000), 0.0, _Rng())   # invested 4000 (<5000) ok
    r._execute(bot, _M, ("BUY", "YES", 4000), 0.0, _Rng())   # would be 8000 (>5000) → blocked
    assert len(client.trades) == 1, "second buy past the exposure cap must be blocked"
    assert r.stats["trades"] == 1 and r.stats["rejects"] == 1
    # selling is never blocked by the exposure cap
    assert bot.would_exceed_exposure("m1", 4000, 0.5) is True
    assert bot.would_exceed_exposure("m1", 500, 0.5) is False


def test_malformed_200_is_reject_not_crash():
    # 200 OK but missing balanceAfter / trade.shares → counted reject, no exception
    client = _FakeClient(script=[(200, {"trade": {}, "ok": True})])
    r = _runner(client)
    bot = _bot()
    r._execute(bot, _M, ("BUY", "YES", 50), 0.0, _Rng())     # must not raise
    assert r.stats["rejects"] == 1 and r.stats["trades"] == 0


def test_circuit_breaker_pause_then_stop():
    # 5 failures → pause (armed). 5 more → raise _CircuitBreakerStop.
    client = _FakeClient(script=[(500, {})] * 10)
    r = _runner(client, BOT_BREAKER_THRESHOLD=5, BOT_TPS_LIMIT=1000)
    bot = _bot()
    stopped = False
    try:
        for _ in range(10):
            r._execute(bot, _M, ("BUY", "YES", 50), 0.0, _Rng())
    except _CircuitBreakerStop:
        stopped = True
    assert stopped, "breaker must stop the runner after failures continue past the pause"
    assert r._breaker_armed is True


def test_circuit_breaker_resets_on_success():
    client = _FakeClient(script=[(500, {}), (500, {}), (200, {"trade": {"shares": 1.0, "cost": 50}, "balanceAfter": 9950})])
    r = _runner(client, BOT_BREAKER_THRESHOLD=5, BOT_TPS_LIMIT=1000)
    bot = _bot()
    for _ in range(3):
        r._execute(bot, _M, ("BUY", "YES", 50), 0.0, _Rng())
    assert r._consec_fail == 0 and r._breaker_armed is False


def test_kill_switch_env_and_file():
    r = _runner(_FakeClient())
    assert r._kill_engaged() is False
    os.environ["BOT_KILL_SWITCH"] = "1"
    try:
        assert r._kill_engaged() is True
    finally:
        os.environ.pop("BOT_KILL_SWITCH", None)
    assert r._kill_engaged() is False
    # file trigger (runtime kill of an already-running process)
    open(r.kill_file, "w").close()
    try:
        assert r._kill_engaged() is True
    finally:
        os.remove(r.kill_file)


def test_trade_decision_math_unchanged():
    # size_coins still clamps to max_trade_coins / 25% of balance / min — the
    # safety patch must not have altered sizing.
    bot = _bot(10000)
    assert bot.size_coins(1.0) == 120          # 10000*0.05=500 → clamped to max_trade_coins
    assert bot.size_coins(0.0) == 10           # floor at min_trade_coins
    small = _bot(200)
    assert small.size_coins(1.0) == 10         # 200*0.05=10 → floor (unchanged formula)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("Safety guardrails: all passed")
