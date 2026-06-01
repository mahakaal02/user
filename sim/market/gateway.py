"""
The market interface — mirrors the pluggable-inference pattern on the market
side. Bots and the engine depend only on :class:`MarketGateway`; the concrete
market (in-process ``SimMarket`` or HTTP-bridged ``HttpMarket``) is chosen from
config. So "internal sim" vs "drive the real bet API" is a config switch with no
engine changes — the same flexibility the inference layer has.
"""
from __future__ import annotations

import abc

from .types import Fill, MarketView, Order


class MarketGateway(abc.ABC):
    @abc.abstractmethod
    def view(self, tick: int) -> MarketView:
        """Snapshot all bots observe this tick (price, history, last flow)."""

    @abc.abstractmethod
    def submit(self, order: Order) -> None:
        """Queue an order for this tick's clearing."""

    @abc.abstractmethod
    def clear(self, rng) -> list[Fill]:
        """Execute all queued orders (in a deterministic shuffled order) and
        return the fills. Updates price/reserves. ``rng`` is a seeded
        ``random.Random`` for fair, reproducible ordering."""

    def resolve(self, outcome: str) -> dict:
        """Optionally resolve the market (YES/NO). Default: no-op metadata."""
        return {"resolved": outcome}
