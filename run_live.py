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
from sim.news_feed import build_market_news_feed
from sim.rng import RngHub

from live.comment_gen import CommentGenerator
from live.kalki_client import KalkiClient
from live.runner import FleetRunner


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return _subst_env(yaml.safe_load(fh))


def apply_admin_endpoints(cfg: dict) -> dict:
    """If the admin panel saved model endpoints (runs/model_endpoints.json), use
    them (overriding config/env): the Qwen URL drives reasoning + LLM comments +
    relevance refine; the embedding URL drives the news relevance cheap-filter.
    So you can point the bots at a Qwen / embedding model from the admin UI."""
    import json
    import os

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", "model_endpoints.json")
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as fh:
            ep = json.load(fh)
    except (OSError, ValueError):
        return cfg
    qwen = (ep.get("qwen_url") or "").strip()
    emb = (ep.get("embedding_url") or "").strip()
    if qwen:
        inf = cfg.setdefault("inference", {})
        inf["reasoning_backend"] = "qwen_api"
        inf.setdefault("qwen_api", {}).update({"type": "qwen_api", "url": qwen})
        inf.setdefault("cache", {"enabled": True, "scope": "tick"})
        cfg.setdefault("comments", {})["backend"] = "llm"
        cfg["comments"]["qwen_url"] = qwen
        cfg.setdefault("news_feed", {}).setdefault("relevance", {}).setdefault("refine", {})["qwen_url"] = qwen
    if emb:
        rel = cfg.setdefault("news_feed", {}).setdefault("relevance", {})
        rel["backend"] = "embeddings"
        rel.setdefault("embeddings", {})["url"] = emb
    if qwen or emb:
        print(f"  admin endpoints (runs/model_endpoints.json): qwen={'set' if qwen else '—'}, "
              f"embedding={'set' if emb else '—'}")
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description="Live Kalki Exchange bot fleet")
    ap.add_argument("--config", default="config.live.yaml")
    ap.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: run forever)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = ap.parse_args()

    cfg = apply_admin_endpoints(load_config(args.config))
    ex = cfg["exchange"]
    client = KalkiClient(ex["base_url"], ex["internal_secret"], ex.get("timeout_s", 15))
    comment_gen = CommentGenerator(
        backend=cfg["comments"].get("backend", "template"),
        qwen_url=cfg["comments"].get("qwen_url") or None,
    )
    inference = build_inference_client(cfg["inference"])
    news_feed = build_market_news_feed(cfg.get("news_feed"))
    rng_hub = RngHub(int(cfg.get("seed", 7)))

    print(f"connecting to {ex['base_url']} …")
    users = client.list_bot_users()
    print(f"  {len(users)} bot users on the exchange")
    markets = client.list_markets()
    print(f"  {len(markets)} open markets")
    if news_feed is not None:
        nf = cfg["news_feed"]
        print(f"  news: Google News ({nf.get('google_news', {}).get('gl', 'US')}) "
              f"+ {len(news_feed.fallback_feeds)} fallback feeds, relevance={nf.get('relevance', {}).get('backend', 'keyword')}")
    else:
        print("  news: disabled (market titles only)")
    if not users:
        raise SystemExit("No bot users — seed them first: (in bet/bet) npx tsx scripts/seed-bots.ts")

    runner = FleetRunner(client, comment_gen, inference, cfg, rng_hub, news_feed=news_feed)
    runner.run(cycles=1 if args.once else args.cycles)


if __name__ == "__main__":
    main()
