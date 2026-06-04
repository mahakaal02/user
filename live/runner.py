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
import os
import threading
import time
from collections import deque

from sim.bots import REGISTRY
from sim.market.types import MarketView
from sim.signals import build_signal_layer

from .comment_gen import CommentGenerator
from .kalki_client import KalkiClient

# --------------------------------------------------------------------------- #
#  Runtime safety guardrails (audit fixes). These ONLY veto / pause / stop —
#  they never change the trade decision math (intent / _decide / size_coins).
# --------------------------------------------------------------------------- #
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KILL_FILE = os.path.join(_REPO_ROOT, "runs", "BOT_KILL_SWITCH")


class _CircuitBreakerStop(Exception):
    """Raised to stop the runner after the breaker re-trips following its pause."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


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
        self.start_balance = float(balance)          # stable capital base for the exposure cap
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
        self.invested: dict[str, float] = {}       # net coins committed per market (exposure cap)
        self._submits: deque = deque()             # monotonic submit times (per-minute rate limit)

    # -- guardrail bookkeeping (no trading logic) -------------------------- #
    def trades_in_last_minute(self, now: float) -> int:
        while self._submits and now - self._submits[0] > 60.0:
            self._submits.popleft()
        return len(self._submits)

    def record_submit(self, now: float) -> None:
        self._submits.append(now)

    def would_exceed_exposure(self, market_id: str, add_coins: float, cap: float) -> bool:
        committed = self.invested.get(market_id, 0.0) + max(0.0, add_coins)
        return committed / max(self.start_balance, 1.0) > cap

    def add_invested(self, market_id: str, coins: float) -> None:
        self.invested[market_id] = max(0.0, self.invested.get(market_id, 0.0) + coins)

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
        self.slugs: dict[str, str] = {}            # market_id -> slug (for admin-panel URLs)
        self._market_news: dict[str, list] = {}    # market_id -> real headlines (latest)
        self._seen_markets: set[str] = set()
        self.stats = {"trades": 0, "comments": 0, "rejects": 0, "cycles": 0, "news_markets": 0}
        # --- runtime safety guardrails (env-configurable; guardrails only) ---
        self.tps_limit = _env_int("BOT_TPS_LIMIT", 10)                    # max trades/bot/min
        self.max_exposure = _env_float("BOT_MAX_EXPOSURE_PER_MARKET", 0.5)
        self.confidence_floor = _env_float("BOT_CONFIDENCE_FLOOR", 0.55)
        self.breaker_threshold = _env_int("BOT_BREAKER_THRESHOLD", 5)
        self.breaker_pause_s = _env_float("BOT_BREAKER_PAUSE_S", 30.0)
        self.kill_file = KILL_FILE
        self._consec_fail = 0
        self._breaker_armed = False
        # --- operator-injected markets (admin panel) — force-included in the
        #     active set so bots trade them alongside the auto-discovered ones.
        #     Thread-safe: the admin server writes from another thread.
        self._injected: dict[str, dict] = {}
        self._inject_lock = threading.Lock()
        # --- crowd-simulation hooks (selection + logging only; default off) ---
        self.attention = None      # live.crowd.AttentionModel | None — biases market SELECTION
        self.sim_logger = None     # live.sim_logger.SimLogger | None — instrumentation

    # -- operator market injection (admin panel; additive, no trade-logic change)
    def inject_market(self, market: dict) -> bool:
        """Force-include a market in the active set. ``market`` is a normalized
        dict (id/title/yesPrice/…) resolved by the admin panel. Returns False if
        it's already active. Bots pick it up on the next cycle like any other."""
        mid = market.get("id")
        if not mid:
            return False
        with self._inject_lock:
            new = mid not in self._injected and mid not in self._seen_markets
            self._injected[mid] = market
        return new

    def _merge_injected(self, markets: list[dict]) -> list[dict]:
        with self._inject_lock:
            known = {m["id"] for m in markets}
            extra = [m for mid, m in self._injected.items() if mid not in known]
        return markets + extra

    def active_markets(self) -> list[dict]:
        """Read-only snapshot for the admin panel: which markets the fleet is on
        (slug included so the panel can build correct market URLs)."""
        with self._inject_lock:
            injected = dict(self._injected)
        out: list[dict] = []
        seen_ids: set[str] = set()
        for mid in list(self._seen_markets):
            hist = self.hist.get(mid)
            out.append({
                "id": mid,
                "slug": self.slugs.get(mid, mid),
                "title": self.titles.get(mid, ""),
                "yesPrice": (round(hist[-1], 4) if hist else None),
                "injected": mid in injected,
            })
            seen_ids.add(mid)
        # Surface a just-injected market immediately (before its first cycle).
        for mid, m in injected.items():
            if mid not in seen_ids:
                out.append({"id": mid, "slug": m.get("slug", mid),
                            "title": m.get("title", ""), "yesPrice": m.get("yesPrice"),
                            "injected": True})
        return out

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
        markets = self._merge_injected(self.client.list_markets())   # + operator-added markets
        refresh_every = self.cfg["pacing"].get("signal_refresh_cycles", 5)
        fetch_budget = self.cfg.get("news_feed", {}).get("max_fetch_per_cycle", 6)
        fetched = 0
        for m in markets:
            mid = m["id"]
            self.hist.setdefault(mid, deque(maxlen=64)).append(m["yesPrice"])
            self.titles[mid] = m["title"]
            if m.get("slug"):
                self.slugs[mid] = m["slug"]
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

        # crowd-sim: compute this cycle's attention weights once (selection only)
        if self.attention is not None:
            self.attention.begin_cycle(cycle, markets, self.signals, self.hist, pick)

        for _ in range(per_cycle):
            if self._kill_engaged():           # near-immediate stop, always between trades
                break
            bot = self.bots[pick.randrange(len(self.bots))]
            # market SELECTION: attention-weighted in crowd-sim, else uniform (unchanged)
            m = self.attention.pick_market(pick) if self.attention is not None \
                else markets[pick.randrange(len(markets))]
            if pick.random() > trade_prob:
                continue
            signal = self.signals[m["id"]]
            # guardrail: confidence floor — never trade on a low-confidence signal
            if getattr(signal, "confidence", 0.0) < self.confidence_floor:
                continue
            view = self._view(m, cycle)
            intent = bot.intent_for(m["id"], view, signal)
            order = self._decide(bot, m["id"], intent)
            if order is None:
                continue
            if self.sim_logger is not None:    # instrument the decided action (tagged)
                self.sim_logger.log_decision(cycle, bot, m, order, signal)
            self._execute(bot, m, order, comment_rate, pick)

        if self.attention is not None and self.sim_logger is not None:
            self.sim_logger.log_cycle(cycle, self.attention, self.signals)

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
        now = time.monotonic()
        # --- guardrail: per-bot rate limit (max trades/bot/min) — reject, no retry
        if bot.trades_in_last_minute(now) >= self.tps_limit:
            self.stats["rejects"] += 1
            return
        # --- guardrail: max exposure per market (applies to additional BUYs)
        if side == "BUY" and bot.would_exceed_exposure(m["id"], amount, self.max_exposure):
            self.stats["rejects"] += 1
            return
        bot.record_submit(now)  # this submission counts toward the per-minute rate

        if side == "BUY":
            status, resp = self.client.trade(bot.user_id, m["id"], "BUY", outcome, coins=amount)
            self._api_result(status == 200)
            if status != 200:
                self.stats["rejects"] += 1
                return
            trade = resp.get("trade") or {}
            shares, bal = trade.get("shares"), resp.get("balanceAfter")
            if not isinstance(shares, (int, float)) or not isinstance(bal, (int, float)):
                self.stats["rejects"] += 1            # malformed 200 → reject, never crash
                return
            bot.apply_buy(m["id"], outcome, shares, bal)
            bot.add_invested(m["id"], float(trade.get("cost", amount) or amount))
            self.stats["trades"] += 1
        else:
            status, resp = self.client.trade(bot.user_id, m["id"], "SELL", outcome, shares=round(amount, 4))
            self._api_result(status == 200)
            if status != 200:
                self.stats["rejects"] += 1
                return
            bal = resp.get("balanceAfter")
            if not isinstance(bal, (int, float)):
                self.stats["rejects"] += 1
                return
            bot.apply_sell(m["id"], outcome, amount, bal)
            bot.add_invested(m["id"], -float((resp.get("trade") or {}).get("coinsReceived", 0) or 0))
            self.stats["trades"] += 1

        # Comment on a fraction of successful trades.
        if pick.random() < comment_rate:
            conviction = min(1.0, abs(bot.ema.get(m["id"], 0.3)))
            text = self.comment_gen.generate(side, outcome, conviction, m["title"], bot.rng)
            cs, _ = self.client.comment(bot.user_id, m["id"], text)
            self._api_result(cs == 200)
            if cs == 200:
                self.stats["comments"] += 1

    # -- guardrail helpers ------------------------------------------------- #
    def _kill_engaged(self) -> bool:
        """Global kill switch: env BOT_KILL_SWITCH=1 or a runtime kill file.
        (The file makes it usable against an already-running process.)"""
        return os.environ.get("BOT_KILL_SWITCH") == "1" or os.path.exists(self.kill_file)

    def _api_result(self, ok: bool) -> None:
        """Circuit breaker over internal-API call outcomes: count consecutive
        failures; at the threshold pause ALL bots once, then stop the runner if
        failures continue after the pause. Any success resets it."""
        if ok:
            self._consec_fail = 0
            self._breaker_armed = False
            return
        self._consec_fail += 1
        if self._consec_fail >= self.breaker_threshold:
            if not self._breaker_armed:
                print(f"  ⚠ circuit breaker: {self._consec_fail} consecutive API failures — "
                      f"pausing all bots {self.breaker_pause_s:.0f}s")
                time.sleep(self.breaker_pause_s)
                self._breaker_armed = True
                self._consec_fail = 0
            else:
                print("  ⚠ circuit breaker: failures continued after pause — stopping runner")
                raise _CircuitBreakerStop()

    # -- run --------------------------------------------------------------- #
    def run(self, cycles: int | None = None) -> None:
        self.setup()
        print(f"  guardrails: kill=env:BOT_KILL_SWITCH=1|file:{os.path.relpath(self.kill_file, _REPO_ROOT)} · "
              f"rate={self.tps_limit}/bot/min · max_exposure={self.max_exposure:.0%}/market · "
              f"confidence_floor={self.confidence_floor:.2f} · breaker={self.breaker_threshold}fail/{self.breaker_pause_s:.0f}s")
        interval = self.cfg["pacing"]["cycle_interval_s"]
        cycle = 0
        try:
            while cycles is None or cycle < cycles:
                # --- global kill switch: checked at the top of EVERY cycle ---
                if self._kill_engaged():
                    print("  ⛔ kill switch engaged — stopping cleanly before next cycle (no partial trades)")
                    break
                t0 = time.perf_counter()
                try:
                    markets = self.refresh_markets(cycle)
                    if not markets:
                        print("  (no open markets yet — waiting)")
                    else:
                        self.step(cycle, markets)
                except _CircuitBreakerStop:
                    break          # breaker escalated to stop — clean shutdown
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
