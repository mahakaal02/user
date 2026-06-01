"""Herd trader — ignores fundamentals and follows the crowd: it trades in the
direction of last tick's net order flow (and the recent price move). Pure social
proof. With enough herd bots, small imbalances self-reinforce into stampedes —
the direct generator of herd behaviour and the runaway phase of a bubble/crash."""
from __future__ import annotations

from ..market.types import MarketView
from ..signals import SignalLayer
from .base import BaseBot


class HerdBot(BaseBot):
    kind = "herd"

    def __init__(self, *args, flow_gain: float = 0.0006, trend_gain: float = 3.5, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.flow_gain = flow_gain
        self.trend_gain = trend_gain

    def intent(self, view: MarketView, signal: SignalLayer) -> float:
        # Follow where the money went last tick + the recent price drift.
        flow = view.last_net_flow * self.flow_gain
        trend = view.last_return * self.trend_gain
        return max(-1.0, min(1.0, flow + trend))
