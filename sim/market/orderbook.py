"""
Minimal price-time-priority CLOB — a compact Python echo of
``bet/lib/orderbook.ts`` (``matchIncoming`` / ``buildLadder``). The default sim
uses the AMM for price formation; this module is provided so the orderbook
surface exists for experiments that need exact-quantity limit fills, and so the
HTTP bridge's ``/api/orders`` payloads have a local analogue.

Pure functions, no I/O — easy to unit-test, just like the TS original.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BookOrder:
    order_id: str
    side: str          # "BUY" | "SELL"
    price: float       # limit price in (0, 1)
    shares: float
    ts: int            # arrival sequence, for time priority


@dataclass
class Match:
    taker_id: str
    maker_id: str
    price: float
    shares: float


@dataclass
class Book:
    bids: list[BookOrder] = field(default_factory=list)  # BUY, best (highest) first
    asks: list[BookOrder] = field(default_factory=list)  # SELL, best (lowest) first

    def _resort(self) -> None:
        self.bids.sort(key=lambda o: (-o.price, o.ts))
        self.asks.sort(key=lambda o: (o.price, o.ts))


def match_incoming(book: Book, incoming: BookOrder) -> tuple[list[Match], BookOrder | None]:
    """Match ``incoming`` against the resting opposite side (best price first,
    time tie-break), with self-trade prevention. Returns (fills, remainder) where
    remainder is the unfilled maker order to rest, or None if fully filled.
    Price improvement accrues to the taker (fills at the maker's price)."""
    matches: list[Match] = []
    remaining = incoming.shares
    resting = book.asks if incoming.side == "BUY" else book.bids

    def crosses(maker: BookOrder) -> bool:
        return incoming.price >= maker.price if incoming.side == "BUY" else incoming.price <= maker.price

    i = 0
    while remaining > 1e-12 and i < len(resting):
        maker = resting[i]
        if maker.order_id.split(":")[0] == incoming.order_id.split(":")[0]:
            i += 1  # self-trade prevention: skip your own resting orders
            continue
        if not crosses(maker):
            break
        fill = min(remaining, maker.shares)
        matches.append(Match(incoming.order_id, maker.order_id, maker.price, fill))
        remaining -= fill
        maker.shares -= fill
        if maker.shares <= 1e-12:
            resting.pop(i)
        else:
            i += 1

    remainder = None
    if remaining > 1e-12:
        remainder = BookOrder(incoming.order_id, incoming.side, incoming.price, remaining, incoming.ts)
        (book.bids if incoming.side == "BUY" else book.asks).append(remainder)
        book._resort()
    return matches, remainder


def build_ladder(book: Book, depth: int = 10) -> dict:
    """Aggregate resting orders into price→size ladders for both sides."""
    def agg(side: list[BookOrder]) -> list[tuple[float, float]]:
        out: dict[float, float] = {}
        for o in side:
            out[o.price] = out.get(o.price, 0.0) + o.shares
        return sorted(out.items(), key=lambda kv: kv[0])[:depth]

    return {"bids": agg(book.bids)[::-1], "asks": agg(book.asks)}
