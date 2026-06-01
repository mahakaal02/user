"""
HTTP client for the Kalki Exchange internal service API (stdlib urllib only).

All calls are Bearer-authenticated with INTERNAL_API_SECRET and target the
`/api/internal/*` routes added to the `bet` app. The base URL must include the
app's basePath (`/markets`) — e.g. http://localhost:3100/markets or
https://kalki.bet/markets.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class KalkiClient:
    def __init__(self, base_url: str, secret: str, timeout_s: float = 15.0) -> None:
        self.base = base_url.rstrip("/")
        self.secret = secret
        self.timeout = timeout_s

    def _req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except (ValueError, OSError):
                return e.code, {}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 0, {"error": "unreachable", "detail": str(e)}

    # -- reads ------------------------------------------------------------- #
    def list_markets(self) -> list[dict]:
        _, b = self._req("GET", "/api/internal/markets")
        return b.get("markets", [])

    def list_bot_users(self) -> list[dict]:
        _, b = self._req("GET", "/api/internal/bot-users")
        return b.get("users", [])

    # -- writes ------------------------------------------------------------ #
    def trade(self, user_id: str, market_id: str, side: str, outcome: str,
              coins: float | None = None, shares: float | None = None) -> tuple[int, dict]:
        body: dict = {"side": side, "userId": user_id, "marketId": market_id, "outcome": outcome}
        if side == "BUY":
            body["coins"] = int(coins or 0)
        else:
            body["shares"] = float(shares or 0)
        return self._req("POST", "/api/internal/trade", body)

    def comment(self, user_id: str, market: str, body_text: str, parent_id: str | None = None) -> tuple[int, dict]:
        body: dict = {"userId": user_id, "market": market, "body": body_text}
        if parent_id:
            body["parentId"] = parent_id
        return self._req("POST", "/api/internal/comment", body)
