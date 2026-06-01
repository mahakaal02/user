"""
Offline, deterministic inference backend.

A dependency-free stand-in for the remote ML services. It produces *plausible*,
*repeatable* sentiment/event output by hashing the input text — no network, no
GPU, no model download. This is what lets the whole simulator run on a low-end
laptop and replay bit-for-bit, and it is the default backend in
``config.offline.yaml``.

It is NOT a real model: it keys off a small finance lexicon plus a deterministic
hash so identical text always yields identical scores. Swap it for the FinBERT /
Qwen adapters by changing ``config.yaml`` only.
"""
from __future__ import annotations

import hashlib

from .base import InferenceClient, normalize_event, normalize_sentiment

_POS_WORDS = (
    "surge", "rally", "beat", "growth", "cut", "ease", "optimism", "record",
    "win", "approve", "soar", "jump", "upgrade", "bullish", "stimulus", "gain",
)
_NEG_WORDS = (
    "crash", "miss", "recession", "hike", "fear", "default", "ban", "downgrade",
    "selloff", "plunge", "slump", "bearish", "sanction", "war", "fraud", "loss",
)
_EVENT_LEXICON = {
    "rate cut": ("interest_rate_cut", {"market_up": 0.8, "inflation_down": 0.5}),
    "rate hike": ("interest_rate_hike", {"market_down": 0.7, "inflation_up": 0.4}),
    "earnings beat": ("earnings_beat", {"market_up": 0.7}),
    "earnings miss": ("earnings_miss", {"market_down": 0.7}),
    "recession": ("recession_signal", {"market_down": 0.85}),
    "stimulus": ("fiscal_stimulus", {"market_up": 0.6, "inflation_up": 0.3}),
    "default": ("credit_default", {"market_down": 0.9}),
    "merger": ("merger", {"market_up": 0.5}),
}


def _hash_unit(text: str, salt: str) -> float:
    """Deterministic float in [0, 1) from text — stable across machines."""
    h = hashlib.blake2b(f"{salt}:{text}".encode("utf-8"), digest_size=8)
    return (int.from_bytes(h.digest(), "big") % 10_000) / 10_000.0


class LocalHeuristicClient(InferenceClient):
    name = "local_heuristic"

    def sentiment(self, text: str) -> dict:
        t = (text or "").lower()
        pos = sum(1.0 for w in _POS_WORDS if w in t)
        neg = sum(1.0 for w in _NEG_WORDS if w in t)
        # Deterministic jitter so headlines without lexicon hits still vary.
        jitter = _hash_unit(text, "sent")
        pos += jitter
        neg += (1.0 - jitter) * 0.6
        neu = 0.8  # baseline neutral mass
        conf = min(1.0, 0.4 + abs(pos - neg) / max(pos + neg + neu, 1e-9))
        return normalize_sentiment(
            {"positive": pos, "negative": neg, "neutral": neu, "confidence": conf}
        )

    def event_extract(self, text: str) -> dict:
        t = (text or "").lower()
        for phrase, (event, impact) in _EVENT_LEXICON.items():
            if phrase in t:
                conf = 0.6 + 0.4 * _hash_unit(text, "evt")
                return normalize_event({"event": event, "impact": impact, "confidence": conf})
        # No lexicon hit → derive a weak directional event from sentiment.
        s = self.sentiment(text)
        direction = s["positive"] - s["negative"]
        key = "market_up" if direction >= 0 else "market_down"
        return normalize_event(
            {"event": "general_news", "impact": {key: abs(direction)}, "confidence": 0.3 + 0.3 * s["confidence"]}
        )

    def reasoning(self, prompt: str) -> dict:
        # Treat the state prompt like text: extract an event and attach a
        # deterministic confidence. A real reasoner (Qwen) would synthesize
        # across the prompt's market state + recent headlines.
        ev = self.event_extract(prompt)
        ev["confidence"] = round(min(1.0, ev["confidence"] * 0.9 + 0.1 * _hash_unit(prompt, "reason")), 6)
        return ev
