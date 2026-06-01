"""Noise trader — trades on essentially nothing: a small random directional
impulse each tick. Provides baseline liquidity and the stochastic 'temperature'
that keeps the market from sitting perfectly still, so trends have something to
catch on. Classic Kyle/Black noise-trader role."""
from __future__ import annotations

from ..market.types import MarketView
from ..signals import SignalLayer
from .base import BaseBot


class NoiseBot(BaseBot):
    kind = "noise"

    def __init__(self, *args, scale: float = 0.25, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.scale = scale

    def intent(self, view: MarketView, signal: SignalLayer) -> float:
        # Zero-mean random impulse from the bot's own deterministic stream.
        return self.rng.uniform(-1.0, 1.0) * self.scale
