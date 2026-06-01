"""Overconfident trader — reacts to news like the news bot but IGNORES the
model's confidence (treats every signal as near-certain), oversizes, and doubles
down on the side it already holds. Amplifies bubbles on the way up and takes the
worst losses on reversal — a behavioural-finance staple (overconfidence bias)."""
from __future__ import annotations

from ..market.types import MarketView
from ..signals import SignalLayer
from .base import BaseBot


class OverconfidentBot(BaseBot):
    kind = "overconfident"

    def __init__(self, *args, gain: float = 1.3, conviction_floor: float = 0.45, doubling: float = 0.12, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.gain = gain
        self.conviction_floor = conviction_floor
        self.doubling = doubling

    def intent(self, view: MarketView, signal: SignalLayer) -> float:
        # Treat confidence as ~1: act on raw direction, with a conviction floor.
        direction = signal.directional
        if abs(direction) < 1e-6:
            direction = view.last_return  # if no news, chase the last move anyway
        base = direction * self.gain
        if base > 0:
            base = max(base, self.conviction_floor)
        elif base < 0:
            base = min(base, -self.conviction_floor)
        # Double down: lean further toward the side already held.
        if self.yes_shares > self.no_shares:
            base += self.doubling
        elif self.no_shares > self.yes_shares:
            base -= self.doubling
        return max(-1.0, min(1.0, base))
