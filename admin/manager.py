"""
LiveSimulation — a controllable, real-time wrapper around the existing engine.

The batch ``sim.engine.Engine`` runs as fast as possible and exits. The admin
panel needs a *long-running, steerable* simulation, so this drives the SAME
building blocks (``build_inference_client`` / ``build_market`` /
``build_population`` / ``build_news_source`` / ``build_signal_layer``) one tick
at a time on a background thread, with:

  * pause/resume, speed control, reset,
  * live per-bot edits + pause/kill,
  * global knobs: news on/off, volatility, liquidity, aggression (stress), chaos,
  * a Server-Sent-Events fan-out so browsers get every tick live,
  * in-memory ring buffers for market history / events / analytics curves,
  * a replay mode that plays a recorded run back like a video.

Concurrency model: ONE background tick thread holds ``self._lock`` while it
mutates state; every HTTP handler takes the same lock for its read/mutate. A
tick is ~2 ms for 1000 bots, so handlers wait at most one tick. No per-bot
locking, no async — plain stdlib threads, matching the project's ethos.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque

from sim.bots import build_population
from sim.config import load_config
from sim.inference import build_inference_client
from sim.market import build_market
from sim.news import build_news_source
from sim.rng import RngHub
from sim.signals import build_signal_layer

DEAD_EQUITY = 5.0          # below this mark-to-market a bot is flagged 'dead'
HISTORY_LEN = 2000         # market-history ring
EVENT_LEN = 400            # event-log ring
CURVE_LEN = 240            # per-type / per-bot PnL curve length
TRADE_SAMPLE = 15          # max trades streamed per tick in the live feed


class LiveSimulation:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.cfg = load_config(config_path)

        # control state
        self.running = False
        self.news_enabled = True
        self.speed = 6.0           # ticks per second
        self.volatility = 1.0      # scales noise-trader amplitude
        self.liquidity_scale = 1.0 # scales AMM pool depth
        self.aggression_mult = 1.0 # global stress multiplier on aggressiveness
        self.chaos = 0.0           # 0..1: extra randomness + bias strength
        self.mode = "live"         # 'live' | 'replay'

        # infra
        self._lock = threading.RLock()
        self._stop = False
        self._subscribers: set[queue.Queue] = set()
        self._thread: threading.Thread | None = None

        # buffers
        self._history: deque[dict] = deque(maxlen=HISTORY_LEN)
        self._events: deque[dict] = deque(maxlen=EVENT_LEN)
        self._news_events: deque[dict] = deque(maxlen=EVENT_LEN)
        self._type_curve: dict[str, deque] = {}
        self._bot_curve: dict[str, deque] = {}

        # replay
        self._replay_rows: list[dict] = []
        self._replay_idx = 0
        self._replay_path: str | None = None

        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        seed = int(self.cfg["sim"].get("seed", 0))
        self.rng_hub = RngHub(seed)
        self.inference = build_inference_client(self.cfg["inference"])
        self.market = build_market(self.cfg["market"])
        self.news = build_news_source(
            self.cfg["sim"].get("news", {"source": "synthetic"}), self.rng_hub.stream("news")
        )
        self.bots = build_population(self.cfg["bots"], self.rng_hub)
        self.bots_by_id = {b.id: b for b in self.bots}
        self.clear_rng = self.rng_hub.stream("clearing")
        self.tick = 0

        # base params (so global multipliers compose instead of compounding)
        chaos_rng = self.rng_hub.stream("chaos")
        self._base = {}
        self._chaos_bias = {}
        for b in self.bots:
            self._base[b.id] = {
                "aggressiveness": b.aggressiveness,
                "bias": b.bias,
                "noise_scale": getattr(b, "scale", None),
            }
            self._chaos_bias[b.id] = chaos_rng.uniform(-0.4, 0.4)

        # per-type curve buffers
        self._type_curve = {k: deque(maxlen=CURVE_LEN) for k in {b.kind for b in self.bots}}
        self._bot_curve = {b.id: deque(maxlen=CURVE_LEN) for b in self.bots}
        self._history.clear()
        self._events.clear()
        self._news_events.clear()
        self.liquidity_scale = 1.0

    # ------------------------------------------------------------ lifecycle
    def start_thread(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="sim-loop", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop = True

    def _run_loop(self) -> None:
        while not self._stop:
            t0 = time.perf_counter()
            payload = None
            with self._lock:
                if self.running:
                    payload = self._replay_tick() if self.mode == "replay" else self._tick()
            if payload is not None:
                self._broadcast(payload)
            interval = 1.0 / max(self.speed, 0.1)
            time.sleep(max(0.0, interval - (time.perf_counter() - t0)))

    # ---------------------------------------------------------------- a tick
    def _tick(self) -> dict:
        t = self.tick
        market = self.market
        view = market.view(t)
        price_before = view.price_yes

        headlines = self.news.headlines(t) if self.news_enabled else []
        signal = build_signal_layer(t, headlines, self.inference, view.price_yes)

        for b in self.bots:
            b.observe(signal, view)
        orders = [o for b in self.bots if (o := b.decide()) is not None]
        for o in orders:
            market.submit(o)
        fills = market.clear(self.clear_rng)
        for f in fills:
            bot = self.bots_by_id.get(f.bot_id)
            if bot is not None:
                bot.apply_fill(f)

        post = market.view(t)
        price = post.price_yes

        # per-type aggregation + dead detection (single pass)
        agg: dict[str, dict] = {}
        for b in self.bots:
            eq = b.equity(price)
            if b.status == "active" and eq < DEAD_EQUITY:
                b.status = "dead"
            d = agg.setdefault(
                b.kind,
                {"count": 0, "active": 0, "paused": 0, "dead": 0, "equity": 0.0,
                 "in_profit": 0, "trades": 0, "delay": 0.0},
            )
            d["count"] += 1
            d[b.status] = d.get(b.status, 0) + 1
            d["equity"] += eq
            d["trades"] += b.trade_count
            d["delay"] += b.reaction_delay
            if eq > b.start_coins:
                d["in_profit"] += 1
            self._bot_curve[b.id].append(round(eq - b.start_coins, 2))

        start_coins = self.cfg["bots"].get("defaults", {}).get("coins", 1000.0)
        types = []
        for kind, d in sorted(agg.items()):
            avg_pnl = d["equity"] / d["count"] - start_coins
            self._type_curve[kind].append(round(avg_pnl, 2))
            types.append({
                "type": kind, "count": d["count"], "active": d["active"],
                "paused": d["paused"], "dead": d["dead"],
                "avg_pnl": round(avg_pnl, 1),
                "avg_pnl_pct": round(avg_pnl / max(start_coins, 1e-9) * 100, 1),
                "profit_share": round(d["in_profit"] / d["count"], 2),
                "trades": d["trades"],
                "avg_delay": round(d["delay"] / d["count"], 2),
            })

        # event log + sampled trades
        new_events = self._log_events(t, headlines, signal, fills, price_before, price)
        sampled = sorted(fills, key=lambda f: f.coins, reverse=True)[:TRADE_SAMPLE]
        trades = [{
            "bot_id": f.bot_id, "type": f.bot_id.rsplit("-", 1)[0],
            "side": f.side, "outcome": f.outcome,
            "coins": round(f.coins, 1), "shares": round(f.shares, 1),
            "price": round(f.avg_price, 3),
        } for f in sampled]

        self._history.append({
            "tick": t, "price": round(price, 5), "volume": round(post.last_volume, 1),
            "net_flow": round(post.last_net_flow, 1), "directional": signal.directional,
            "sent_pos": signal.sentiment["positive"], "sent_neg": signal.sentiment["negative"],
        })
        self.tick += 1

        return {
            "type": "tick", "tick": t,
            "market": {
                "price": round(price, 5), "prob": round(price, 5),
                "volume": round(post.last_volume, 1), "net_flow": round(post.last_net_flow, 1),
                "directional": signal.directional, "sentiment": signal.sentiment,
                "liquidity": round(market.reserves.yes_shares + market.reserves.no_shares, 0)
                if hasattr(market, "reserves") else None,
                "orders": len(orders), "trades": len(fills),
            },
            "news": headlines,
            "trades": trades,
            "events": new_events,
            "types": types,
            "control": self.control_snapshot(),
        }

    def _log_events(self, t, headlines, signal, fills, price_before, price) -> list[dict]:
        out = []
        for h in headlines:
            ev = {"tick": t, "kind": "news", "text": h,
                  "sentiment": signal.sentiment, "directional": signal.directional}
            out.append(ev)
            self._news_events.append(ev)
        if abs(price - price_before) >= 0.02:
            out.append({"tick": t, "kind": "price",
                        "text": f"price {price_before:.3f} → {price:.3f}"})
        for f in sorted(fills, key=lambda f: f.coins, reverse=True)[:2]:
            if f.coins >= 20:
                out.append({"tick": t, "kind": "trade",
                            "text": f"{f.bot_id} {f.side} {f.outcome} {f.shares:.0f}@{f.avg_price:.2f}"})
        for ev in out:
            self._events.append(ev)
        return out

    # ---------------------------------------------------------------- replay
    def load_replay(self, path: str) -> None:
        rows = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        self._replay_rows = rows
        self._replay_idx = 0
        self._replay_path = path
        self.mode = "replay"

    def _replay_tick(self) -> dict | None:
        if not self._replay_rows:
            return None
        if self._replay_idx >= len(self._replay_rows):
            self.running = False  # reached the end; pause
            return None
        row = self._replay_rows[self._replay_idx]
        self._replay_idx += 1
        price = row.get("price_yes", 0.5)
        sent = {"positive": row.get("sent_pos", row.get("inference", [{}])[0].get("sentiment", {}).get("positive", 0.0) if row.get("inference") else 0.0),
                "negative": 0.0, "neutral": 0.0}
        headlines = row.get("headlines", [])
        new_events = [{"tick": row.get("tick", self._replay_idx), "kind": "news", "text": h} for h in headlines]
        for ev in new_events:
            self._events.append(ev)
        self._history.append({"tick": row.get("tick"), "price": round(price, 5),
                              "volume": row.get("volume", 0), "net_flow": row.get("net_flow", 0),
                              "directional": row.get("directional", 0)})
        return {
            "type": "tick", "tick": row.get("tick", self._replay_idx),
            "market": {"price": round(price, 5), "prob": round(price, 5),
                       "volume": row.get("volume", 0), "net_flow": row.get("net_flow", 0),
                       "directional": row.get("directional", 0),
                       "sentiment": sent, "liquidity": None, "orders": 0, "trades": 0},
            "news": headlines, "trades": [], "events": new_events, "types": [],
            "control": self.control_snapshot(),
        }

    def replay_seek(self, idx: int) -> None:
        self._replay_idx = max(0, min(idx, len(self._replay_rows)))

    # -------------------------------------------------------- global controls
    def apply_globals(self) -> None:
        for b in self.bots:
            base = self._base[b.id]
            b.aggressiveness = base["aggressiveness"] * self.aggression_mult * (1.0 + 0.5 * self.chaos)
            b.bias = base["bias"] + self.chaos * self._chaos_bias[b.id]
            if base["noise_scale"] is not None:
                b.scale = base["noise_scale"] * self.volatility * (1.0 + 2.0 * self.chaos)

    def set_liquidity(self, scale: float) -> None:
        scale = max(0.1, float(scale))
        if hasattr(self.market, "reserves"):
            factor = scale / max(self.liquidity_scale, 1e-9)
            self.market.reserves.yes_shares *= factor
            self.market.reserves.no_shares *= factor
        self.liquidity_scale = scale

    def reset_sim(self) -> None:
        # rebuild everything from config; keep control knobs + subscribers
        self._build()
        self.apply_globals()

    def control_snapshot(self) -> dict:
        return {
            "running": self.running, "news_enabled": self.news_enabled,
            "speed": self.speed, "volatility": round(self.volatility, 2),
            "liquidity_scale": round(self.liquidity_scale, 2),
            "aggression_mult": round(self.aggression_mult, 2), "chaos": round(self.chaos, 2),
            "mode": self.mode, "tick": self.tick, "n_bots": len(self.bots),
            "replay": {"idx": self._replay_idx, "total": len(self._replay_rows),
                       "path": self._replay_path} if self.mode == "replay" else None,
        }

    # ------------------------------------------------------------ SSE fan-out
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def _broadcast(self, payload: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # slow client — drop this frame rather than block the sim

    # ----------------------------------------------------------- read models
    def bots_table(self, type_filter=None, status_filter=None, sort="pnl", limit=2000) -> list[dict]:
        price = self._price()
        rows = []
        for b in self.bots:
            if type_filter and b.kind != type_filter:
                continue
            if status_filter and b.status != status_filter:
                continue
            rows.append(b.snapshot(price))
        rows.sort(key=lambda r: r.get(sort, 0), reverse=True)
        return rows[:limit]

    def bot_detail(self, bot_id: str) -> dict | None:
        b = self.bots_by_id.get(bot_id)
        if b is None:
            return None
        snap = b.snapshot(self._price())
        snap["equity_curve"] = list(self._bot_curve.get(bot_id, []))
        snap["base_params"] = self._base.get(bot_id)
        return snap

    def market_state(self) -> dict:
        price = self._price()
        return {"price": round(price, 5), "prob": round(price, 5),
                "tick": self.tick, "control": self.control_snapshot(),
                "history_len": len(self._history)}

    def market_history(self) -> list[dict]:
        return list(self._history)

    def market_events(self) -> list[dict]:
        return list(self._news_events)

    def event_log(self) -> list[dict]:
        return list(self._events)

    def analytics(self) -> dict:
        """Per-type behavioural analytics: PnL curve, win/loss, avg size, delay,
        and bias effectiveness (which personalities are winning)."""
        price = self._price()
        by_type: dict[str, dict] = {}
        for b in self.bots:
            d = by_type.setdefault(b.kind, {
                "count": 0, "pnl": 0.0, "wins": 0, "losses": 0, "trades": 0,
                "volume": 0.0, "delay": 0.0, "in_profit": 0,
            })
            d["count"] += 1
            d["pnl"] += b.equity(price) - b.start_coins
            d["wins"] += b.wins
            d["losses"] += b.losses
            d["trades"] += b.trade_count
            d["volume"] += b.volume_traded
            d["delay"] += b.reaction_delay
            if b.equity(price) > b.start_coins:
                d["in_profit"] += 1
        out = []
        for kind, d in sorted(by_type.items(), key=lambda kv: -kv[1]["pnl"] / max(kv[1]["count"], 1)):
            n = d["count"]
            out.append({
                "type": kind, "count": n,
                "avg_pnl": round(d["pnl"] / n, 1),
                "avg_pnl_pct": round(d["pnl"] / n / max(self._start_coins(), 1e-9) * 100, 1),
                "win_loss_ratio": round(d["wins"] / max(d["losses"], 1), 2),
                "wins": d["wins"], "losses": d["losses"],
                "avg_trade_size": round(d["volume"] / max(d["trades"], 1), 1),
                "trades_per_bot": round(d["trades"] / n, 1),
                "avg_reaction_delay": round(d["delay"] / n, 2),
                "profit_share": round(d["in_profit"] / n, 2),
                "pnl_curve": list(self._type_curve.get(kind, [])),
            })
        return {"types": out, "tick": self.tick}

    # ------------------------------------------------------------- internals
    def _price(self) -> float:
        return self.market.view(self.tick).price_yes

    def _start_coins(self) -> float:
        return self.cfg["bots"].get("defaults", {}).get("coins", 1000.0)
