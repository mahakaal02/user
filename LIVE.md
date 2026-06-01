# Live Kalki Exchange bot fleet

Seed ~1000 human-named users (each with 20,000 coins) onto the **real** Kalki
Exchange (`bet`) and run a fleet that trades **every open market (existing +
newly created)** and posts **LLM-generated one-liner comments** — reusing the
simulator's bot personalities to drive the decisions.

Verified end-to-end against a local `bet` instance: trades move the real AMM
price, wallets debit, comments land under human-like usernames, 1000-bot scale,
0 rejects.

---

## How it's wired (no NextAuth juggling)

The public trade/comment endpoints are session-gated, so the fleet uses **internal
service routes** (Bearer `INTERNAL_API_SECRET`, the same pattern as the existing
`/api/internal/wallet`). The core trade logic is shared, so a bot trade is
byte-for-byte identical to a human one.

```
 live/runner.py  ──Bearer──▶  bet  /api/internal/markets     (poll open markets)
 (reuses sim bot              app  /api/internal/bot-users    (the fleet roster)
  personalities)                   /api/internal/trade  ──▶ lib/trade-core (same
                                   /api/internal/comment      executeBuy/Sell as
                                                              the public route)
```

### Changes made to the `bet` repo

| File | What |
|------|------|
| `lib/trade-core.ts` | **New.** `executeBuy`/`executeSell`/`HttpError` extracted verbatim from `app/api/trade/route.ts` (now exported, parameterised by `userId`). |
| `app/api/trade/route.ts` | Refactored to import from `lib/trade-core` (behaviour unchanged — public route still works, returns 401 without a session). |
| `app/api/internal/trade/route.ts` | **New.** Bearer-auth trade as any `userId`; reuses `executeBuy/Sell`; publishes the same SSE events so bot trades show live. |
| `app/api/internal/comment/route.ts` | **New.** Bearer-auth comment as any `userId` (same 500-char, single-level-threading rules). |
| `app/api/internal/markets/route.ts` | **New.** Open markets + AMM price (so the fleet can poll existing + NEW markets). |
| `app/api/internal/bot-users/route.ts` | **New.** The seeded fleet roster (users on the `@sim.kalki.local` email domain). |
| `scripts/seed-bots.ts` | **New.** Seeds N human-named users + 20k-coin wallets. Idempotent; `--reset-coins`, `--purge`. |

### New files on the simulator side

```
bots/
├── run_live.py            # launcher
├── config.live.yaml       # base_url + secret (env), fleet mix, pacing, comment backend
└── live/
    ├── kalki_client.py    # stdlib HTTP client for the internal routes
    ├── comment_gen.py     # one-liner comments: template (offline) | llm (Qwen)
    └── runner.py          # the fleet loop (reuses sim personalities + signals)
```

---

## Run it (local first)

**1. Configure the `bet` server.** In `bet/bet/.env` set a secret:

```
INTERNAL_API_SECRET="dev-bot-fleet-secret-0c4f1e7a9b"
```

**2. Seed markets + the fleet** (from `bet/bet`):

```bash
npx prisma db seed                              # ensures demo markets exist
BOTS_COUNT=1000 npx tsx scripts/seed-bots.ts    # 1000 users × 20,000 coins
npm run dev                                      # http://localhost:3100/markets
```

**3. Run the fleet** (from `bots/`):

```bash
python run_live.py --config config.live.yaml            # runs continuously
python run_live.py --cycles 20                          # or a bounded run
```

Open `http://localhost:3100/markets` and watch the bots trade and comment live.

### Point it at production (config/env only — no code change)

```bash
export KALKI_URL=https://kalki.bet/markets       # include the /markets basePath
export INTERNAL_API_SECRET=<prod secret>         # must match the deployed bet .env
python run_live.py
```

> Seed the prod users with the same script against the prod `DATABASE_URL`
> (`BOTS_COUNT=1000 npx tsx scripts/seed-bots.ts`), then run the fleet.

---

## Behaviour & tuning (`config.live.yaml`)

- **Personalities** — each bot is assigned one (deterministically by user id) from
  `fleet.mix` (momentum / contrarian / news-reactive / overconfident / herd /
  noise), reusing the exact `intent()` logic from the offline simulator.
- **Signals** — each market's title is run through the inference layer **once per
  cycle** (shared across all bots — no per-bot inference), feeding the
  news/overconfident personalities. Swap `inference.*_backend` to FinBERT/Qwen.
- **Comments** — `comments.backend: template` (offline, human-like) or `llm`
  (set `qwen_url`; the reference server in `inference_server/server.py` serves a
  `comment` task). The line reflects the bot's actual stance.
- **Pacing** — `trades_per_cycle`, `cycle_interval_s`, `comment_rate`, and
  `fleet.trade_prob` control intensity. Defaults are gentle (≈40 trades / 2 s)
  so 1000 bots look organic and don't hammer the server. `max_trade_coins` caps
  per-trade size so thin markets don't snap.
- **New markets** — the runner re-polls `/api/internal/markets` every cycle and
  auto-onboards anything new (initialises its history + signal), so bots start
  trading freshly-created markets automatically.

---

## Managing the fleet

```bash
# top up / resize the roster (idempotent)
npx tsx scripts/seed-bots.ts --count 1000 --reset-coins
# remove the entire fleet (cascades wallets/positions/comments)
npx tsx scripts/seed-bots.ts --purge
```

Bot users are tagged by the `@sim.kalki.local` email domain (usernames stay
human-like). Per your choice this is a **closed research sandbox**, so accounts
are intentionally indistinguishable from humans; on a platform with real users
you'd add a visible bot marker first.

---

## Verified

Against a local `bet` (Postgres + Next.js):

- `POST /api/internal/trade` moved a real market `0.12 → 0.48`, debited the wallet
  `20000 → 19200`; wrong secret → 401.
- 1000 users seeded (20k each); fleet onboarded all 41 open markets; trades +
  comments landed (human-like usernames, e.g. `@swati60: "buying YES here,
  momentum looks strong"`), **0 rejects**.
- Public `/api/trade` still compiles and auth-gates (refactor preserved it).
