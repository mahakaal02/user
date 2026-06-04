"""
Crowd-simulation attention model — the market-SELECTION (routing) layer.

This decides WHICH market each bot looks at every cycle, via a soft weight:

    weight(m) = base_attention
              + qwen_weight     * qwen_news_score(m)      (Qwen signal relevance)
              + momentum_weight * momentum_score(m)       (|recent price move|)
              + herd_multiplier * herd_score(m)           (recently-traded → more attention)
              + noise_floor     * U(0,1)                  (baseline randomness)

Newly-created markets are over-weighted via a BUCKET ALLOCATION: when both new
and old markets exist, exactly ``target_new_share`` of the selection probability
mass is routed to the new-market bucket (default 0.60), distributed within it by
a decaying recency boost (exponential or power-law, tunable half-life). With no
new markets, no artificial boost is applied.

It NEVER changes the trade decision (``intent``/``_decide``/``size_coins``), the
order size, the AMM, or any safety system — it only biases attention/selection.
Signal-source attribution (qwen / heuristic / noise) is derived from the bot's
personality for instrumentation only.
"""
from __future__ import annotations

import math
import time

# personality → which signal source drives its decision (for SIGNAL_SOURCE tagging)
_QWEN_PERSONALITIES = ("news_reactive", "overconfident")   # these read signal.directional
_NOISE_PERSONALITIES = ("noise",)


def signal_source(bot_kind: str, qwen_active: bool) -> str:
    """qwen | heuristic | noise — what actually drove this bot's decision."""
    if bot_kind in _NOISE_PERSONALITIES:
        return "noise"
    if qwen_active and bot_kind in _QWEN_PERSONALITIES:
        return "qwen"
    return "heuristic"


def _parse_epoch(created) -> float | None:
    """Best-effort epoch seconds from an ISO-8601 string or numeric timestamp."""
    if created is None:
        return None
    if isinstance(created, (int, float)):
        return float(created) / (1000.0 if created > 1e12 else 1.0)
    if isinstance(created, str):
        s = created.strip().replace("Z", "+00:00")
        try:
            from datetime import datetime
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
    return None


class AttentionModel:
    """Per-cycle market weights + a weighted sampler. Stateful across cycles for
    the herd (clustering) and reaction-delay (news EMA) effects."""

    def __init__(self, sim_cfg: dict, qwen_active: bool = True, now_fn=time.time) -> None:
        self.a = dict(sim_cfg.get("attention", {}))
        self.nb = dict(sim_cfg.get("new_market_boost", {}))
        self.qwen_active = qwen_active
        self.now = now_fn
        self._herd: dict[str, float] = {}       # market_id -> decaying recent-pick count
        self._news_ema: dict[str, float] = {}   # market_id -> smoothed news score (reaction delay)
        self.weights: dict[str, float] = {}      # current-cycle normalized weights
        self.components: dict[str, dict] = {}    # per-market component breakdown (for logging)
        self.picks: dict[str, int] = {}          # market_id -> picks this cycle
        self._by_id: dict[str, dict] = {}
        self.new_ids: set[str] = set()

    # -- decay curve for the new-market boost ------------------------------ #
    def _recency(self, age_s: float) -> float:
        hl = max(1.0, float(self.nb.get("half_life_s", 1800)))
        if str(self.nb.get("decay_function", "exponential")) == "power_law":
            return 1.0 / (1.0 + age_s / hl)
        return math.pow(0.5, age_s / hl)        # exponential: halves every half_life

    def _qwen_news_score(self, signal) -> float:
        if not self.qwen_active or signal is None:
            return 0.0
        conf = float(getattr(signal, "confidence", 0.0) or 0.0)
        directional = abs(float(getattr(signal, "directional", 0.0) or 0.0))
        intensity = float(getattr(signal, "news_intensity", 0.0) or 0.0)
        return conf * directional + 0.5 * intensity

    # -- compute the cycle's weights --------------------------------------- #
    def begin_cycle(self, cycle: int, markets: list[dict], signals: dict, hist: dict, rng) -> None:
        a = self.a
        self._by_id = {m["id"]: m for m in markets}
        now = self.now()
        cutoff = float(self.nb.get("cutoff_age_s", 7200))
        # herd memory decays each cycle (recent activity attracts, then fades)
        for mid in list(self._herd):
            self._herd[mid] *= 0.7

        intrinsic: dict[str, float] = {}
        ages: dict[str, float] = {}
        self.components = {}
        self.new_ids = set()
        for m in markets:
            mid = m["id"]
            sig = signals.get(mid)
            raw_news = self._qwen_news_score(sig)
            # reaction delay: ramp attention to news over news_delay_cycles (EMA)
            k = 1.0 / max(1.0, float(a.get("news_delay_cycles", 1)))
            self._news_ema[mid] = (1 - k) * self._news_ema.get(mid, 0.0) + k * raw_news
            news = self._news_ema[mid]
            h = hist.get(mid)
            mom = abs(h[-1] - h[-min(5, len(h))]) if h and len(h) > 1 else 0.0
            herd = self._herd.get(mid, 0.0)
            noise = rng.random()
            w = (float(a.get("base_attention", 1.0))
                 + float(a.get("qwen_weight", 0.0)) * news
                 + float(a.get("momentum_weight", 0.0)) * mom
                 + float(a.get("herd_multiplier", 0.0)) * herd
                 + float(a.get("noise_floor", 0.0)) * noise)
            intrinsic[mid] = max(1e-9, w)
            age = now - (_parse_epoch(m.get("createdAt")) or (now - cutoff - 1))
            ages[mid] = age
            if age < cutoff:
                self.new_ids.add(mid)
            self.components[mid] = {"news": round(news, 5), "momentum": round(mom, 5),
                                    "herd": round(herd, 3), "noise": round(noise, 3),
                                    "age_s": round(age, 1), "is_new": age < cutoff}

        self.weights = self._allocate(intrinsic, ages, cutoff)
        self.picks = {}

    def _allocate(self, intrinsic: dict, ages: dict, cutoff: float) -> dict[str, float]:
        """Bucket allocation → guarantees ~target_new_share to the new bucket."""
        new = {mid: w * self._recency(ages[mid]) for mid, w in intrinsic.items() if ages[mid] < cutoff}
        old = {mid: w for mid, w in intrinsic.items() if ages[mid] >= cutoff}
        weights: dict[str, float] = {}
        if new and old:
            tn = min(1.0, max(0.0, float(self.nb.get("target_new_share", 0.6))))
            sn = sum(new.values()) or 1.0
            so = sum(old.values()) or 1.0
            for mid, w in new.items():
                weights[mid] = (w / sn) * tn
            for mid, w in old.items():
                weights[mid] = (w / so) * (1.0 - tn)
        else:                                    # no new markets → no artificial boost
            pool = new or old or intrinsic
            s = sum(pool.values()) or 1.0
            weights = {mid: w / s for mid, w in pool.items()}
        return weights

    # -- weighted sampling -------------------------------------------------- #
    def pick_market(self, rng) -> dict:
        total = sum(self.weights.values()) or 1.0
        x = rng.random() * total
        acc = 0.0
        chosen = None
        for mid, w in self.weights.items():
            acc += w
            if x <= acc:
                chosen = mid
                break
        if chosen is None:
            chosen = next(iter(self.weights))
        self._herd[chosen] = self._herd.get(chosen, 0.0) + 1.0    # clustering feedback
        self.picks[chosen] = self.picks.get(chosen, 0) + 1
        return self._by_id[chosen]

    # -- read-outs for instrumentation ------------------------------------- #
    def new_market_share(self) -> float:
        total = sum(self.picks.values())
        if not total:
            return 0.0
        return sum(c for mid, c in self.picks.items() if mid in self.new_ids) / total

    def cycle_snapshot(self) -> dict:
        return {
            "weights": {mid: round(w, 6) for mid, w in self.weights.items()},
            "picks": dict(self.picks),
            "new_market_ids": sorted(self.new_ids),
            "new_market_pick_share": round(self.new_market_share(), 4),
            "components": self.components,
        }
