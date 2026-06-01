"""News-reactive trader — trades the (shared) inference signal directly, scaled
by confidence. Configured with a SHORT reaction delay, so it moves first on a
shock; the slower momentum/herd crowd piles in afterwards, which is what creates
the *delayed* news reaction visible in the price path."""
from __future__ import annotations

from ..market.types import MarketView
from ..signals import SignalLayer
from .base import BaseBot


class NewsReactiveBot(BaseBot):
    kind = "news_reactive"

    def __init__(self, *args, gain: float = 1.4, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.gain = gain

    def intent(self, view: MarketView, signal: SignalLayer) -> float:
        # Directional pressure from inference, trusted in proportion to its
        # confidence. No model call here — just reads the shared signal layer.
        return max(-1.0, min(1.0, signal.directional * signal.confidence * self.gain))
