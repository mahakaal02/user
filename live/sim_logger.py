"""
Crowd-simulation instrumentation (logging only — no behavioral effect).

Writes newline-delimited JSON to ``<log_dir>/{trades,markets,cycles}.jsonl``.
EVERY row is stamped with the simulation label (``sim_mode``, default
``SIMULATION_MODE``) and the run mode — the labeling is mandatory and cannot be
emptied, so logged actions are never mistakable for organic activity.

Schemas
  trades.jsonl  per decided action — bot_type + signal_source (qwen/heuristic/noise),
                side/outcome/size, market age, new-market flag.
  markets.jsonl per market per logged cycle — attention weight + its components
                (news / momentum / herd / noise), age, picks.
  cycles.jsonl  per cycle — selected-market distribution, new-vs-old activity share,
                and the Qwen signal-influence strength.
"""
from __future__ import annotations

import json
import os
import time

from .crowd import _parse_epoch, signal_source


class SimLogger:
    def __init__(self, log_dir: str, run_mode: str, label: str = "SIMULATION_MODE",
                 qwen_active: bool = True, now_fn=time.time) -> None:
        # the label is mandatory — never allow it to be blank
        self.label = (label or "").strip() or "SIMULATION_MODE"
        self.run_mode = run_mode
        self.qwen_active = qwen_active
        self.now = now_fn
        os.makedirs(log_dir, exist_ok=True)
        self._trades = open(os.path.join(log_dir, "trades.jsonl"), "a", encoding="utf-8")
        self._markets = open(os.path.join(log_dir, "markets.jsonl"), "a", encoding="utf-8")
        self._cycles = open(os.path.join(log_dir, "cycles.jsonl"), "a", encoding="utf-8")
        self.log_dir = log_dir

    def _emit(self, fh, row: dict) -> None:
        row = {"ts": round(self.now(), 3), "sim_mode": self.label, "run_mode": self.run_mode, **row}
        fh.write(json.dumps(row) + "\n")
        fh.flush()

    # -- per decided action (tagged: BOT_TYPE, SIGNAL_SOURCE, SIMULATION_MODE)
    def log_decision(self, cycle: int, bot, market: dict, order, signal) -> None:
        side, outcome, amount = order
        age = None
        created = _parse_epoch(market.get("createdAt"))
        if created is not None:
            age = round(self.now() - created, 1)
        self._emit(self._trades, {
            "type": "trade",
            "cycle": cycle,
            "market_id": market["id"],
            "slug": market.get("slug"),
            "market_age_s": age,
            "is_new": bool(getattr(self, "_new_ids", set()) and market["id"] in self._new_ids),
            "bot_id": bot.user_id,
            "bot_type": bot.kind,
            "signal_source": signal_source(bot.kind, self.qwen_active),
            "side": side,
            "outcome": outcome,
            "size": round(float(amount), 4),
            "intent": round(float(bot.ema.get(market["id"], 0.0)), 4),
        })

    # -- per cycle: market attention rows + a cycle summary ---------------- #
    def log_cycle(self, cycle: int, attention, signals: dict) -> None:
        snap = attention.cycle_snapshot()
        self._new_ids = set(snap["new_market_ids"])     # used to tag trades as new/old
        # per-market attention over time
        for mid, comp in snap["components"].items():
            self._emit(self._markets, {
                "type": "market_attention",
                "cycle": cycle,
                "market_id": mid,
                "attention_weight": snap["weights"].get(mid, 0.0),
                "picks": snap["picks"].get(mid, 0),
                **comp,
            })
        # Qwen influence strength = mean(confidence * |directional|) over this cycle's signals
        infl = []
        for sig in signals.values():
            c = float(getattr(sig, "confidence", 0.0) or 0.0)
            d = abs(float(getattr(sig, "directional", 0.0) or 0.0))
            infl.append(c * d)
        qwen_influence = round(sum(infl) / len(infl), 5) if infl else 0.0
        self._emit(self._cycles, {
            "type": "cycle",
            "cycle": cycle,
            "n_markets": len(snap["weights"]),
            "n_new_markets": len(snap["new_market_ids"]),
            "new_market_pick_share": snap["new_market_pick_share"],
            "market_pick_distribution": snap["picks"],
            "qwen_influence_strength": qwen_influence if self.qwen_active else 0.0,
        })

    def close(self) -> None:
        for fh in (self._trades, self._markets, self._cycles):
            try:
                fh.close()
            except OSError:
                pass
