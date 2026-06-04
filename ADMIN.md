# Bot Admin Panel

A research-grade, real-time control center for the simulation. Control every
bot, steer the market, watch emergent behaviour live, and replay past runs.

> **Tech choice.** The brief suggested FastAPI + React, but the whole simulator
> is a deliberately dependency-light stdlib package — so the panel matches that:
> **Python stdlib HTTP server + Server-Sent Events**, zero new dependencies, and
> a **single-file dashboard** that runs with no build step. (It's the same
> realtime shape your `bet` app already uses — SSE via `lib/pubsub.ts`.) A real
> **React/Vite** structure is also provided in `frontend/` for those who want the
> full SPA — it talks to the exact same API. The deliverable is the panel; the
> framework is interchangeable because everything goes through the HTTP/SSE API.

```bash
python run_admin.py                 # → http://127.0.0.1:8080  (offline config, 1000 bots)
python run_admin.py --config config.offline.yaml --port 8080 --paused
```

Stdlib only (+ PyYAML for the YAML config). No node, no build, no external
services.

---

## What it does (every mandatory feature)

| Spec | Where |
|---|---|
| **A. Bot dashboard** — id, type, bankroll, PnL, positions, risk, status | left table, `GET /bots` |
| **B. Bot controls** — pause / resume / reset / edit (risk, bias, trade freq, reaction delay, bankroll) | bot modal, `POST /bot/{id}/…` |
| **C. Global controls** — pause/resume all, reset sim, news on/off, volatility, liquidity | top bar, `POST /simulation/*` |
| **D. Live market** — price/probability, price history chart, volume, sentiment timeline, news feed | center charts, SSE + `GET /market/*` |
| **E. Behaviour analytics** — per-type PnL curve, win/loss, avg trade size, reaction-delay & bias effectiveness | analytics table, `GET /analytics` |
| **F. Event log** — live stream of news, sentiment, bot actions, trades, price | right feeds, SSE `/stream` |
| **G. Model endpoints** — set/test optional **Qwen** + **embedding** URLs so the bots use real models | Model Endpoints card, `GET/POST /models`, `POST /models/test` |
| **Bonus** — replay-as-video, stress mode, chaos slider | replay box, `🔥 Stress`, `chaos` slider |

### Model endpoints (Qwen + embedding, both optional)

The **Model Endpoints** card lets the admin point the bots at real models at
runtime — no restart, no config edit:

- **Qwen URL** — applied to the running simulation **immediately** (per-tick
  shared reasoning routes through Qwen; high-volume per-headline sentiment stays
  on the local heuristic so Qwen isn't hammered). Empty → local heuristic.
- **API key + model** (next to the Qwen URL) — for a **hosted, OpenAI-compatible
  Qwen** (e.g. PodStack at `…/chat/completions`). When a key is set, or the URL is
  a `/chat/completions` endpoint, the panel uses the OpenAI chat protocol
  (Bearer auth + `model` id) instead of the legacy custom server — auto-detected,
  no toggle. A bare `http://host:8002/qwen` with no key still uses the custom
  contract. The key is **never echoed back** (a masked hint is shown); leave the
  box blank on Save to keep the saved key.
- **Embedding URL** — the news relevance cheap-filter's semantic backend. Empty →
  local lexical hashing.
- **Test** buttons ping each endpoint (`POST /models/test`) and show
  reachable/latency + the protocol (`openai`/`custom`), or the error — no lock
  held, so the sim keeps ticking. The Qwen test makes a real Bearer-authed chat
  completion when in OpenAI mode.
- All settings are **persisted** to `runs/model_endpoints.json` (gitignored — it
  may hold the key), which `run_live.py` reads on start, so the **live Kalki
  fleet** uses the same Qwen (reasoning + LLM comments + relevance refine) and
  embedding endpoints. Admin settings override config/env. `remote_fallback_local`
  keeps everything running if an endpoint is unreachable.

Verified live with 1000 bots: price chart renders the bubble→crash→recovery,
analytics rank personalities (contrarian wins, herd loses), trades stream at
~500 fills/tick, no console errors, no lag.

---

## Architecture

```
 ┌─────────── browser (dashboard.html, no build) ───────────┐
 │  EventSource('/stream')  ◀── live ticks (SSE)             │
 │  fetch('/bots','/analytics')  ── polled 1.5s/3s           │
 │  fetch POST /bot/* /simulation/*  ── control commands     │
 └───────────────────────────┬──────────────────────────────┘
                             HTTP
 ┌───────────────────────────▼──────────────────────────────┐
 │  admin/server.py  — stdlib ThreadingHTTPServer            │
 │    • REST handlers (take the sim lock, read/mutate)       │
 │    • SSE /stream (per-client queue, no lock while streaming)│
 └───────────────────────────┬──────────────────────────────┘
                     shared LiveSimulation
 ┌───────────────────────────▼──────────────────────────────┐
 │  admin/manager.py — LiveSimulation                        │
 │    background thread: drain controls → tick → broadcast   │
 │    wraps the EXISTING engine pieces unchanged:            │
 │    build_inference_client / build_market /                │
 │    build_population / build_news_source / build_signal_layer │
 └──────────────────────────────────────────────────────────┘
```

### Concurrency / state management (backend)

* **One** background thread runs the tick loop. It holds a single
  `threading.RLock` while it mutates simulation state each tick (~2 ms for 1000
  bots).
* Every HTTP handler takes the **same lock** for its read or mutation, so it
  sees a consistent snapshot and never races a half-finished tick. Worst-case
  wait ≈ one tick.
* The **SSE** endpoint does *not* hold the lock while streaming — it owns a
  thread-safe `queue.Queue`; the tick loop pushes one payload per tick to every
  subscriber's queue and the handler drains it to the wire.
* No `async`, no per-bot locks — plain stdlib threads, matching the project.

### Why SSE (not WebSocket)

The live feed is one-directional (server → browser); controls are ordinary
POSTs. SSE gives that with **zero dependencies and zero framing code**, auto-
reconnects in the browser (`EventSource`), and is exactly what the requirement
("WebSocket **or** polling for live updates") is after. Swapping to WebSocket
later is localised to `server._stream` + the client's `connect()`.

### Scaling to 1000 bots without lag

* SSE payload is **bounded**: market scalars + per-type aggregates (6 rows) + a
  **sample** of ≤15 trades + new event lines. It does **not** stream 1000 rows.
* The full bot table is fetched by **polling** `GET /bots?limit=150` (top-N by
  the chosen sort), so the DOM stays light.
* A tick is ~2 ms; paced at `speed` ticks/sec (default 6), the server is ~99 %
  idle. Measured: 1000 bots stream smoothly; 500+ fills/tick.

---

## API reference

### Bots
| Method | Path | Body / Query | Returns |
|---|---|---|---|
| GET | `/bots` | `?type=&status=&sort=pnl&limit=150` | `{bots:[…], count}` |
| GET | `/bot/{id}` | — | full snapshot + `equity_curve`, `memory` |
| POST | `/bot/{id}/pause` · `/resume` · `/reset` | — | `{ok, bot}` |
| POST | `/bot/{id}/update` | `{aggressiveness?,bias?,trade_prob?,reaction_delay?,coins?}` | `{ok, bot}` |
| POST | `/bots/pause_all` · `/bots/resume_all` | — | `{ok}` |

### Market
| GET | `/market/state` | current price/prob + control snapshot |
| GET | `/market/history` | price/volume/flow/sentiment ring (≤2000) |
| GET | `/market/events` | news-impact events |
| GET | `/analytics` | per-type PnL curve, win/loss, avg size, delay, profit-share |

### Simulation
| POST | `/simulation/pause` · `/resume` · `/reset` | — |
| POST | `/simulation/config` | `{news_enabled?,speed?,volatility?,liquidity?,aggression_mult?,chaos?,mode?}` |
| POST | `/simulation/stress` | `{on:bool}` → global aggression ×2.5 |
| POST | `/simulation/replay` | `{path}` load · `{action:"seek",idx}` · `{action:"exit"}` |

### Live stream
`GET /stream` — `text/event-stream`. Each tick emits:

```jsonc
{ "type":"tick", "tick": 268,
  "market": { "price":0.39, "prob":0.39, "volume":5646, "net_flow":2583.8,
              "directional":0.6, "sentiment":{"positive":..,"negative":..,"neutral":..},
              "liquidity":103000, "orders":702, "trades":526 },
  "news":   ["Central bank signals surprise interest rate cut", …],
  "trades": [ {"bot_id":"momentum-12","type":"momentum","side":"BUY","outcome":"YES",
               "coins":120.0,"shares":190.1,"price":0.63}, … up to 15 ],
  "events": [ {"tick":268,"kind":"news"|"price"|"trade","text":"…"} ],
  "types":  [ {"type":"contrarian","count":160,"active":160,"avg_pnl":496,"profit_share":1.0,…} ],
  "control":{ "running":true,"news_enabled":true,"speed":6,"volatility":1.0,
              "liquidity_scale":1.0,"aggression_mult":1.0,"chaos":0.0,"mode":"live","tick":268,"n_bots":1000 } }
```

---

## Folder structure (added)

```
bots/
├── run_admin.py                 # launcher (python run_admin.py)
├── ADMIN.md                     # this file
├── admin/
│   ├── __init__.py
│   ├── manager.py               # LiveSimulation: threaded loop, controls, stats, SSE fan-out
│   ├── server.py                # stdlib HTTP + SSE + REST handlers
│   └── static/
│       └── dashboard.html       # the working dashboard (vanilla JS + SVG, no build)
├── frontend/                    # OPTIONAL React/Vite SPA (same API) — see frontend/README.md
└── .claude/launch.json          # preview/launch config for the admin server
```

Core engine change: `sim/bots/base.py` gained admin hooks (`status`, live
`set_params`/`reset_state`, per-bot stats: trades, realized PnL via cost basis,
win/loss, `snapshot()`). All additive — the existing tests and the batch
`run.py` are unaffected (verified).

---

## Realism

* Bots act **independently** — each has its own seeded RNG, bias, and reaction
  delay; you watch 1000 of them diverge.
* **Delays are visible** — news-reactive bots move first; momentum/herd pile in
  ticks later (the analytics' `avg_reaction_delay` column makes this concrete).
* **Trades stream live** — the right-hand feed shows real fills as they clear.
* **Sentiment visibly moves price** — the sentiment line and price line track
  together in the chart; toggle **news OFF** and the price flattens into noise.
* Pace it with **speed**; perturb it with **volatility / chaos / stress** and
  watch the regime change in real time.

---

## React structure (optional full SPA)

If you prefer a React app, `frontend/` is a Vite project that renders the same
panel from the same API. State management: a single `EventSource` feeds a
`useReducer` store (`src/store.jsx`); components subscribe via context; controls
dispatch `POST`s through `src/api.js`. Run:

```bash
cd frontend && npm install && npm run dev      # Vite dev server, proxies /api → :8080
```

Component map (1:1 with the dashboard sections):

```
src/
  main.jsx                # mount
  App.jsx                 # layout + provider
  store.jsx               # reducer + context (SSE-fed state)
  useLiveSocket.js        # EventSource('/stream') → dispatch
  api.js                  # GET/POST helpers
  components/
    GlobalControls.jsx    # top bar (play/pause/reset/news/vol/liq/chaos/stress)
    BotTable.jsx          # left: filterable bot list (polls /bots)
    MarketChart.jsx       # center: price/sentiment/volume (from store.history)
    AnalyticsPanel.jsx    # center: per-type PnL curves + bias effectiveness
    EventFeed.jsx         # right: live events + trades
    BotModal.jsx          # detail + live param edit
```
