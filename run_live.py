#!/usr/bin/env python3
"""
Launch the live Kalki Exchange bot fleet.

    python run_live.py --config config.live.yaml            # run continuously
    python run_live.py --cycles 20                          # run 20 cycles then stop
    KALKI_URL=https://kalki.bet/markets INTERNAL_API_SECRET=... python run_live.py

Prereqs:
  1. The bet app is running with the internal routes + INTERNAL_API_SECRET set.
  2. Bot users are seeded:  (in bet/bet)  BOTS_COUNT=1000 npx tsx scripts/seed-bots.ts
"""
from __future__ import annotations

import argparse

import yaml

from sim.config import _subst_env
from sim.inference import build_inference_client
from sim.rng import RngHub

from live.comment_gen import CommentGenerator
from live.kalki_client import KalkiClient
from live.runner import FleetRunner


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return _subst_env(yaml.safe_load(fh))


def main() -> None:
    ap = argparse.ArgumentParser(description="Live Kalki Exchange bot fleet")
    ap.add_argument("--config", default="config.live.yaml")
    ap.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: run forever)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ex = cfg["exchange"]
    client = KalkiClient(ex["base_url"], ex["internal_secret"], ex.get("timeout_s", 15))
    comment_gen = CommentGenerator(
        backend=cfg["comments"].get("backend", "template"),
        qwen_url=cfg["comments"].get("qwen_url") or None,
    )
    inference = build_inference_client(cfg["inference"])
    rng_hub = RngHub(int(cfg.get("seed", 7)))

    print(f"connecting to {ex['base_url']} …")
    users = client.list_bot_users()
    print(f"  {len(users)} bot users on the exchange")
    markets = client.list_markets()
    print(f"  {len(markets)} open markets")
    if not users:
        raise SystemExit("No bot users — seed them first: (in bet/bet) npx tsx scripts/seed-bots.ts")

    runner = FleetRunner(client, comment_gen, inference, cfg, rng_hub)
    runner.run(cycles=1 if args.once else args.cycles)


if __name__ == "__main__":
    main()
