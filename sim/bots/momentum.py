"""Momentum trader — buys what's rising, sells what's falling. Trend follower;
the primary amplifier that turns a news nudge into a bubble."""
from __future__ import annotations

from ..market.types import MarketView
from ..signals import SignalLayer
from .base import BaseBot


class MomentumBot(BaseBot):
    kind = "momentum"

    def __init__(self, *args, lookback: int = 5, gain: float = 5.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lookback = lookback
        self.gain = gain

    def intent(self, view: MarketView, signal: SignalLayer) -> float:
        h = view.price_history
        if len(h) < 2:
            return 0.0
        w = min(self.lookback, len(h) - 1)
        recent_return = h[-1] - h[-1 - w]
        # A sustained move of a few cents → strong conviction in its direction.
        return max(-1.0, min(1.0, recent_return * self.gain))
