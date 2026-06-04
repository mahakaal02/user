"""
Dry-run exchange client wrapper (Step 5).

Wraps the real internal/public exchange client. READ calls (``list_markets`` /
``list_bot_users``) pass straight through, so the pipeline still loads markets +
bots, generates signals, decides, and runs every safety check. The two WRITE
calls (``trade`` / ``comment``) are intercepted: NOTHING is sent to the exchange
— each is logged as ``DRY RUN …`` and a synthetic 200 is returned so the runner's
normal metrics + bot bookkeeping still update exactly as in a live run.

This adds dry-run WITHOUT touching trading logic, decisions, safety systems, or
the real API client — it only sits in front of the two write methods.
"""
from __future__ import annotations


class DryRunClient:
    """Transparent proxy that suppresses (only) the exchange WRITE calls."""

    name = "dry_run"

    def __init__(self, inner) -> None:
        self._inner = inner
        self._bal: dict[str, float] = {}     # synthetic balances (seeded from bot-users)
        self._price: dict[str, float] = {}   # market_id -> yesPrice (seeded from markets)
        self.dry_trades = 0
        self.dry_comments = 0

    # -- reads: pass through (and cache prices/balances for realistic logs) -- #
    def list_bot_users(self) -> list[dict]:
        users = self._inner.list_bot_users()
        for u in users:
            self._bal.setdefault(u["id"], float(u.get("balance", 0) or 0))
        return users

    def list_markets(self) -> list[dict]:
        markets = self._inner.list_markets()
        for m in markets:
            try:
                self._price[m["id"]] = float(m.get("yesPrice", 0.5) or 0.5)
            except (TypeError, ValueError):
                self._price[m["id"]] = 0.5
        return markets

    # -- writes: intercepted — NO network, log + synthetic 200 -------------- #
    def trade(self, user_id, market_id, side, outcome, coins=None, shares=None):
        yes = self._price.get(market_id, 0.5)
        px = max(1e-6, yes if outcome == "YES" else (1.0 - yes))
        bal = self._bal.get(user_id, 0.0)
        if side == "BUY":
            spent = int(coins or 0)
            sh = spent / px
            bal_after = max(0.0, bal - spent)
            self._bal[user_id] = bal_after
            self.dry_trades += 1
            print(f"  DRY RUN BUY  {user_id[:10]} {outcome} {spent}c → ~{sh:.1f} sh @ {px:.3f}  market={market_id[:8]}")
            return 200, {"ok": True, "trade": {"shares": round(sh, 4), "cost": spent}, "balanceAfter": round(bal_after, 2)}
        sh = float(shares or 0)
        proceeds = sh * px
        bal_after = bal + proceeds
        self._bal[user_id] = bal_after
        self.dry_trades += 1
        print(f"  DRY RUN SELL {user_id[:10]} {outcome} {sh:.2f} sh → ~{proceeds:.0f}c @ {px:.3f}  market={market_id[:8]}")
        return 200, {"ok": True, "trade": {"shares": sh, "coinsReceived": round(proceeds, 2)}, "balanceAfter": round(bal_after, 2)}

    def comment(self, user_id, market, body_text, parent_id=None):
        self.dry_comments += 1
        print(f"  DRY RUN COMMENT {user_id[:10]} market={str(market)[:8]}: {str(body_text)[:60]}")
        return 200, {"ok": True, "id": "dry-run"}

    # -- anything else (e.g. _req used by startup validation) → real client - #
    def __getattr__(self, name):
        return getattr(self._inner, name)
