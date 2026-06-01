"""AMM parity: the Python port must reproduce the numbers in bet/bet/README.md
('Pool at 50/50, buying 1000 YES → ~1487 shares, avg ~0.67, marginal after ~0.80')
and the symmetric sell. Run: python tests/test_amm_parity.py"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.market import amm  # noqa: E402


def test_buy_1000_at_5050_matches_readme():
    q = amm.quote_buy(amm.Reserves(1000.0, 1000.0), "YES", 1000.0)
    assert q is not None
    assert 1486.0 <= q.shares_out <= 1489.0, q.shares_out
    assert 0.66 <= q.avg_price <= 0.68, q.avg_price
    assert 0.79 <= q.new_yes_price <= 0.81, q.new_yes_price


def test_sell_roundtrip_is_lossy_but_bounded():
    # Selling pushes price down; avg received < marginal-before (0.50).
    q = amm.quote_sell(amm.Reserves(1000.0, 1000.0), "YES", 100.0)
    assert q is not None
    assert 0.0 < q.avg_price < 0.50, q.avg_price
    assert q.new_yes_price < 0.50  # selling YES lowers the YES price


def test_price_is_relative_scarcity():
    assert abs(amm.price_yes(amm.Reserves(1000.0, 1000.0)) - 0.5) < 1e-9
    assert amm.price_yes(amm.Reserves(500.0, 1500.0)) == 0.75  # noShares/(total)


def test_slippage_guards_reject_pathological_trades():
    # A buy so large it would drain a reserve below epsilon returns None.
    assert amm.quote_buy(amm.Reserves(1000.0, 1000.0), "YES", 10_000_000.0) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("AMM parity: all passed")
