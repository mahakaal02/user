# `bet`-side integration (overlay)

These are the files the live fleet ([`../live/`](../live)) needs on the Kalki
Exchange (`bet`) backend. They are kept **here**, in the bots project, so the
`bet` repo stays clean — apply them as an overlay rather than committing them
into `bet`.

**They are purely additive — no existing `bet` file is modified.** Drop them in
preserving the paths:

```
bet-integration/lib/trade-core.ts                      → bet/bet/lib/trade-core.ts
bet-integration/app/api/internal/trade/route.ts        → bet/bet/app/api/internal/trade/route.ts
bet-integration/app/api/internal/comment/route.ts      → bet/bet/app/api/internal/comment/route.ts
bet-integration/app/api/internal/markets/route.ts      → bet/bet/app/api/internal/markets/route.ts
bet-integration/app/api/internal/bot-users/route.ts    → bet/bet/app/api/internal/bot-users/route.ts
bet-integration/scripts/seed-bots.ts                   → bet/bet/scripts/seed-bots.ts
```

e.g. from the repo root:

```bash
cp -R bots/bet-integration/lib       bots/bet-integration/app  bots/bet-integration/scripts  bet/bet/
```

Then in `bet/bet/.env` set the shared secret the routes authenticate with:

```
INTERNAL_API_SECRET="<a long random string>"     # openssl rand -base64 32
```

Seed the fleet and run (see [`../LIVE.md`](../LIVE.md) for the full flow):

```bash
cd bet/bet
BOTS_COUNT=1000 npx tsx scripts/seed-bots.ts
npm run dev
```

## What each file is

| File | Purpose |
|------|---------|
| `lib/trade-core.ts` | `executeBuy`/`executeSell` (the AMM trade transaction), exported so the internal route reuses the exact same logic as the public `/api/trade`. |
| `app/api/internal/trade/route.ts` | Bearer-auth trade as any `userId`. |
| `app/api/internal/comment/route.ts` | Bearer-auth comment as any `userId`. |
| `app/api/internal/markets/route.ts` | Open markets + AMM price (fleet polls for existing + new markets). |
| `app/api/internal/bot-users/route.ts` | The seeded fleet roster (`@sim.kalki.local` users). |
| `scripts/seed-bots.ts` | Seeds N human-named users + 20k-coin wallets. Idempotent; `--reset-coins`, `--purge`. |

## Note — the trade route is untouched

`trade-core.ts` is **standalone**: the internal route imports `executeBuy`/
`executeSell` from it, and the public `app/api/trade/route.ts` keeps working
unchanged. Nothing in the existing `bet` app needs editing.

*Optional DRY tidy-up (not required):* if you prefer one copy of the trade
logic, change `bet/bet/app/api/trade/route.ts` to `import { executeBuy,
executeSell, HttpError } from "@/lib/trade-core"` and delete its local copies of
those functions. The verified local instance in this session used that tidied
form; the standalone form here is functionally identical.
