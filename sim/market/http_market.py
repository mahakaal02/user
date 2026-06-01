"""
Optional bridge that drives the REAL ``bet`` Next.js market over HTTP instead of
the in-process AMM. Selected with ``market.mode: http`` in config — no engine or
bot changes. Use it to replay a simulated cohort against a running Kalki
Exchange instance (e.g. for a live demo or load test).

Endpoints (from the bet repo):
  * ``GET  /api/markets/{id}/state``  → current AMM state / price
  * ``POST /api/trade``               → market-buy YES/NO  (NextAuth-gated)
  * ``POST /api/orders``              → place CLOB limit order

AUTH NOTE: ``/api/trade`` is user-authenticated (NextAuth JWT cookie). Two clean
options, both config-only:
  (a) put a session cookie / bearer token in ``market.http.auth_header`` (each
      bot can map to one demo account, or all share one service account); or
  (b) add a thin internal trade route mirroring the existing
      ``/api/internal/wallet`` (Bearer INTERNAL_API_SECRET) pattern — see the
      README "Integration" section. Then point ``trade_path`` at it.

This adapter keeps the same submit/clear contract; it just flushes the queue as
HTTP calls on ``clear``. It is intentionally not used by the default offline
run, so the simulator never requires a live backend.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .gateway import MarketGateway
from .types import Fill, MarketView, Order


class HttpMarket(MarketGateway):
    def __init__(
        self,
        base_url: str,
        market_id: str,
        trade_path: str = "/api/trade",
        state_path: str = "/api/markets/{id}/state",
        auth_header: str | None = None,
        timeout_s: float = 10.0,
        history_len: int = 64,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.market_id = market_id
        self.trade_path = trade_path
        self.state_path = state_path
        self.auth_header = auth_header
        self.timeout_s = timeout_s
        self._queue: list[Order] = []
        self._history: list[float] = []
        self._history_len = history_len
        self._last_net_flow = 0.0
        self._last_volume = 0.0

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.auth_header:
            # e.g. "Cookie: next-auth.session-token=..." or "Authorization: Bearer ..."
            name, _, value = self.auth_header.partition(":")
            h[name.strip()] = value.strip()
        return h

    def _get(self, path: str) -> dict:
        url = self.base_url + path.replace("{id}", str(self.market_id))
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: dict) -> dict:
        url = self.base_url + path.replace("{id}", str(self.market_id))
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def view(self, tick: int) -> MarketView:
        state = self._get(self.state_path)
        # The bet state route returns priceYes + reserves; tolerate key variants.
        price = float(state.get("priceYes", state.get("price_yes", 0.5)))
        yes = float(state.get("yesShares", state.get("yes_shares", 0.0)))
        no = float(state.get("noShares", state.get("no_shares", 0.0)))
        self._history.append(price)
        self._history = self._history[-self._history_len :]
        return MarketView(
            tick=tick,
            price_yes=price,
            price_history=list(self._history),
            last_net_flow=self._last_net_flow,
            last_volume=self._last_volume,
            reserves=(yes, no),
        )

    def submit(self, order: Order) -> None:
        self._queue.append(order)

    def clear(self, rng) -> list[Fill]:
        orders = self._queue
        self._queue = []
        rng.shuffle(orders)
        fills: list[Fill] = []
        net_flow = 0.0
        volume = 0.0
        for o in orders:
            if o.side != "BUY":
                continue  # the public trade route is buy-only; sells go via /api/orders
            try:
                resp = self._post(
                    self.trade_path,
                    {"marketId": self.market_id, "outcome": o.outcome, "coins": o.coins},
                )
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
                continue
            shares = float(resp.get("sharesOut", resp.get("shares", 0.0)))
            avg = float(resp.get("avgPrice", resp.get("avg_price", 0.0)))
            price_after = float(resp.get("newYesPrice", resp.get("price_yes", 0.0)))
            fill = Fill(o.bot_id, "BUY", o.outcome, o.coins, shares, avg, price_after)
            fills.append(fill)
            volume += o.coins
            net_flow += o.coins if o.outcome == "YES" else -o.coins
        self._last_net_flow = net_flow
        self._last_volume = volume
        return fills
