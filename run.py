#!/usr/bin/env python3
"""
Example run script — the simulation entrypoint.

    python run.py --config config.offline.yaml
    python run.py --config config.yaml --ticks 200 --seed 7 --out runs/run1.jsonl
    python run.py --config config.offline.yaml --replay runs/run1.jsonl   # deterministic re-run

It only orchestrates the factories — every choice (which inference backend,
which market, how many of each bot) comes from the config file. There is no
backend-specific or URL-specific logic here or anywhere in the bot code.
"""
from __future__ import annotations

import argparse
import time

from sim.bots import build_population
from sim.config import apply_overrides, load_config
from sim.engine import Engine, Recorder
from sim.inference import build_inference_client
from sim.market import build_market
from sim.metrics import summarize
from sim.news import build_news_source
from sim.rng import RngHub


def main() -> None:
    ap = argparse.ArgumentParser(description="Multi-bot prediction-market simulation")
    ap.add_argument("--config", required=True, help="path to config.yaml / config.json")
    ap.add_argument("--ticks", type=int, default=None, help="override sim.ticks")
    ap.add_argument("--seed", type=int, default=None, help="override sim.seed")
    ap.add_argument("--out", default=None, help="JSONL output path (also the replay tape)")
    ap.add_argument(
        "--replay",
        default=None,
        help="replay inference from a prior run's JSONL (forces deterministic re-run)",
    )
    args = ap.parse_args()

    cfg = apply_overrides(load_config(args.config), ticks=args.ticks, seed=args.seed)
    sim_cfg = cfg["sim"]
    seed = int(sim_cfg.get("seed", 0))
    ticks = int(sim_cfg.get("ticks", 100))
    out_path = args.out or sim_cfg.get("out")

    # If replaying, swap the inference section to the replay backend (config-only
    # behaviour change — bots are oblivious).
    if args.replay:
        cfg["inference"] = {
            "sentiment_backend": "replay",
            "event_backend": "replay",
            "reasoning_backend": "replay",
            "replay": {"type": "replay", "recording": args.replay},
            "cache": {"enabled": True, "scope": "tick"},
        }

    rng_hub = RngHub(seed)
    inference = build_inference_client(cfg["inference"])
    market = build_market(cfg["market"])
    news = build_news_source(sim_cfg.get("news", {"source": "synthetic"}), rng_hub.stream("news"))
    bots = build_population(cfg["bots"], rng_hub)
    recorder = Recorder(out_path)

    print(
        f"Running {ticks} ticks · {len(bots)} bots · "
        f"inference={cfg['inference'].get('sentiment_backend')}/"
        f"{cfg['inference'].get('reasoning_backend')} · market={cfg['market'].get('mode')} · seed={seed}"
    )
    engine = Engine(
        inference=inference,
        market=market,
        bots=bots,
        news=news,
        rng_hub=rng_hub,
        recorder=recorder,
        ticks=ticks,
        resolve_outcome=sim_cfg.get("resolve_outcome"),
        progress_every=sim_cfg.get("progress_every", 0),
    )

    t0 = time.perf_counter()
    result = engine.run()
    elapsed = time.perf_counter() - t0
    recorder.close()

    start_coins = cfg["bots"].get("defaults", {}).get("coins", 1000.0)
    max_upstream_tick = max((r["upstream_calls_tick"] for r in recorder.ticks), default=0)
    print(summarize(result, start_coins=start_coins, max_upstream_tick=max_upstream_tick))
    print(
        f"\n  {ticks} ticks × {len(bots)} bots in {elapsed:.2f}s "
        f"({ticks * len(bots) / max(elapsed, 1e-9):,.0f} bot-ticks/s)"
        + (f" · log → {out_path}" if out_path else "")
    )


if __name__ == "__main__":
    main()
