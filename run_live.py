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
import os

import yaml

from sim.config import _subst_env
from sim.inference import build_inference_client
from sim.news_feed import build_market_news_feed
from sim.rng import RngHub

from live.comment_gen import CommentGenerator
from live.kalki_client import KalkiClient
from live.public_fleet import PublicExchangeFacade, provision_fleet
from live.runner import FleetRunner


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return _subst_env(yaml.safe_load(fh))


def apply_admin_endpoints(cfg: dict) -> dict:
    """If the admin panel saved model endpoints (runs/model_endpoints.json), use
    them (overriding config/env): the Qwen URL drives the bots' ACTION signal
    (sentiment + event + reasoning) + LLM comments + relevance refine; the
    embedding URL drives the news relevance cheap-filter. So you can point the
    bots at a Qwen / embedding model from the admin UI.

    Routing all three inference capabilities at Qwen makes every bot decision
    LLM-driven: the per-market signal (directional + confidence the personalities
    read in ``intent()``) is computed by the LLM — still ONCE per market, never
    per-bot, so cost stays O(markets) not O(bots).

    The Qwen URL is auto-detected: an OpenAI-compatible chat endpoint (api_key/
    model present, or a ``.../chat/completions`` URL — e.g. PodStack) wires the
    ``qwen_openai`` backend with Bearer auth + model; otherwise the legacy
    ``qwen_api`` custom server is used."""
    import json
    import os

    from sim.inference.openai_chat import is_openai_style

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", "model_endpoints.json")
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as fh:
            ep = json.load(fh)
    except (OSError, ValueError):
        return cfg
    qwen = (ep.get("qwen_url") or "").strip()
    key = (ep.get("qwen_api_key") or "").strip()
    model = (ep.get("qwen_model") or "").strip()
    emb = (ep.get("embedding_url") or "").strip()
    openai = is_openai_style(qwen, key)
    if qwen:
        inf = cfg.setdefault("inference", {})
        inf.setdefault("cache", {"enabled": True, "scope": "tick"})
        comments = cfg.setdefault("comments", {})
        comments["backend"] = "llm"
        comments["qwen_url"] = qwen
        refine = cfg.setdefault("news_feed", {}).setdefault("relevance", {}).setdefault("refine", {})
        refine["qwen_url"] = qwen
        backend = "qwen_openai" if openai else "qwen_api"
        # Route EVERY capability that feeds the bots' action signal at the LLM.
        inf["sentiment_backend"] = backend
        inf["event_backend"] = backend
        inf["reasoning_backend"] = backend
        if openai:
            inf.setdefault("qwen_openai", {}).update(
                {"type": "qwen_openai", "url": qwen, "api_key": key, "model": model})
            comments["qwen_api_key"] = key
            comments["qwen_model"] = model
            refine["api_key"] = key
            refine["model"] = model
        else:
            inf.setdefault("qwen_api", {}).update({"type": "qwen_api", "url": qwen})
    if emb:
        rel = cfg.setdefault("news_feed", {}).setdefault("relevance", {})
        rel["backend"] = "embeddings"
        rel.setdefault("embeddings", {})["url"] = emb
    if qwen or emb:
        print(f"  admin endpoints (runs/model_endpoints.json): "
              f"qwen={('LLM-driven actions ['+('openai' if openai else 'custom')+']') if qwen else '—'}, "
              f"embedding={'set' if emb else '—'}")
    return cfg


def _build_exchange(cfg: dict, fleet_size: int | None):
    """Construct the runner's exchange client per ``exchange.mode``:

    * ``internal`` (default) — Bearer ``INTERNAL_API_SECRET`` against
      ``/api/internal/*``; bots are seeded in the DB (scripts/seed-bots.ts).
    * ``public`` — bots are REAL users via the regular register/login API; no
      secret, no DB. Accounts persist in ``runs/kalki_accounts.json``.
    """
    ex = cfg["exchange"]
    mode = ex.get("mode", "internal")
    if mode == "public":
        count = int(fleet_size if fleet_size is not None else ex.get("fleet_size", 6))
        store = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs", "kalki_accounts.json")
        print(f"provisioning {count} bot(s) via the public register/login API "
              f"(register 3/min, login 8/min — first run is slow) …")
        bots = provision_fleet(ex["base_url"], count, store, timeout_s=ex.get("timeout_s", 20))
        if not bots:
            raise SystemExit("No bots could be provisioned (register/login failed) — check the base_url / connectivity.")
        # market_limit caps how many markets get a (shared) LLM signal per refresh,
        # bounding inference cost when reasoning is LLM-driven. Public trending max is 20.
        return PublicExchangeFacade(ex["base_url"], bots, timeout_s=ex.get("timeout_s", 20),
                                    market_limit=int(ex.get("market_limit", 20))), mode
    return KalkiClient(ex["base_url"], ex["internal_secret"], ex.get("timeout_s", 15)), mode


def _validate_internal_startup(ex: dict, client) -> None:
    """Fail-fast guardrail (audit #4) for internal mode: a real secret + an
    explicit KALKI_URL + a working authenticated probe, or exit the process.
    Prevents silent 401-spin and accidental hits on the production default."""
    secret = (ex.get("internal_secret") or "").strip()
    if not secret or secret == "CHANGE_ME_set_INTERNAL_API_SECRET":
        raise SystemExit("FATAL [startup]: INTERNAL_API_SECRET is empty or the placeholder — "
                         "set a real secret in the environment before running internal mode.")
    if not (os.environ.get("KALKI_URL") or "").strip():
        raise SystemExit("FATAL [startup]: KALKI_URL must be set explicitly for internal mode "
                         "(refusing to silently default to production).")
    base = ex.get("base_url") or ""
    if not (base.startswith("http://") or base.startswith("https://")):
        raise SystemExit(f"FATAL [startup]: base_url is not a valid http(s) URL: {base!r}")
    status, _ = client._req("GET", "/api/internal/bot-users")   # single authenticated probe
    if status in (401, 403):
        raise SystemExit(f"FATAL [startup]: internal API rejected the secret (HTTP {status}) — "
                         "INTERNAL_API_SECRET does not match the deployed bet server.")
    if status != 200:
        raise SystemExit(f"FATAL [startup]: internal API probe failed (HTTP {status}) — "
                         "backend unreachable or misconfigured.")
    print("  startup validation OK: internal secret + KALKI_URL + /api/internal/bot-users probe (200)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live Kalki Exchange bot fleet")
    ap.add_argument("--config", default="config.live.yaml")
    ap.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: run forever)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--fleet-size", type=int, default=None,
                    help="public mode: how many bots to register/login (overrides exchange.fleet_size)")
    ap.add_argument("--admin", type=int, nargs="?", const=8090, default=None,
                    help="serve the LOCAL-only fleet admin panel on this port (default 8090): "
                         "shows traded markets + URLs, and adds a market from a pasted local URL")
    ap.add_argument("--dry-run", action="store_true",
                    help="run the full pipeline (load bots/markets, signals, decisions, safety checks) "
                         "but submit NO trades/comments — log each as DRY RUN instead")
    ap.add_argument("--sim", action="store_true",
                    help="CROWD SIMULATION mode: attention-weighted market selection + new-market "
                         "boost + crowd logging (SANDBOX ONLY; refuses production). Reads the "
                         "`simulation:` config section.")
    ap.add_argument("--control", action="store_true",
                    help="with --sim: CONTROL arm — force Qwen OFF (local heuristic), no news attention")
    args = ap.parse_args()

    cfg = apply_admin_endpoints(load_config(args.config))
    ex = cfg["exchange"]

    # --- crowd-simulation mode (selection/logging layer only) -------------- #
    run_mode = "LIVE"
    if args.sim:
        if "kalki.bet" in (ex.get("base_url") or ""):
            raise SystemExit("FATAL: crowd-simulation (--sim) refuses to target PRODUCTION "
                             "(kalki.bet). Point KALKI_URL at a local/sandbox instance — this "
                             "is a closed agent-based simulation, not a real-user system.")
        if args.control:                       # CONTROL arm: remove all Qwen influence
            for k in ("sentiment_backend", "event_backend", "reasoning_backend"):
                cfg["inference"][k] = "local_heuristic"
            cfg.setdefault("comments", {})["backend"] = "template"
        run_mode = ("DRY_RUN_MODE" if args.dry_run else
                    "CONTROL_MODE" if args.control else "SIMULATION_MODE")
    qwen_active = (not args.control) and str(cfg["inference"].get("reasoning_backend", "")).startswith("qwen")

    # Start the admin panel FIRST so it's reachable immediately — before the
    # (potentially slow or failing) fleet startup. The runner attaches once built.
    panel = None
    if args.admin is not None:
        from live.admin_panel import FleetAdminPanel
        panel = FleetAdminPanel(None, ex["base_url"], port=args.admin)
        panel.serve_in_thread()

    comment_gen = CommentGenerator(
        backend=cfg["comments"].get("backend", "template"),
        qwen_url=cfg["comments"].get("qwen_url") or None,
        qwen_api_key=cfg["comments"].get("qwen_api_key") or None,
        qwen_model=cfg["comments"].get("qwen_model") or None,
    )
    inference = build_inference_client(cfg["inference"])
    news_feed = build_market_news_feed(cfg.get("news_feed"))
    rng_hub = RngHub(int(cfg.get("seed", 7)))

    inf_cfg = cfg["inference"]
    llm_caps = [c for c in ("sentiment", "event", "reasoning")
                if str(inf_cfg.get(f"{c}_backend", "")).startswith("qwen")]
    print(f"connecting to {ex['base_url']} … (mode: {ex.get('mode', 'internal')})")
    print(f"  bot-action inference: sentiment={inf_cfg.get('sentiment_backend', 'local_heuristic')} "
          f"event={inf_cfg.get('event_backend', 'local_heuristic')} "
          f"reasoning={inf_cfg.get('reasoning_backend', 'local_heuristic')}"
          + (f"  → LLM drives {'/'.join(llm_caps)}" if llm_caps else ""))
    client, mode = _build_exchange(cfg, args.fleet_size)
    if mode == "internal":
        _validate_internal_startup(ex, client)   # fail-fast before any trading (read-only probe)
    if args.dry_run:
        from live.dry_run import DryRunClient
        client = DryRunClient(client)            # reads pass through; trades/comments suppressed
        print("  *** DRY RUN — full pipeline runs; NO trades/comments are submitted to the exchange ***")
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
        hint = ("register them via the public API (exchange.mode: public)" if mode == "public"
                else "seed them first: (in bet/bet) npx tsx scripts/seed-bots.ts")
        raise SystemExit(f"No bot users — {hint}")

    runner = FleetRunner(client, comment_gen, inference, cfg, rng_hub, news_feed=news_feed)
    if args.sim:
        from live.crowd import AttentionModel
        from live.sim_logger import SimLogger
        sim_cfg = cfg.get("simulation", {})
        runner.attention = AttentionModel(sim_cfg, qwen_active=qwen_active)
        runner.sim_logger = SimLogger(sim_cfg.get("log_dir", "runs/sim_logs"), run_mode,
                                      label=sim_cfg.get("label", "SIMULATION_MODE"), qwen_active=qwen_active)
        nb = sim_cfg.get("new_market_boost", {})
        print(f"  *** {run_mode} · crowd attention ON · qwen_active={qwen_active} · "
              f"new-market target share={nb.get('target_new_share')} · logs → {runner.sim_logger.log_dir} ***")
    if panel is not None:
        panel.runner = runner   # fleet is up → panel now shows live markets + accepts injects
    runner.run(cycles=1 if args.once else args.cycles)
    if args.sim and runner.sim_logger is not None:
        runner.sim_logger.close()
    if llm_caps:
        # Proof the LLM actually drove the run: real upstream calls made to Qwen
        # (the per-tick cache dedupes, so this is # of distinct market signals).
        print(f"  LLM inference: {inference.upstream_calls} upstream call(s) to Qwen "
              f"({'/'.join(llm_caps)} per market signal)")


if __name__ == "__main__":
    main()
