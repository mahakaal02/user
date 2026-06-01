"""
The simulation engine — the tick loop.

Each tick (mirrors the spec's 8 steps):
  1. fetch news for the tick (synthetic / stored / RSS),
  2. run the SHARED inference pipeline once → build the SignalLayer,
  3. publish that one signal layer + market view to every bot,
  4. each bot observes the identical signal layer,
  5. bots generate at most one order each,
  6. the market clears all orders (AMM, fair shuffled order),
  7. prices update from the fills,
  8. state is logged (incl. per-tick upstream inference-call count).

Inference is called O(headlines) times per tick, NOT O(bots) — the loop never
calls inference inside the per-bot iteration. That is the structural guarantee
behind "no per-bot model inference," and the recorder logs the count so it can
be verified after the fact.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..bots.base import BaseBot
from ..inference.base import InferenceClient
from ..market.gateway import MarketGateway
from ..news import NewsSource
from ..rng import RngHub
from ..signals import build_signal_layer
from .recorder import Recorder


@dataclass
class RunResult:
    prices: list[float]
    flows: list[float]
    bots: list[BaseBot]
    recorder: Recorder
    upstream_calls_total: int
    ticks: int
    resolved: str | None = None


class Engine:
    def __init__(
        self,
        inference: InferenceClient,
        market: MarketGateway,
        bots: list[BaseBot],
        news: NewsSource,
        rng_hub: RngHub,
        recorder: Recorder,
        ticks: int,
        resolve_outcome: str | None = None,
        progress_every: int = 0,
    ) -> None:
        self.inference = inference
        self.market = market
        self.bots = bots
        self.news = news
        self.rng_hub = rng_hub
        self.recorder = recorder
        self.ticks = ticks
        self.resolve_outcome = resolve_outcome
        self.progress_every = progress_every

    def run(self) -> RunResult:
        clear_rng = self.rng_hub.stream("clearing")
        bots_by_id = {b.id: b for b in self.bots}
        prev_upstream = 0

        for t in range(self.ticks):
            # (1) news  (2) shared inference → signal layer
            view = self.market.view(t)
            headlines = self.news.headlines(t)
            signal = build_signal_layer(t, headlines, self.inference, view.price_yes)

            # (3)(4) every bot observes the SAME signal layer + view
            for bot in self.bots:
                bot.observe(signal, view)

            # (5) decisions — one pass, no inference calls inside
            orders = []
            for bot in self.bots:
                order = bot.decide()
                if order is not None:
                    orders.append(order)

            # (6) clear  (7) price updates
            for order in orders:
                self.market.submit(order)
            fills = self.market.clear(clear_rng)
            for fill in fills:
                bot = bots_by_id.get(fill.bot_id)
                if bot is not None:
                    bot.apply_fill(fill)

            # (8) log
            post = self.market.view(t)
            upstream_total = self.inference.upstream_calls
            self.recorder.log_tick(
                tick=t,
                price_yes=post.price_yes,
                signal=signal,
                net_flow=post.last_net_flow,
                volume=post.last_volume,
                n_orders=len(orders),
                upstream_calls_total=upstream_total,
                upstream_calls_tick=upstream_total - prev_upstream,
            )
            prev_upstream = upstream_total

            if self.progress_every and (t + 1) % self.progress_every == 0:
                print(f"  tick {t + 1:>4}/{self.ticks}  price_yes={post.price_yes:.3f}  orders={len(orders)}")

        if self.resolve_outcome:
            self.market.resolve(self.resolve_outcome)

        return RunResult(
            prices=self.recorder.prices,
            flows=self.recorder.flows,
            bots=self.bots,
            recorder=self.recorder,
            upstream_calls_total=self.inference.upstream_calls,
            ticks=self.ticks,
            resolved=self.resolve_outcome,
        )
