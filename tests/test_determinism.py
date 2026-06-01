"""Deterministic replay: same seed → identical price path; different seed →
different. This is the 'reproducible' requirement. Run: python tests/test_determinism.py"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.bots import build_population  # noqa: E402
from sim.engine import Engine, Recorder  # noqa: E402
from sim.inference import build_inference_client  # noqa: E402
from sim.market import build_market  # noqa: E402
from sim.news import build_news_source  # noqa: E402
from sim.rng import RngHub  # noqa: E402


def _run(seed: int, ticks: int = 60) -> list[float]:
    cfg_inf = {
        "sentiment_backend": "local_heuristic",
        "event_backend": "local_heuristic",
        "reasoning_backend": "local_heuristic",
        "cache": {"enabled": True, "scope": "tick"},
    }
    cfg_bots = {
        "defaults": {"coins": 1000.0},
        "jitter": {"bias": 0.15, "aggressiveness": 0.3, "reaction_spread": 2},
        "population": [
            {"type": "momentum", "count": 20, "params": {"reaction_delay": 1}},
            {"type": "contrarian", "count": 15},
            {"type": "news_reactive", "count": 15},
            {"type": "herd", "count": 20, "params": {"reaction_delay": 1}},
            {"type": "noise", "count": 10},
        ],
    }
    hub = RngHub(seed)
    eng = Engine(
        inference=build_inference_client(cfg_inf),
        market=build_market({"mode": "sim", "sim": {}}),
        bots=build_population(cfg_bots, hub),
        news=build_news_source({"source": "synthetic"}, hub.stream("news")),
        rng_hub=hub,
        recorder=Recorder(None),
        ticks=ticks,
    )
    return eng.run().prices


def test_same_seed_is_bit_identical():
    assert _run(7) == _run(7)


def test_different_seed_diverges():
    assert _run(7) != _run(8)


def test_no_per_bot_inference_calls_are_bounded():
    # Upstream calls per tick must be independent of bot count: rerun with 5x
    # the bots and confirm the total upstream calls is unchanged.
    def upstream(n_bots: int) -> int:
        cfg_inf = {
            "sentiment_backend": "local_heuristic",
            "event_backend": "local_heuristic",
            "reasoning_backend": "local_heuristic",
            "cache": {"enabled": True, "scope": "tick"},
        }
        hub = RngHub(3)
        inf = build_inference_client(cfg_inf)
        eng = Engine(
            inference=inf,
            market=build_market({"mode": "sim", "sim": {}}),
            bots=build_population(
                {"defaults": {"coins": 1000.0}, "population": [{"type": "momentum", "count": n_bots}]}, hub
            ),
            news=build_news_source({"source": "synthetic"}, hub.stream("news")),
            rng_hub=hub,
            recorder=Recorder(None),
            ticks=40,
        )
        eng.run()
        return inf.upstream_calls

    assert upstream(20) == upstream(100), "inference cost must not scale with bots"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("Determinism: all passed")
