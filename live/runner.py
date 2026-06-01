"""
Live fleet runner.

Maps each seeded bot user to a personality (deterministically by user id), then
every cycle:
  1. pulls the open-market list (auto-onboarding NEW markets),
  2. computes ONE shared inference signal per market from its title (not per
     bot — same "no per-bot inference" guarantee as the simulator),
  3. has a paced batch of (bot, market) pairs decide via the reused personality
     `intent()`, sizing trades against the bot's live balance,
  4. submits trades through the internal API, and
  5. posts an LLM/template one-liner on a fraction of trades.

The server is the source of truth for balances/positions; the runner keeps light
local estimates and lets the server reject anything stale (graceful).
"""
from __future__ import annotations

import hashlib
import time
from collections import deque

from sim.bots import REGISTRY
from sim.market.types import MarketView
from sim.signals import build_signal_layer

from .comment_gen import CommentGenerator
from .kalki_client import KalkiClient


def _weighted_pick(weights: dict[str, int], h: int) -> str:
    total = sum(weights.values())
    x = h % total
    acc = 0
    for k, w in weights.items():
        acc += w
        if x < acc:
            return k
    return next(iter(weights))


class LiveBot:
    def __init__(self, user_id, username, kind, rng, balance, fleet_cfg) -> None:
        self.user_id = user_id
        self.username = username
        self.kind = kind
        self.rng = rng
        self.balance = float(balance)
        self.aggressiveness = fleet_cfg["aggressiveness"]
        self.max_trade_coins = fleet_cfg["max_trade_coins"]
        self.min_trade_coins = fleet_cfg["min_trade_coins"]
        # One reusable personality instance; we call its pure intent() with a
        # per-market view. bias/reaction come from the personality's own jitter.
        self.persona = REGISTRY[kind](
            f"{kind}:{user_id}", rng,
            coins=balance, aggressiveness=self.aggressiveness,
            reaction_delay=fleet_cfg.get("reaction_delay", 1),
        )
        self.ema: dict[str, float] = {}            # per-market smoothed intent
        self.pos: dict[str, dict] = {}             # per-market {YES,NO} share estimate

    def intent_for(self, market_id: str, view: MarketView, signal) -> float:
        p = self.pos.get(market_id, {"YES": 0.0, "NO": 0.0})
        # Let position-aware personalities (overconfident) see their holding.
        self.persona.yes_shares, self.persona.no_shares = p["YES"], p["NO"]
        raw = max(-1.0, min(1.0, self.persona.intent(view, signal) + self.persona.bias))
        e = 0.5 * self.ema.get(market_id, 0.0) + 0.5 * raw
        self.ema[market_id] = e
        return e

    def size_coins(self, conviction: float) -> int:
        coins = self.balance * self.aggressiveness * conviction
        coins = min(coins, self.max_trade_coins, self.balance * 0.25)
        coins = max(coins, self.min_trade_coins)
        return int(coins)

    def apply_buy(self, market_id, outcome, shares, balance_after):
        self.balance = balance_after
        p = self.pos.setdefault(market_id, {"YES": 0.0, "NO": 0.0})
        p[outcome] += shares

    def apply_sell(self, market_id, outcome, shares, balance_after):
        self.balance = balance_after
        p = self.pos.setdefault(market_id, {"YES": 0.0, "NO": 0.0})
        p[outcome] = max(0.0, p[outcome] - shares)


class FleetRunner:
    def __init__(self, client: KalkiClient, comment_gen: CommentGenerator, inference, cfg, rng_hub, news_feed=None) -> None:
        self.client = client
        self.comment_gen = comment_gen
        self.inference = inference
        self.cfg = cfg
        self.rng_hub = rng_hub
        self.news_feed = news_feed                 # MarketNewsFeed or None (→ title-only)
        self.bots: list[LiveBot] = []
        self.bots_by_id: dict[str, LiveBot] = {}
        self.hist: dict[str, deque] = {}           # market_id -> price history
        self.signals: dict[str, object] = {}       # market_id -> shared SignalLayer
        self.titles: dict[str, str] = {}
        self._market_news: dict[str, list] = {}    # market_id -> real headlines (latest)
        self._seen_markets: set[str] = set()
        self.stats = {"trades": 0, "comments": 0, "rejects": 0, "cycles": 0, "news_markets": 0}

    # -- setup ------------------------------------------------------------- #
    def setup(self) -> None:
        users = self.client.list_bot_users()
        if not users:
            raise RuntimeError("no bot users found — run scripts/seed-bots.ts first")
        mix = self.cfg["fleet"]["mix"]
        for u in users:
            h = int(hashlib.blake2b(u["id"].encode(), digest_size=4).hexdigest(), 16)
            kind = _weighted_pick(mix, h)
            rng = self.rng_hub.stream(f"livebot:{u['id']}")
            bot = LiveBot(u["id"], u["username"], kind, rng, u["balance"], self.cfg["fleet"])
            self.bots.append(bot)
            self.bots_by_id[u["id"]] = bot
        kinds = {}
        for b in self.bots:
            kinds[b.kind] = kinds.get(b.kind, 0) + 1
        print(f"fleet: {len(self.bots)} bots — " + ", ".join(f"{k}:{v}" for k, v in sorted(kinds.items())))

    # -- market refresh ---------------------------------------------------- #
    def refresh_markets(self, cycle: int) -> list[dict]:
        markets = self.client.list_markets()
        refresh_every = self.cfg["pacing"].get("signal_refresh_cycles", 5)
        fetch_budget = self.cfg.get("news_feed", {}).get("max_fetch_per_cycle", 6)
        fetched = 0
        for m in markets:
            mid = m["id"]
            self.hist.setdefault(mid, deque(maxlen=64)).append(m["yesPrice"])
            self.titles[mid] = m["title"]
            if mid not in self._seen_markets:
                self._seen_markets.add(mid)
                print(f"  + onboarded market '{m['title'][:48]}' ({mid[:8]}) @ {m['yesPrice']:.3f}")
            if mid not in self.signals or cycle % refresh_every == 0:
                # Real-news pipeline: market title → Google News → BBC fallback →
                # relevance filter. Throttled by an HTTP budget per cycle; cached
                # with a TTL; falls back to the market title if news is empty.
                headlines = [m["title"]]
                if self.news_feed is not None:
                    gt = m.get("groupTitle")  # parent group context (e.g. FIFA World Cup)
                    needs = self.news_feed.needs_fetch(m["title"], m.get("category"), gt)
                    allow = needs and fetched < fetch_budget
                    real = self.news_feed.headlines_for(m["title"], m.get("category"), gt, allow_fetch=allow)
                    if needs and allow:
                        fetched += 1
                    if real:
                        if mid not in self._market_news:
                            self.stats["news_markets"] += 1
                        headlines = real
                        self._market_news[mid] = real
                # Shared per-market inference signal (computed once, used by all bots).
                self.signals[mid] = build_signal_layer(cycle, headlines, self.inference, m["yesPrice"])
        return markets

    def _view(self, m: dict, cycle: int) -> MarketView:
        return MarketView(
            tick=cycle,
            price_yes=m["yesPrice"],
            price_history=list(self.hist[m["id"]]),
            last_net_flow=0.0,
            last_volume=m.get("volumeCoins", 0.0),
            reserves=(m.get("yesShares", 0.0), m.get("noShares", 0.0)),
        )

    # -- one cycle --------------------------------------------------------- #
    def step(self, cycle: int, markets: list[dict]) -> None:
        pacing = self.cfg["pacing"]
        per_cycle = pacing["trades_per_cycle"]
        comment_rate = pacing["comment_rate"]
        trade_prob = self.cfg["fleet"]["trade_prob"]
        pick = self.rng_hub.stream("pick")

        for _ in range(per_cycle):
            bot = self.bots[pick.randrange(len(self.bots))]
            m = markets[pick.randrange(len(markets))]
            if pick.random() > trade_prob:
                continue
            view = self._view(m, cycle)
            signal = self.signals[m["id"]]
            intent = bot.intent_for(m["id"], view, signal)
            order = self._decide(bot, m["id"], intent)
            if order is None:
                continue
            self._execute(bot, m, order, comment_rate, pick)

    def _decide(self, bot: LiveBot, market_id: str, intent: float):
        pos = bot.pos.get(market_id, {"YES": 0.0, "NO": 0.0})
        if intent <= -0.30 and pos["YES"] > 1:
            return ("SELL", "YES", pos["YES"] * 0.5)
        if intent >= 0.30 and pos["NO"] > 1:
            return ("SELL", "NO", pos["NO"] * 0.5)
        if intent >= 0.10 and bot.balance > bot.min_trade_coins:
            return ("BUY", "YES", bot.size_coins(intent))
        if intent <= -0.10 and bot.balance > bot.min_trade_coins:
            return ("BUY", "NO", bot.size_coins(-intent))
        return None

    def _execute(self, bot, m, order, comment_rate, pick) -> None:
        side, outcome, amount = order
        if side == "BUY":
            status, resp = self.client.trade(bot.user_id, m["id"], "BUY", outcome, coins=amount)
            if status == 200:
                bot.apply_buy(m["id"], outcome, resp["trade"]["shares"], resp["balanceAfter"])
                self.stats["trades"] += 1
            else:
                self.stats["rejects"] += 1
                return
        else:
            status, resp = self.client.trade(bot.user_id, m["id"], "SELL", outcome, shares=round(amount, 4))
            if status == 200:
                bot.apply_sell(m["id"], outcome, amount, resp["balanceAfter"])
                self.stats["trades"] += 1
            else:
                self.stats["rejects"] += 1
                return
        # Comment on a fraction of successful trades.
        if pick.random() < comment_rate:
            conviction = min(1.0, abs(bot.ema.get(m["id"], 0.3)))
            text = self.comment_gen.generate(side, outcome, conviction, m["title"], bot.rng)
            cs, _ = self.client.comment(bot.user_id, m["id"], text)
            if cs == 200:
                self.stats["comments"] += 1

    # -- run --------------------------------------------------------------- #
    def run(self, cycles: int | None = None) -> None:
        self.setup()
        interval = self.cfg["pacing"]["cycle_interval_s"]
        cycle = 0
        try:
            while cycles is None or cycle < cycles:
                t0 = time.perf_counter()
                markets = self.refresh_markets(cycle)
                if not markets:
                    print("  (no open markets yet — waiting)")
                else:
                    self.step(cycle, markets)
                self.stats["cycles"] += 1
                if cycle % 5 == 0 or cycles is not None:
                    avg = sum(b.balance for b in self.bots) / len(self.bots)
                    print(f"cycle {cycle:>4} · markets={len(markets)} · news={self.stats['news_markets']} "
                          f"trades={self.stats['trades']} comments={self.stats['comments']} "
                          f"rejects={self.stats['rejects']} avg_bal={avg:,.0f}")
                cycle += 1
                time.sleep(max(0.0, interval - (time.perf_counter() - t0)))
        except KeyboardInterrupt:
            print("\nstopped.")
        print(f"done: {self.stats}")
