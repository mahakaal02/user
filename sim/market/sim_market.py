"""
In-process simulation market (the default). Uses the ported constant-product
AMM as the price-formation mechanism: every bot order moves the marginal price,
so trends, bubbles and crashes are emergent rather than scripted.

Clearing each tick:
  1. The queued orders are shuffled with the seeded RNG (fair, reproducible).
  2. Each is executed against the AMM in turn — earlier orders move the price
     the later ones see (this intra-tick path dependence is what lets herding
     and momentum compound into bubbles).
  3. Per-order fills are returned; the engine applies them to bot wallets.
  4. Net YES-ward coin flow and gross volume are recorded for next tick's view
     (the herd bots read ``last_net_flow``).

The market does not own wallets/positions — the engine does — so this class
stays a pure price engine, exactly like ``lib/amm.ts`` is on the TS side.
"""
from __future__ import annotations

from collections import deque

from . import amm
from .gateway import MarketGateway
from .types import Fill, MarketView, Order


class SimMarket(MarketGateway):
    def __init__(
        self,
        yes_shares: float = 1000.0,
        no_shares: float = 1000.0,
        history_len: int = 64,
    ) -> None:
        self.reserves = amm.Reserves(yes_shares, no_shares)
        self._queue: list[Order] = []
        self._history: deque[float] = deque(maxlen=history_len)
        self._history.append(amm.price_yes(self.reserves))
        self._last_net_flow = 0.0
        self._last_volume = 0.0

    # -- observation ------------------------------------------------------- #
    def view(self, tick: int) -> MarketView:
        return MarketView(
            tick=tick,
            price_yes=amm.price_yes(self.reserves),
            price_history=list(self._history),
            last_net_flow=self._last_net_flow,
            last_volume=self._last_volume,
            reserves=(self.reserves.yes_shares, self.reserves.no_shares),
        )

    # -- order intake ------------------------------------------------------ #
    def submit(self, order: Order) -> None:
        self._queue.append(order)

    # -- clearing ---------------------------------------------------------- #
    def clear(self, rng) -> list[Fill]:
        orders = self._queue
        self._queue = []
        rng.shuffle(orders)  # fair, deterministic execution order

        fills: list[Fill] = []
        net_flow = 0.0
        volume = 0.0
        for o in orders:
            fill = self._execute(o)
            if fill is None:
                continue
            fills.append(fill)
            volume += fill.coins
            net_flow += _yes_ward(fill)

        self._last_net_flow = net_flow
        self._last_volume = volume
        self._history.append(amm.price_yes(self.reserves))
        return fills

    def _execute(self, o: Order) -> Fill | None:
        if o.side == "BUY":
            q = amm.quote_buy(self.reserves, o.outcome, o.coins)
            if q is None:
                return None
            self.reserves = q.new_reserves
            return Fill(o.bot_id, "BUY", o.outcome, o.coins, q.shares_out, q.avg_price, q.new_yes_price)
        else:  # SELL
            q = amm.quote_sell(self.reserves, o.outcome, o.shares)
            if q is None:
                return None
            self.reserves = q.new_reserves
            return Fill(o.bot_id, "SELL", o.outcome, q.coins_out, o.shares, q.avg_price, q.new_yes_price)

    def resolve(self, outcome: str) -> dict:
        return {"resolved": outcome, "final_price_yes": amm.price_yes(self.reserves)}


def _yes_ward(f: Fill) -> float:
    """Signed coin flow on the YES axis — the crowd-direction signal herd bots
    follow. Buying YES or selling NO is bullish (+); the opposites are bearish."""
    bullish = (f.side == "BUY" and f.outcome == "YES") or (f.side == "SELL" and f.outcome == "NO")
    return f.coins if bullish else -f.coins
