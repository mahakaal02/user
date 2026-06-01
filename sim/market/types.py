"""Shared market value types: what bots emit (Order), what they get back (Fill),
and what they observe each tick (MarketView)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Order:
    """A bot's trade intent for this tick.

    BUY uses ``coins`` (AMM market-buy of ``outcome`` with that many coins).
    SELL uses ``shares`` (sell that many ``outcome`` shares back to the AMM).
    ``limit_price`` is only consulted by the CLOB path (optional).
    """

    bot_id: str
    side: str          # "BUY" | "SELL"
    outcome: str       # "YES" | "NO"
    coins: float = 0.0
    shares: float = 0.0
    limit_price: float | None = None


@dataclass
class Fill:
    """The realized result of an executed order."""

    bot_id: str
    side: str
    outcome: str
    coins: float        # coins paid (BUY) or received (SELL), always >= 0
    shares: float       # shares gained (BUY) or sold (SELL), always >= 0
    avg_price: float
    price_after: float


@dataclass
class MarketView:
    """Read-only snapshot every bot observes at the top of a tick. Identical for
    all bots — the shared observation surface."""

    tick: int
    price_yes: float
    price_history: list[float] = field(default_factory=list)  # bounded, oldest→newest
    last_net_flow: float = 0.0   # signed YES-ward coin flow last tick (herd signal)
    last_volume: float = 0.0     # gross coins traded last tick
    reserves: tuple[float, float] = (0.0, 0.0)  # (yes_shares, no_shares)

    @property
    def last_return(self) -> float:
        """Most recent 1-tick price change (0 if not enough history)."""
        if len(self.price_history) < 2:
            return 0.0
        return self.price_history[-1] - self.price_history[-2]
