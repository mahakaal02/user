"""
The pluggable inference contract.

Every ML backend — local heuristic, remote FinBERT, remote Qwen, or a replay
log — implements :class:`InferenceClient`. Bots and the signal layer depend ONLY
on this interface, never on a concrete adapter, a URL, or a model. Swapping
FinBERT for a different sentiment model, or Qwen for a different reasoner, is a
one-line change in ``config.yaml``.

All adapters MUST return the standardized schemas below. The ``normalize_*``
helpers are the single choke-point that coerces any backend's raw output into
those schemas, so downstream code can assume the shape is always valid.
"""
from __future__ import annotations

import abc
from typing import Any, Mapping

# --------------------------------------------------------------------------- #
# Standardized response schemas (MANDATORY — every backend returns these).
# --------------------------------------------------------------------------- #
#
# Sentiment:
#   {"positive": 0.7, "negative": 0.1, "neutral": 0.2, "confidence": 0.85}
#
# Reasoning / event extraction:
#   {"event": "interest_rate_cut",
#    "impact": {"market_up": 0.8, "inflation_down": 0.6},
#    "confidence": 0.77}
#
# `impact` keys are free-form, but the simulator understands a canonical
# directional vocabulary so any backend's taxonomy maps onto market pressure.
# Anything matching *_up / market_up / bullish pushes YES up; *_down / bearish
# pushes it down. Unknown keys are ignored by the market but preserved in logs.

SENTIMENT_KEYS = ("positive", "negative", "neutral", "confidence")

# Substrings that mark an impact key as bullish (+) or bearish (-) for the
# *market* (i.e. pressure on the YES price). Extend freely — unknown keys are
# kept in the payload but contribute 0 directional pressure.
_BULLISH_HINTS = ("market_up", "price_up", "bullish", "rally", "up", "rise", "gain", "positive")
_BEARISH_HINTS = ("market_down", "price_down", "bearish", "crash", "down", "fall", "drop", "negative")


def _clamp(x: Any, lo: float = 0.0, hi: float = 1.0, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def normalize_sentiment(raw: Mapping[str, Any] | None) -> dict[str, float]:
    """Coerce any backend output into the canonical sentiment schema.

    Guarantees: keys positive/negative/neutral sum to 1.0 (renormalized if the
    backend's don't), and a confidence in [0, 1] is always present.
    """
    raw = raw or {}
    pos = _clamp(raw.get("positive"))
    neg = _clamp(raw.get("negative"))
    neu = _clamp(raw.get("neutral"))
    total = pos + neg + neu
    if total <= 0:
        pos, neg, neu = 0.0, 0.0, 1.0  # default to fully neutral
        total = 1.0
    pos, neg, neu = pos / total, neg / total, neu / total
    # If the backend gave no explicit confidence, infer it from how decisive
    # the distribution is (1 - neutral mass, blended with peak class).
    conf = raw.get("confidence")
    confidence = _clamp(conf, default=round(max(pos, neg) * (1.0 - neu) + max(pos, neg) * neu * 0.0, 6))
    return {
        "positive": round(pos, 6),
        "negative": round(neg, 6),
        "neutral": round(neu, 6),
        "confidence": round(confidence, 6),
    }


def normalize_event(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Coerce any backend output into the canonical event/reasoning schema."""
    raw = raw or {}
    event = str(raw.get("event") or "none")
    impact_raw = raw.get("impact") or {}
    impact: dict[str, float] = {}
    if isinstance(impact_raw, Mapping):
        for k, v in impact_raw.items():
            impact[str(k)] = _clamp(v, lo=-1.0, hi=1.0)
    confidence = _clamp(raw.get("confidence"))
    return {"event": event, "impact": impact, "confidence": confidence}


def event_directional_pressure(event: Mapping[str, Any]) -> float:
    """Collapse an event's free-form ``impact`` dict into a scalar in [-1, 1].

    Positive → pressure pushing the YES price up; negative → down. This is the
    only place the simulator interprets the (otherwise opaque) impact taxonomy,
    so a new backend with new keys needs no bot changes — just keys whose names
    contain a recognised hint.
    """
    impact = event.get("impact") or {}
    score = 0.0
    weight = 0.0
    for key, val in impact.items():
        k = str(key).lower()
        v = float(val)
        if any(h in k for h in _BEARISH_HINTS) and not any(h in k for h in _BULLISH_HINTS):
            score -= abs(v)
            weight += abs(v)
        elif any(h in k for h in _BULLISH_HINTS):
            score += v if "down" not in k else -abs(v)
            weight += abs(v)
        # unknown keys: ignored for direction, but counted as mild uncertainty
    if weight <= 0:
        return 0.0
    return max(-1.0, min(1.0, score / max(weight, 1e-9)))


class InferenceClient(abc.ABC):
    """Unified inference interface. The ONLY ML surface bots ever see."""

    name: str = "inference"

    @abc.abstractmethod
    def sentiment(self, text: str) -> dict:
        """Return the canonical sentiment schema for ``text``."""

    @abc.abstractmethod
    def event_extract(self, text: str) -> dict:
        """Return the canonical event schema extracted from ``text``."""

    @abc.abstractmethod
    def reasoning(self, prompt: str) -> dict:
        """Return the canonical event schema reasoned from a state ``prompt``."""

    # Lifecycle hook — overridden by the caching wrapper to scope a per-tick
    # memo. No-op by default so every backend can be used without a cache.
    def new_tick(self, tick: int) -> None:  # noqa: D401 - simple hook
        """Signal the start of a new simulation tick (cache boundary)."""

    @property
    def upstream_calls(self) -> int:
        """Number of real upstream inference calls made (0 if not tracked)."""
        return 0
