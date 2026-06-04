"""Crowd-simulation layer: the new-market bucket allocation hits ~60% of selection
mass, the recency decay behaves, the weighted sampler is proportional, signal-source
attribution is correct, and SimLogger stamps a mandatory SIMULATION_MODE label.
No network. Run: python tests/test_crowd_sim.py"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.crowd import AttentionModel, signal_source  # noqa: E402
from live.sim_logger import SimLogger  # noqa: E402

NOW = 1_000_000.0


def _mk(mid, age_s):
    return {"id": mid, "slug": mid, "createdAt": NOW - age_s, "yesPrice": 0.5}


_SIM_CFG = {
    "attention": {"base_attention": 1.0, "qwen_weight": 0.0, "momentum_weight": 0.0,
                  "herd_multiplier": 0.0, "noise_floor": 0.0, "news_delay_cycles": 1},
    "new_market_boost": {"target_new_share": 0.60, "decay_function": "exponential",
                         "half_life_s": 1800, "cutoff_age_s": 7200},
}


def test_new_market_bucket_hits_60pct_share():
    rng = random.Random(42)
    am = AttentionModel(_SIM_CFG, qwen_active=False, now_fn=lambda: NOW)
    markets = [_mk("new1", 60), _mk("new2", 600),
               _mk("old1", 100_000), _mk("old2", 200_000), _mk("old3", 300_000)]
    am.begin_cycle(0, markets, signals={}, hist={}, rng=rng)
    picks = [am.pick_market(rng)["id"] for _ in range(8000)]
    new_share = sum(p in ("new1", "new2") for p in picks) / len(picks)
    assert 0.56 <= new_share <= 0.64, f"new-market share {new_share} not ~0.60"


def test_no_new_markets_means_no_artificial_boost():
    rng = random.Random(1)
    am = AttentionModel(_SIM_CFG, qwen_active=False, now_fn=lambda: NOW)
    markets = [_mk("old1", 100_000), _mk("old2", 200_000), _mk("old3", 300_000)]
    am.begin_cycle(0, markets, signals={}, hist={}, rng=rng)
    # equal intrinsic, no new bucket → ~uniform 1/3 each
    for w in am.weights.values():
        assert abs(w - 1 / 3) < 0.02


def test_recency_decay_curve():
    am = AttentionModel(_SIM_CFG, qwen_active=False, now_fn=lambda: NOW)
    assert abs(am._recency(0) - 1.0) < 1e-9
    assert abs(am._recency(1800) - 0.5) < 1e-6            # half-life
    assert abs(am._recency(3600) - 0.25) < 1e-6
    pl = AttentionModel({"attention": {}, "new_market_boost": {"decay_function": "power_law", "half_life_s": 1800}},
                        now_fn=lambda: NOW)
    assert abs(pl._recency(1800) - 0.5) < 1e-9            # power-law: 1/(1+1)


def test_qwen_news_score_only_when_active():
    sig = types.SimpleNamespace(confidence=0.8, directional=0.5, news_intensity=0.0)
    on = AttentionModel(_SIM_CFG, qwen_active=True, now_fn=lambda: NOW)
    off = AttentionModel(_SIM_CFG, qwen_active=False, now_fn=lambda: NOW)
    assert on._qwen_news_score(sig) > 0.0
    assert off._qwen_news_score(sig) == 0.0


def test_signal_source_attribution():
    assert signal_source("news_reactive", qwen_active=True) == "qwen"
    assert signal_source("overconfident", qwen_active=True) == "qwen"
    assert signal_source("news_reactive", qwen_active=False) == "heuristic"   # control arm
    assert signal_source("momentum", qwen_active=True) == "heuristic"
    assert signal_source("contrarian", qwen_active=True) == "heuristic"
    assert signal_source("noise", qwen_active=True) == "noise"


def test_sim_logger_labels_and_schema():
    bot = types.SimpleNamespace(user_id="bot1", kind="news_reactive", ema={"m1": 0.42})
    market = _mk("m1", 60)
    sig = types.SimpleNamespace(confidence=0.7, directional=0.4)
    with tempfile.TemporaryDirectory() as d:
        # blank label must NOT stay blank
        lg = SimLogger(d, "SIMULATION_MODE", label="", qwen_active=True, now_fn=lambda: NOW)
        assert lg.label == "SIMULATION_MODE"
        am = AttentionModel(_SIM_CFG, qwen_active=True, now_fn=lambda: NOW)
        am.begin_cycle(0, [market, _mk("old", 99999)], {"m1": sig, "old": sig}, {}, random.Random(0))
        lg.log_cycle(0, am, {"m1": sig, "old": sig})
        lg.log_decision(0, bot, market, ("BUY", "YES", 50), sig)
        lg.close()
        trades = [json.loads(x) for x in open(os.path.join(d, "trades.jsonl"))]
        cycles = [json.loads(x) for x in open(os.path.join(d, "cycles.jsonl"))]
        markets_log = [json.loads(x) for x in open(os.path.join(d, "markets.jsonl"))]
    t = trades[0]
    assert t["sim_mode"] == "SIMULATION_MODE" and t["run_mode"] == "SIMULATION_MODE"
    assert t["bot_type"] == "news_reactive" and t["signal_source"] == "qwen"
    assert t["side"] == "BUY" and t["outcome"] == "YES" and t["size"] == 50
    c = cycles[0]
    assert c["sim_mode"] == "SIMULATION_MODE" and "new_market_pick_share" in c and "qwen_influence_strength" in c
    assert all(r["sim_mode"] == "SIMULATION_MODE" and "attention_weight" in r for r in markets_log)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("Crowd simulation: all passed")
