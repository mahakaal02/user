"""Contrarian trader — fades extremes and sharp moves. Bets the price reverts
toward 0.5. The main corrective force: it leans against bubbles and buys crashes,
producing the mean-reverting 'contrarian correction'."""
from __future__ import annotations

from ..market.types import MarketView
from ..signals import SignalLayer
from .base import BaseBot


class ContrarianBot(BaseBot):
    kind = "contrarian"

    def __init__(self, *args, fair_value: float = 0.5, gain: float = 5.0, fade_move: float = 1.2, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fair_value = fair_value
        self.gain = gain
        self.fade_move = fade_move

    def intent(self, view: MarketView, signal: SignalLayer) -> float:
        # Nonlinear lean against the level: weak near fair value (so mid-range
        # trends/bubbles can run), strong near the extremes (so it forces the
        # reversal that ends a bubble or a crash). Quadratic in the gap.
        gap = self.fair_value - view.price_yes
        level = self.gain * gap * abs(gap) * 4.0
        # ...plus a mild fade of the most recent move.
        fade = -view.last_return * self.fade_move
        return max(-1.0, min(1.0, level + fade))
