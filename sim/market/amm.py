"""
Constant-product AMM for binary outcomes — a faithful Python port of
``bet/lib/amm.ts`` (the Kalki Exchange market math).

Keeping this a 1:1 mirror of the TypeScript means the simulator's price
formation is identical to the real market, so behavioural findings transfer and
the optional HTTP bridge (``http_market.py``) stays consistent with the sim.

    priceYes = noShares / (yesShares + noShares)

Split-coin buy: C coins (minus 1% fee → c) mint c YES + c NO; the c of the
opposite side joins the pool, which trades back shares to preserve k = yes*no.
The buyer keeps  c + poolTransfer  shares. See the TS file's header for the full
derivation; parity is asserted in ``tests/test_amm_parity.py``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

FEE_BPS = 100  # 1% fee, matches lib/amm.ts


@dataclass
class Reserves:
    yes_shares: float
    no_shares: float


@dataclass
class BuyQuote:
    shares_out: float
    avg_price: float
    new_yes_price: float
    new_reserves: Reserves


@dataclass
class SellQuote:
    coins_out: float
    avg_price: float
    new_yes_price: float
    new_reserves: Reserves


def price_yes(r: Reserves) -> float:
    denom = r.yes_shares + r.no_shares
    if denom <= 0:
        return 0.5
    return r.no_shares / denom


def price_no(r: Reserves) -> float:
    return 1.0 - price_yes(r)


def quote_buy(reserves: Reserves, outcome: str, coins: float) -> BuyQuote | None:
    """Port of ``quoteBuy``. ``outcome`` is "YES" or "NO". Returns None if the
    trade is invalid or would breach the slippage guards."""
    if not math.isfinite(coins) or coins <= 0:
        return None
    fee = (coins * FEE_BPS) / 10_000
    c = coins - fee
    if c <= 0:
        return None
    k = reserves.yes_shares * reserves.no_shares
    if k <= 0:
        return None

    if outcome == "YES":
        new_no = reserves.no_shares + c
        new_yes = k / new_no
        pool_transfer = reserves.yes_shares - new_yes
    else:
        new_yes = reserves.yes_shares + c
        new_no = k / new_yes
        pool_transfer = reserves.no_shares - new_no

    shares_out = c + pool_transfer
    if not math.isfinite(shares_out) or shares_out <= 0:
        return None
    if new_yes < 1 or new_no < 1:
        return None
    if pool_transfer < 0:
        return None

    new_reserves = Reserves(new_yes, new_no)
    avg_price = coins / shares_out
    if avg_price > 1 + 1e-9 or avg_price <= 0:
        return None
    return BuyQuote(shares_out, avg_price, price_yes(new_reserves), new_reserves)


def quote_sell(reserves: Reserves, outcome: str, shares: float) -> SellQuote | None:
    """Port of ``quoteSell``. Sells ``shares`` of ``outcome`` back to the AMM."""
    if not math.isfinite(shares) or shares <= 0:
        return None
    k = reserves.yes_shares * reserves.no_shares
    if k <= 0:
        return None

    if outcome == "YES":
        Y, N = reserves.yes_shares, reserves.no_shares
    else:
        Y, N = reserves.no_shares, reserves.yes_shares
    Q = shares

    b = Y + Q + N
    disc = b * b - 4 * Q * N
    if disc < 0:
        return None
    c = (b - math.sqrt(disc)) / 2
    if not math.isfinite(c) or c <= 0 or c >= N:
        return None

    fee = (c * FEE_BPS) / 10_000
    coins_out = c - fee
    if coins_out <= 0:
        return None

    new_y = Y + Q - c
    new_n = N - c
    if new_y < 1 or new_n < 1:
        return None
    if new_n < N * 0.1:  # slippage guard from the TS impl
        return None

    new_reserves = Reserves(new_y, new_n) if outcome == "YES" else Reserves(new_n, new_y)
    avg_price = coins_out / Q
    marginal_before = (
        reserves.no_shares / (reserves.yes_shares + reserves.no_shares)
        if outcome == "YES"
        else reserves.yes_shares / (reserves.yes_shares + reserves.no_shares)
    )
    if avg_price <= 0 or avg_price > marginal_before + 1e-9:
        return None
    return SellQuote(coins_out, avg_price, price_yes(new_reserves), new_reserves)
