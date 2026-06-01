"""
BaseBot — the shared machinery every personality inherits.

A bot NEVER touches the inference client, a model, or a URL. It only:
  * observes the shared :class:`SignalLayer` and the :class:`MarketView`, and
  * returns at most one :class:`Order` per tick.

Common machinery provided here:
  * internal bias        — a fixed per-bot prior in [-1, 1] (set from a seeded
                           RNG, so reproducible).
  * memory state         — a bounded deque of perceived directional signals.
  * reaction delay       — the bot acts on the signal from ``reaction_delay``
                           ticks ago, modelling slow vs. fast traders.
  * wallet + positions   — coins and YES/NO share inventory, updated by fills.
  * sizing               — conviction → order size, capped by aggressiveness and
                           available coins (so bulls naturally exhaust capital —
                           an endogenous source of crashes).

Subclasses implement only :meth:`intent`, returning a scalar in [-1, 1]:
positive = bullish (push YES up), negative = bearish. Magnitude = conviction.
"""
from __future__ import annotations

from collections import deque

from ..market.types import MarketView, Order
from ..signals import SignalLayer


class BaseBot:
    kind = "base"

    def __init__(
        self,
        bot_id: str,
        rng,
        coins: float = 1000.0,
        aggressiveness: float = 0.06,
        reaction_delay: int = 0,
        memory_len: int = 16,
        bias: float = 0.0,
        max_trade_fraction: float = 0.18,
        min_trade_coins: float = 2.0,
        buy_threshold: float = 0.10,
        sell_threshold: float = 0.30,
        trade_prob: float = 0.6,
        smooth: float = 0.45,
    ) -> None:
        self.id = bot_id
        self.rng = rng
        self.coins = coins
        self.yes_shares = 0.0
        self.no_shares = 0.0
        self.aggressiveness = aggressiveness
        self.reaction_delay = max(0, int(reaction_delay))
        self.bias = bias
        self.max_trade_fraction = max_trade_fraction
        self.min_trade_coins = min_trade_coins
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.trade_prob = trade_prob
        self.smooth = smooth

        self.memory: deque[float] = deque(maxlen=memory_len)
        self._signal_buffer: deque[SignalLayer] = deque(maxlen=self.reaction_delay + 1)
        self._view: MarketView | None = None
        self._intent_ema = 0.0  # smooths intent so trends build over many ticks

        # --- admin/runtime state (used by the Bot Admin Panel) ------------- #
        self.status = "active"           # active | paused | dead
        self.start_coins = coins         # for PnL + reset
        self.trade_count = 0
        self.volume_traded = 0.0         # gross coins transacted
        self.realized_pnl = 0.0          # closed-position P&L (cost-basis method)
        self.wins = 0
        self.losses = 0
        self._yes_cost = 0.0             # coins invested in current YES inventory
        self._no_cost = 0.0             # coins invested in current NO inventory
        self.last_action: str | None = None  # human-readable last decision, for the feed

    # -- observation ------------------------------------------------------- #
    def observe(self, signals: SignalLayer, view: MarketView) -> None:
        self._signal_buffer.append(signals)
        self._view = view
        self.memory.append(self.perceived_signal().directional)

    def perceived_signal(self) -> SignalLayer:
        """The signal this bot currently reacts to (delayed by reaction_delay)."""
        return self._signal_buffer[0]

    # -- decision ---------------------------------------------------------- #
    def intent(self, view: MarketView, signal: SignalLayer) -> float:
        """Subclass hook: directional conviction in [-1, 1]. Default: flat."""
        return 0.0

    def decide(self) -> Order | None:
        # Paused/dead bots observe (above) but never trade — this is what the
        # admin panel's pause/kill controls hook into.
        if self.status != "active":
            return None
        if self._view is None or not self._signal_buffer:
            return None
        view = self._view
        signal = self.perceived_signal()
        raw = max(-1.0, min(1.0, self.intent(view, signal) + self.bias))
        # Smooth the intent (EMA): a bot needs a *sustained* turn to flip sides,
        # so bubbles inflate and crashes unfold over many ticks instead of
        # thrashing every tick. `smooth` is higher for fast/news bots.
        self._intent_ema = (1.0 - self.smooth) * self._intent_ema + self.smooth * raw
        intent = self._intent_ema

        # Heterogeneous trading frequency — not every bot re-trades every tick.
        if self.rng.random() > self.trade_prob:
            return None

        # Reversal-driven selling — the engine of crashes. Trigger only on a
        # sustained opposing intent (the EMA), not a one-tick blip.
        if intent <= -self.sell_threshold and self.yes_shares > 1e-6:
            return self._sell("YES", -intent)
        if intent >= self.sell_threshold and self.no_shares > 1e-6:
            return self._sell("NO", intent)

        if intent >= self.buy_threshold:
            return self._buy("YES", intent)
        if intent <= -self.buy_threshold:
            return self._buy("NO", -intent)
        return None

    # -- order construction ------------------------------------------------ #
    def _buy(self, outcome: str, conviction: float) -> Order | None:
        conviction = max(0.0, min(1.0, conviction))
        budget = self.coins * self.aggressiveness * conviction
        budget = min(budget, self.coins * self.max_trade_fraction)
        if budget < self.min_trade_coins or self.coins < self.min_trade_coins:
            return None
        return Order(self.id, "BUY", outcome, coins=round(budget, 4))

    def _sell(self, outcome: str, conviction: float) -> Order | None:
        conviction = max(0.0, min(1.0, conviction))
        held = self.yes_shares if outcome == "YES" else self.no_shares
        qty = min(held, held * conviction * max(self.aggressiveness, 0.25) + held * 0.1)
        if qty < 1.0:
            return None
        return Order(self.id, "SELL", outcome, shares=round(qty, 4))

    # -- accounting (called by the engine) --------------------------------- #
    def apply_fill(self, fill) -> None:
        self.trade_count += 1
        self.volume_traded += fill.coins
        self.last_action = f"{fill.side} {fill.outcome} {fill.shares:.0f}@{fill.avg_price:.2f}"
        if fill.side == "BUY":
            self.coins -= fill.coins
            if fill.outcome == "YES":
                self.yes_shares += fill.shares
                self._yes_cost += fill.coins
            else:
                self.no_shares += fill.shares
                self._no_cost += fill.coins
        else:  # SELL — realize P&L against average cost basis of the position
            self.coins += fill.coins
            if fill.outcome == "YES":
                basis = self._yes_cost / max(self.yes_shares, 1e-9)
                cost_removed = basis * fill.shares
                self.yes_shares = max(0.0, self.yes_shares - fill.shares)
                self._yes_cost = max(0.0, self._yes_cost - cost_removed)
            else:
                basis = self._no_cost / max(self.no_shares, 1e-9)
                cost_removed = basis * fill.shares
                self.no_shares = max(0.0, self.no_shares - fill.shares)
                self._no_cost = max(0.0, self._no_cost - cost_removed)
            realized = fill.coins - cost_removed
            self.realized_pnl += realized
            if realized >= 0:
                self.wins += 1
            else:
                self.losses += 1

    def equity(self, price_yes: float) -> float:
        """Mark-to-market net worth: cash + inventory valued at current price."""
        return self.coins + self.yes_shares * price_yes + self.no_shares * (1.0 - price_yes)

    # -- admin controls (live mutation, all reproducible) ------------------ #
    @property
    def risk_level(self) -> str:
        a = self.aggressiveness
        return "low" if a < 0.06 else "medium" if a < 0.12 else "high"

    @property
    def avg_trade_size(self) -> float:
        return self.volume_traded / self.trade_count if self.trade_count else 0.0

    def set_reaction_delay(self, delay: int) -> None:
        """Resize the reaction-delay buffer live, preserving recent history."""
        delay = max(0, int(delay))
        self.reaction_delay = delay
        kept = list(self._signal_buffer)[-(delay + 1):]
        self._signal_buffer = deque(kept, maxlen=delay + 1)

    def set_params(self, **kw) -> None:
        """Live-edit admin-exposed parameters. Unknown keys are ignored."""
        if "reaction_delay" in kw and kw["reaction_delay"] is not None:
            self.set_reaction_delay(kw.pop("reaction_delay"))
        editable = {
            "aggressiveness", "bias", "trade_prob", "coins", "smooth",
            "buy_threshold", "sell_threshold", "max_trade_fraction",
        }
        for k, v in kw.items():
            if k in editable and v is not None:
                setattr(self, k, float(v))

    def reset_state(self) -> None:
        """Wipe positions/memory and restore the starting bankroll (sim only)."""
        self.coins = self.start_coins
        self.yes_shares = self.no_shares = 0.0
        self._yes_cost = self._no_cost = 0.0
        self.trade_count = self.wins = self.losses = 0
        self.volume_traded = self.realized_pnl = 0.0
        self._intent_ema = 0.0
        self.memory.clear()
        self._signal_buffer.clear()
        self.last_action = None
        self.status = "active"

    def snapshot(self, price_yes: float) -> dict:
        """Full state for the admin table / detail modal."""
        eq = self.equity(price_yes)
        return {
            "bot_id": self.id,
            "type": self.kind,
            "status": self.status,
            "bankroll": round(self.coins, 1),
            "equity": round(eq, 1),
            "pnl": round(eq - self.start_coins, 1),
            "pnl_pct": round((eq - self.start_coins) / max(self.start_coins, 1e-9) * 100, 1),
            "realized_pnl": round(self.realized_pnl, 1),
            "positions": {"yes": round(self.yes_shares, 1), "no": round(self.no_shares, 1)},
            "risk_level": self.risk_level,
            "trades": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "avg_trade_size": round(self.avg_trade_size, 1),
            "params": {
                "aggressiveness": round(self.aggressiveness, 4),
                "bias": round(self.bias, 4),
                "trade_prob": round(self.trade_prob, 3),
                "reaction_delay": self.reaction_delay,
                "smooth": round(self.smooth, 3),
            },
            "memory": [round(x, 4) for x in self.memory],
            "intent_ema": round(self._intent_ema, 4),
            "last_action": self.last_action,
        }
