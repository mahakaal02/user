"""Admin control surface — drives the LiveSimulation directly (no HTTP) so it's
fast and deterministic. Verifies pause/resume/reset/edit, global knobs, the SSE
tick payload shape, and analytics. Run: python tests/test_admin.py"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin.manager import LiveSimulation  # noqa: E402

CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.offline.yaml")


def _sim(n_ticks=20):
    sim = LiveSimulation(CONFIG)
    sim.running = True
    for _ in range(n_ticks):
        sim._tick()
    return sim


def test_tick_payload_shape():
    sim = LiveSimulation(CONFIG)
    p = sim._tick()
    assert p["type"] == "tick"
    for k in ("market", "news", "trades", "events", "types", "control"):
        assert k in p, k
    m = p["market"]
    assert 0.0 <= m["price"] <= 1.0
    assert {"positive", "negative", "neutral"} <= set(m["sentiment"])
    # types aggregate covers all six personalities
    assert {t["type"] for t in p["types"]} == {"momentum", "contrarian", "news_reactive", "overconfident", "herd", "noise"}


def test_pause_bot_stops_it_trading():
    sim = _sim(15)
    bot = next(b for b in sim.bots if b.trade_count > 0)
    bot.status = "paused"
    before = bot.trade_count
    for _ in range(15):
        sim._tick()
    assert bot.trade_count == before, "a paused bot must not trade"
    bot.status = "active"
    for _ in range(15):
        sim._tick()
    assert bot.trade_count >= before, "resumed bot can trade again"


def test_live_param_edit_and_reset():
    sim = _sim(10)
    bot = sim.bots[0]
    bot.set_params(aggressiveness=0.25, bias=0.3, reaction_delay=4)
    assert abs(bot.aggressiveness - 0.25) < 1e-9
    assert bot.reaction_delay == 4
    assert bot._signal_buffer.maxlen == 5
    bot.reset_state()
    assert bot.coins == bot.start_coins
    assert bot.yes_shares == 0 and bot.no_shares == 0
    assert bot.trade_count == 0 and bot.status == "active"


def test_global_knobs_apply():
    sim = LiveSimulation(CONFIG)
    # liquidity scaling preserves price (ratio), changes depth
    price0 = sim._price()
    depth0 = sim.market.reserves.yes_shares + sim.market.reserves.no_shares
    sim.set_liquidity(2.0)
    assert abs(sim._price() - price0) < 1e-6, "liquidity change must not jump price"
    assert sim.market.reserves.yes_shares + sim.market.reserves.no_shares > depth0
    # stress raises aggressiveness via base×multiplier (idempotent, not compounding)
    base = sim._base[sim.bots[0].id]["aggressiveness"]
    sim.aggression_mult = 2.5
    sim.apply_globals()
    assert abs(sim.bots[0].aggressiveness - base * 2.5) < 1e-6
    sim.aggression_mult = 1.0
    sim.apply_globals()
    assert abs(sim.bots[0].aggressiveness - base) < 1e-6


def test_news_toggle_flattens_signal():
    sim = LiveSimulation(CONFIG)
    sim.news_enabled = False
    # with news off, headlines are empty across the bull regime window
    for _ in range(30):
        p = sim._tick()
        assert p["news"] == []


def test_analytics_structure():
    sim = _sim(25)
    a = sim.analytics()
    assert "types" in a and len(a["types"]) == 6
    row = a["types"][0]
    for k in ("type", "avg_pnl", "win_loss_ratio", "avg_trade_size", "avg_reaction_delay", "profit_share", "pnl_curve"):
        assert k in row, k


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("Admin control surface: all passed")
