"""
Public-API bot fleet — bots that are REAL registered users.

Instead of seeding users directly in the database and trading via the
Bearer-secret `/api/internal/*` routes, this path has each bot behave like an
ordinary human:

    register  → POST /api/auth/register            (creates the account)
    login     → NextAuth credentials sign-in       (gets a session cookie)
    markets   → GET  /api/markets/trending          (public, no auth)
    trade     → POST /api/trade                      (as the logged-in user)
    comment   → POST /api/markets/{id}/comments      (as the logged-in user)

So the fleet needs **neither the prod database nor the INTERNAL_API_SECRET** —
just the public site. Each bot keeps its own cookie jar (its own session).

Accounts are persisted locally (``runs/kalki_accounts.json``, gitignored — it
holds passwords) so re-runs reuse the same bots instead of registering new ones.

Server-side throttles shape provisioning: register is 3/min/IP and login is
8/min/IP (NestJS backend), so the fleet is brought up slowly and is best kept
modest. Trading is rate-limited 10/10s per user; a 429 is treated as a soft
reject by the runner.

:class:`PublicExchangeFacade` exposes the SAME methods the internal
``KalkiClient`` does (``list_markets`` / ``list_bot_users`` / ``trade`` /
``comment``), so :class:`live.runner.FleetRunner` drives it unchanged.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

_UA = "KalkiSimFleet/1.0 (+research-sandbox)"
_BOT_EMAIL_DOMAIN = "@sim.kalki.local"   # identifiable + matches /api/internal/bot-users


# --------------------------------------------------------------------------- #
#  Per-bot authenticated client (its own NextAuth session).
# --------------------------------------------------------------------------- #
class PublicBotClient:
    def __init__(self, base_url: str, email: str, username: str, password: str,
                 timeout_s: float = 20.0, ua: str = _UA) -> None:
        self.base = base_url.rstrip("/")
        self.email = email
        self.username = username
        self.password = password
        self.timeout = timeout_s
        self.ua = ua
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.user_id = email          # local handle the facade/runner key on
        self.balance = 0.0
        self.logged_in = False

    # NOTE: deliberately send NO Origin header — the register route's
    # same-origin guard allows requests that omit Origin (server-to-server).
    def _req(self, method: str, path: str, *, data=None, form: bool = False) -> tuple[int, object]:
        headers = {"User-Agent": self.ua, "Accept": "application/json"}
        body = None
        if data is not None:
            if form:
                body = urllib.parse.urlencode(data).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw or "{}")
                except ValueError:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw or "{}")
            except ValueError:
                return e.code, raw
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 0, {"error": "unreachable", "detail": str(e)[:160]}

    # -- provisioning ------------------------------------------------------ #
    def register(self) -> tuple[int, object]:
        return self._req("POST", "/api/auth/register",
                         data={"email": self.email, "password": self.password, "username": self.username})

    def login(self) -> bool:
        """NextAuth credentials sign-in → session cookie in this client's jar.
        Verified by a follow-up /api/me (the callback returns 200 even on bad
        creds, so we trust the session, not the callback status)."""
        s, body = self._req("GET", "/api/auth/csrf")
        csrf = body.get("csrfToken") if isinstance(body, dict) else None
        if not csrf:
            return False
        self._req("POST", "/api/auth/callback/credentials",
                  data={"csrfToken": csrf, "email": self.email, "password": self.password,
                        "json": "true", "callbackUrl": self.base}, form=True)
        s, me = self._req("GET", "/api/me")
        if s == 200 and isinstance(me, dict) and isinstance(me.get("user"), dict) and me["user"].get("id"):
            self.balance = float(me.get("wallet", {}).get("balance", 0) or 0)
            self.logged_in = True
            return True
        return False

    def refresh_balance(self) -> float:
        s, me = self._req("GET", "/api/me")
        if s == 200 and isinstance(me, dict):
            self.balance = float(me.get("wallet", {}).get("balance", self.balance) or self.balance)
        return self.balance

    # -- actions ----------------------------------------------------------- #
    def trade(self, market_id: str, side: str, outcome: str,
              coins: float | None = None, shares: float | None = None) -> tuple[int, object]:
        if side == "BUY":
            payload = {"side": "BUY", "marketId": market_id, "outcome": outcome, "coins": int(coins or 0)}
        else:
            payload = {"side": "SELL", "marketId": market_id, "outcome": outcome, "shares": float(shares or 0)}
        return self._req("POST", "/api/trade", data=payload)

    def comment(self, market_id: str, body_text: str) -> tuple[int, object]:
        return self._req("POST", f"/api/markets/{market_id}/comments", data={"body": body_text})


# --------------------------------------------------------------------------- #
#  Runner-compatible facade over a set of logged-in bots.
# --------------------------------------------------------------------------- #
class PublicExchangeFacade:
    """Mimics ``live.kalki_client.KalkiClient`` so ``FleetRunner`` is unchanged.

    Markets come from the PUBLIC trending endpoint (no auth). Trades/comments are
    routed to the per-bot session keyed by ``user_id`` (here, the bot's email).
    The public ``/api/trade`` response has no ``balanceAfter``, so we synthesise
    one from the trade ``cost``/``coinsReceived`` for the runner's local sizing
    (the server stays the source of truth and rejects overdrafts)."""

    def __init__(self, base_url: str, bots: list[PublicBotClient], timeout_s: float = 20.0,
                 market_limit: int = 20, ua: str = _UA) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout_s
        self.market_limit = market_limit
        self.ua = ua
        self._bots = bots
        self._by_id = {b.user_id: b for b in bots}

    # -- reads ------------------------------------------------------------- #
    def list_markets(self) -> list[dict]:
        """Public top-trending OPEN markets, normalized to the runner's shape.
        Reserves are reconstructed from price + liquidity (priceYes =
        noShares / (yesShares + noShares))."""
        req = urllib.request.Request(
            f"{self.base}/api/markets/trending?limit={self.market_limit}",
            headers={"User-Agent": self.ua, "Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read() or b"{}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
            return []
        out = []
        for m in body.get("markets", []):
            yes = max(0.0, min(1.0, m.get("yesCents", 50) / 100.0))
            total = float(m.get("liquidityCoins", 0) or 0)
            out.append({
                "id": m["id"], "slug": m.get("slug"), "title": m.get("title", ""),
                "category": None, "groupTitle": None,
                "yesPrice": yes,
                "yesShares": (1.0 - yes) * total,   # priceYes = noShares/total
                "noShares": yes * total,
                "volumeCoins": float(m.get("volumeCoins", 0) or 0),
                "endsAt": m.get("endsAt"),
            })
        return out

    def list_bot_users(self) -> list[dict]:
        return [{"id": b.user_id, "username": b.username, "balance": b.balance} for b in self._bots]

    # -- writes ------------------------------------------------------------ #
    def trade(self, user_id: str, market_id: str, side: str, outcome: str,
              coins: float | None = None, shares: float | None = None) -> tuple[int, dict]:
        bot = self._by_id.get(user_id)
        if bot is None:
            return 0, {"error": "no such bot"}
        status, body = bot.trade(market_id, side, outcome, coins=coins, shares=shares)
        if status == 200 and isinstance(body, dict):
            tr = body.get("trade", {}) or {}
            if side == "BUY":
                bot.balance = max(0.0, bot.balance - float(tr.get("cost", coins or 0) or 0))
            else:
                bot.balance = bot.balance + float(tr.get("coinsReceived", 0) or 0)
            # Adapt to the shape FleetRunner expects (balanceAfter + trade.shares).
            return status, {**body, "balanceAfter": bot.balance,
                            "trade": {**tr, "shares": float(tr.get("shares", 0) or 0)}}
        return status, body if isinstance(body, dict) else {"error": str(body)[:120]}

    def comment(self, user_id: str, market: str, body_text: str,
                parent_id: str | None = None) -> tuple[int, dict]:
        bot = self._by_id.get(user_id)
        if bot is None:
            return 0, {"error": "no such bot"}
        status, body = bot.comment(market, body_text)
        return status, body if isinstance(body, dict) else {"ok": status == 200}


# --------------------------------------------------------------------------- #
#  Local account store + provisioning (register + login, throttle-aware).
# --------------------------------------------------------------------------- #
def _load_accounts(path: str) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_accounts(path: str, accounts: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(accounts, fh, indent=2)


def _new_account(rng: random.Random) -> dict:
    n = rng.randint(100_000, 999_999)
    # username regex: ^[a-zA-Z0-9_]{3,20}$  ("simbot_123456" = 13 chars)
    return {"email": f"simbot_{n}{_BOT_EMAIL_DOMAIN}",
            "username": f"simbot_{n}",
            "password": f"Sandbox_{n}!"}


def provision_fleet(base_url: str, count: int, store_path: str, *, timeout_s: float = 20.0,
                    register_gap_s: float = 20.0, login_gap_s: float = 8.0,
                    log=print) -> list[PublicBotClient]:
    """Ensure ``count`` registered+logged-in bots exist, reusing the local store.

    Registration is paced to the server's 3/min throttle (``register_gap_s``) and
    login to 8/min (``login_gap_s``), both with 429 backoff. Returns the list of
    logged-in :class:`PublicBotClient`. Re-runs skip registration (accounts are
    already in the store) and only log in."""
    rng = random.Random()
    accounts = _load_accounts(store_path)

    # 1) Register new accounts until the store holds `count`.
    new_registered = 0
    attempts = 0
    while len(accounts) < count and attempts < count * 6:
        attempts += 1
        acct = _new_account(rng)
        c = PublicBotClient(base_url, acct["email"], acct["username"], acct["password"], timeout_s=timeout_s)
        st, body = c.register()
        if st == 200 or (st == 409):  # created, or (rarely) the random handle already exists
            if st == 200:
                accounts.append(acct)
                _save_accounts(store_path, accounts)
                new_registered += 1
                log(f"  registered {acct['username']} ({len(accounts)}/{count})")
                time.sleep(register_gap_s)   # respect 3/min register throttle
            continue
        if st == 429:
            log("  register throttled (429) — backing off 25s")
            time.sleep(25.0)
            continue
        if st in (502, 0):
            log(f"  register backend unreachable ({st} {body}) — aborting provisioning")
            break
        log(f"  register failed ({st} {body}) for {acct['username']} — skipping")
    if new_registered:
        log(f"  registered {new_registered} new bot(s); store has {len(accounts)}")

    # 2) Log every account in (each gets its own session). Pace to 8/min.
    bots: list[PublicBotClient] = []
    for i, acct in enumerate(accounts[:count]):
        c = PublicBotClient(base_url, acct["email"], acct["username"], acct["password"], timeout_s=timeout_s)
        ok = False
        for attempt in range(3):
            ok = c.login()
            if ok:
                break
            time.sleep(login_gap_s * (attempt + 1))   # 429/backoff
        if ok:
            bots.append(c)
            log(f"  logged in {c.username} · balance {c.balance:,.0f}")
        else:
            log(f"  login FAILED for {acct['username']} — skipping")
        if i < len(accounts[:count]) - 1:
            time.sleep(login_gap_s)                    # respect 8/min login throttle
    return bots
