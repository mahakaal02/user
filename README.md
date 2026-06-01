# Agent-Based Prediction-Market Simulator

A standalone, modular **multi-bot behavioural simulation engine** for the Kalki
Exchange prediction market. A population of 100–1000 heterogeneous trading bots
reacts to a shared news/inference signal and trades a binary YES/NO market,
producing emergent **bubbles, crashes, herding, delayed news reaction and
contrarian corrections**.

All ML inference (sentiment + reasoning/event-extraction) lives behind **one
pluggable interface** and is selected entirely from `config.yaml`. Bot logic
never imports a model, an SDK, or a URL — swapping FinBERT/Qwen for anything
else is a config edit, not a code change.

> **Academic use only.** This studies prediction markets, behavioural economics
> and agent-based trading dynamics. It is not a trading system. It mirrors the
> math of the `bet/` app but trades a simulated market by default.

> **🧪 Bot Admin Panel** — a real-time control center (control every bot, steer
> the market, watch emergent behaviour live, replay past runs) ships in
> `admin/`. Run `python run_admin.py` → open http://127.0.0.1:8080. Stdlib HTTP +
> SSE, no build step. See **[ADMIN.md](ADMIN.md)**.

---

## Why it's a separate Python service

Your market core (`../bet`) is **TypeScript** (Next.js + Prisma) with a
constant-product AMM + CLOB. Wedging a Python bot engine into it would mean
either rewriting bots in TS or breaking the core. Instead this is a clean
sidecar that:

* **mirrors** the real AMM math (`sim/market/amm.py` is a 1:1 port of
  `bet/bet/lib/amm.ts`, verified in `tests/test_amm_parity.py`), so simulated
  price formation is identical to production; and
* can optionally **drive the real `bet` API over HTTP** (`market.mode: http`)
  with zero engine changes.

So the existing repo is untouched; this extends it as a peer.

---

## Folder structure

```
bots/
├── run.py                     # example entrypoint / CLI
├── config.yaml                # canonical config — REMOTE inference (FinBERT + Qwen)
├── config.offline.yaml        # 100% local + deterministic (default for research/CI)
├── config.offline.json        # same, JSON — runs with ZERO installs (no PyYAML)
├── requirements.txt           # PyYAML only (core is stdlib); extras optional
├── sim/
│   ├── config.py              # config loader + ${ENV} substitution + validation
│   ├── rng.py                 # seeded, reproducible random sub-streams
│   ├── signals.py             # SignalLayer — shared per-tick inference output
│   ├── news.py                # news feeds: synthetic / stored / rss
│   ├── metrics.py             # bubble/crash/herd detection + P&L + ASCII chart
│   ├── inference/             # ── THE PLUGGABLE INFERENCE LAYER ──
│   │   ├── base.py            #    InferenceClient ABC + standardized schemas
│   │   ├── local_heuristic.py #    offline, deterministic backend (no GPU/net)
│   │   ├── remote.py          #    FinBERTAPIClient + QwenAPIClient (HTTP)
│   │   ├── replay.py          #    ReplayInferenceClient (deterministic re-runs)
│   │   ├── caching.py         #    per-tick dedup → enforces "no per-bot inference"
│   │   └── factory.py         #    composite router + build_inference_client()
│   ├── market/                # ── THE PLUGGABLE MARKET LAYER ──
│   │   ├── amm.py             #    CPMM, ported from bet/lib/amm.ts
│   │   ├── orderbook.py       #    minimal CLOB, echo of bet/lib/orderbook.ts
│   │   ├── gateway.py         #    MarketGateway ABC
│   │   ├── sim_market.py      #    in-process market (default)
│   │   ├── http_market.py     #    bridge to the real bet API
│   │   └── types.py           #    Order / Fill / MarketView
│   ├── bots/                  # ── THE BOTS (backend-agnostic) ──
│   │   ├── base.py            #    bias, memory, reaction delay, wallet, sizing
│   │   ├── momentum.py  contrarian.py  news_reactive.py
│   │   ├── overconfident.py   herd.py   noise.py
│   │   └── factory.py         #    build_population() from config
│   └── engine/
│       ├── loop.py            #    the tick loop (the 8-step simulation cycle)
│       └── recorder.py        #    JSONL state log + replay tape
├── inference_server/          # OPTIONAL reference server for the "friend's machine"
│   ├── server.py              #    FastAPI: POST /finbert, POST /qwen
│   └── requirements.txt
├── run_admin.py              # ── BOT ADMIN PANEL launcher (see ADMIN.md) ──
├── ADMIN.md                  #    admin panel docs (API, SSE, state mgmt, React structure)
├── admin/                    #    real-time control center (stdlib HTTP + SSE)
│   ├── manager.py            #    LiveSimulation — threaded tick loop + controls + stats
│   ├── server.py             #    REST + SSE endpoints + static
│   └── static/dashboard.html #    the working dashboard (vanilla JS, no build)
├── frontend/                 #    OPTIONAL React/Vite SPA (same API)
└── tests/
    ├── test_amm_parity.py         # Python AMM == bet/lib/amm.ts
    ├── test_determinism.py        # same seed → identical run; inference cost ⟂ bot count
    ├── test_inference_contract.py # schemas, composite routing, per-tick cache dedup
    └── test_admin.py              # admin control surface: pause/edit/reset, knobs, SSE shape
```

---

## Architecture

Two swappable seams, one shared signal bus:

```
  news ─▶ InferenceClient ─▶ SignalLayer ─▶ [ every bot reads the SAME layer ]
        (FinBERT / Qwen /     (built ONCE          │
         local / replay)       per tick)           ▼
                                            bots emit Orders
                                                    │
                                            MarketGateway.clear()
                                          (SimMarket AMM  |  HttpMarket→bet API)
                                                    │
                                            prices update ─▶ recorder (JSONL)
```

**The tick loop** (`sim/engine/loop.py`) — each tick:

1. fetch the tick's news (synthetic / stored / RSS),
2. run the **shared** inference pipeline once → build one `SignalLayer`,
3. publish that layer + a `MarketView` to every bot,
4. each bot **observes the identical layer**,
5. each bot emits at most one `Order`,
6. the market **clears** all orders (AMM, fair shuffled order),
7. prices update from the fills,
8. state is logged (incl. the per-tick upstream inference-call count).

Inference is called `O(headlines)` times per tick — **never inside the per-bot
loop** — so cost is independent of bot count. That is the structural guarantee
behind "no per-bot model inference," and the recorder logs the count so you can
verify it.

---

## Quickstart

```bash
cd bots
python3 -m venv .venv && source .venv/bin/activate     # optional
pip install -r requirements.txt                         # just PyYAML

# Fully offline, deterministic, no network/GPU:
python run.py --config config.offline.yaml

# Zero-install variant (no PyYAML needed):
python run.py --config config.offline.json
```

Run the tests (no pytest required):

```bash
python tests/test_amm_parity.py
python tests/test_determinism.py
python tests/test_inference_contract.py
```

### Sample output

```
Running 120 ticks · 1000 bots · inference=local_heuristic/local_heuristic · market=sim · seed=7
  price path (YES, 0.29–0.87):
    ▅▅▆▆▆▆▆▆▆▆▆▅▅▄▃▃▄▄▅▅▅▆▆▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▆▆▅▄▃▃▄▄▄▄▄▃▃▃▃▄▄▄▄▅▅▅▅▅▅▅▄▄▃▃▃▄▄▄▄▄▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▄▄▃▃▃▄▄▄▄▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▄

  emergent phenomena
    bubble : YES  (run-up +0.55 into peak 0.87 @ tick 29)
    crash  : YES  (max drawdown 66.6% to 0.29)
    herding: YES  (flow autocorr 0.823, sign persistence 0.908)
    delayed reaction: bull signal @ 21 → peak +8 ticks; bear @ 62 → trough +7 ticks

  P&L by bot type (avg per bot)
    contrarian     n=160  avg P&L    +554.8  ( +55.5%)
    herd           n=240  avg P&L    -305.6  ( -30.6%)
    momentum       n=220  avg P&L    -135.7  ( -13.6%)
    news_reactive  n=140  avg P&L     +51.4  (  +5.1%)
    noise          n=120  avg P&L     +81.4  (  +8.1%)
    overconfident  n=120  avg P&L    -109.1  ( -10.9%)

  hard-constraint check
    total upstream inference calls : 256
    max inference calls in any tick: 5  (independent of bot count ✔)
```

A clean **bubble → crash → recovery**, the contrarians profit while the herd /
momentum / overconfident crowd loses (textbook behavioural finance), and
inference cost stays flat at ≤5 calls/tick regardless of the 1000 bots.

---

## Configuration — everything is swappable here

`config.yaml` is the single source of truth. The `inference:` block names a
backend **per capability**:

```yaml
inference:
  sentiment_backend: finbert_api     # who answers .sentiment()
  event_backend:     qwen_api        # who answers .event_extract()
  reasoning_backend: qwen_api        # who answers .reasoning()
  remote_fallback_local: true        # degrade to local heuristic if a box is down

  finbert_api:
    type: finbert_api
    url: "${FINBERT_URL:-http://YOUR_FRIEND_MACHINE:8001/finbert}"
    timeout_s: 5
  qwen_api:
    type: qwen_api
    url: "${QWEN_URL:-http://YOUR_FRIEND_MACHINE:8002/qwen}"
    timeout_s: 20

  cache: { enabled: true, scope: tick }   # per-tick dedup
```

URLs come from **environment variables** (`${VAR:-default}`), so hosts/secrets
are injected at deploy time and never hardcoded.

### Deployment Mode A ↔ Mode B (config only, no code changes)

| | What changes |
|---|---|
| **Mode A — local / friend's machine** | `export FINBERT_URL=http://192.168.1.50:8001/finbert` and point `qwen_api.url` likewise. FinBERT can even run locally while Qwen is remote — mix freely. |
| **Mode B — all remote** | Point both URLs at the remote inference box. |
| **Offline / CI / laptop** | Use `config.offline.yaml` — every backend is `local_heuristic` (no net, no GPU). |
| **Deterministic replay of a remote run** | `python run.py --config config.yaml --out run1.jsonl` then `python run.py --config config.yaml --replay run1.jsonl`. |

Nothing but config/env differs between these. Bots are oblivious.

---

## The inference contract

Every backend implements `sim/inference/base.py::InferenceClient`:

```python
class InferenceClient:
    def sentiment(self, text: str) -> dict: ...
    def event_extract(self, text: str) -> dict: ...
    def reasoning(self, prompt: str) -> dict: ...
```

and returns the **standardized schemas** (coerced through `normalize_*`, so any
backend's raw output is forced into shape):

```jsonc
// sentiment
{ "positive": 0.7, "negative": 0.1, "neutral": 0.2, "confidence": 0.85 }

// reasoning / event extraction
{ "event": "interest_rate_cut",
  "impact": { "market_up": 0.8, "inflation_down": 0.6 },
  "confidence": 0.77 }
```

Adapters provided: `LocalHeuristicClient` (offline), `FinBERTAPIClient` (remote
sentiment), `QwenAPIClient` (remote reasoning/events), `ReplayInferenceClient`.
A `CompositeInferenceClient` routes each method to its configured backend, and a
`CachingInferenceClient` memoizes per tick.

### Remote server contract (the friend's machine)

```
POST /finbert   {"text": "..."}                       → sentiment schema
POST /qwen      {"task":"reasoning"|"event"|"sentiment", "prompt"/"text":"..."} → event/sentiment schema
```

A runnable reference lives in `inference_server/server.py` (FastAPI). It uses
real FinBERT/Qwen if installed and falls back to a heuristic otherwise, so the
whole Mode-A/B pipeline is testable end-to-end without a GPU:

```bash
pip install -r inference_server/requirements.txt
uvicorn inference_server.server:app --host 0.0.0.0 --port 8001
```

---

## The bots

Six personalities, each implementing only `intent(view, signal) -> [-1, 1]`:

| Bot | Drives | Behaviour |
|---|---|---|
| **Momentum** | bubbles | buys what's rising; trend amplifier |
| **Contrarian** | corrections | fades extremes (nonlinear: weak mid, strong at the edges) |
| **News-reactive** | first move | trades the shared signal × confidence; short reaction delay |
| **Overconfident** | over-extension | ignores confidence, oversizes, doubles down |
| **Herd** | stampedes | follows last tick's net order flow + recent trend |
| **Noise** | liquidity | small random impulses |

Shared machinery in `base.py`: internal **bias**, **memory**, **reaction delay**
(acts on the signal from N ticks ago), an EMA-smoothed intent (so trends build
over many ticks), a wallet/positions, and conviction-based sizing. Bots run out
of coins as a bubble inflates — an endogenous crash trigger.

A bot **never** sees the inference client — only the `SignalLayer` and
`MarketView`. Inject a new bot by adding a file + one `REGISTRY` entry.

---

## Determinism & replay

* One master `seed` → independent reproducible sub-streams (`sim/rng.py`).
* Same seed ⇒ **bit-identical** run (`test_determinism.py`).
* `--replay run.jsonl` reproduces a run **exactly**, even one that used remote
  FinBERT/Qwen, by replaying the recorded inference (verified bit-identical).

---

## Performance

Pure stdlib, no per-bot inference. Measured on this laptop (120 ticks):

```
  102 bots : 0.04s   (~300k bot-ticks/s)
 1002 bots : 0.22s   (~543k bot-ticks/s)
 2004 bots : 0.42s   (~569k bot-ticks/s)
```

Comfortably inside the 100–1000 bot target with headroom.

---

## Integration into the existing `bet` repo

The default sim needs nothing from `bet`. To drive the **real** market instead,
set `market.mode: http` (see `config.yaml`'s `market.http` block). Endpoints
used (all already in the repo):

* `GET  /api/markets/{id}/state`  → current price/reserves
* `POST /api/trade`               → AMM market-buy (NextAuth-gated)
* `POST /api/orders`              → CLOB limit order

**Auth.** `/api/trade` is user-authenticated. Two config-only options:

1. **Session token** — put a NextAuth cookie/bearer in `market.http.auth_header`
   (e.g. one shared demo account, or one per bot).
2. **Recommended — a thin internal trade route.** `bet` already has the pattern:
   `app/api/internal/wallet/route.ts` authenticates with
   `Authorization: Bearer <INTERNAL_API_SECRET>` via `lib/internal-auth.ts`.
   Add a sibling `app/api/internal/trade/route.ts` that calls the existing
   `quoteBuy` + the same transactional trade path, guarded by
   `checkInternalSecret`. Then point `trade_path: /api/internal/trade` and set
   `BET_AUTH_HEADER="Authorization: Bearer $INTERNAL_API_SECRET"`. This treats
   the simulator as a trusted internal service (like the Auctions/Aviator
   backends already do) and never touches the user-facing core.

No bot or engine code changes either way — only config.

---

## Extending

* **New inference backend** — subclass `InferenceClient`, add a branch in
  `inference/factory.py::_build_backend`, reference it by name in config.
* **New bot** — add `sim/bots/<name>.py` implementing `intent()`, register it in
  `bots/factory.py::REGISTRY`, add a population entry.
* **New news source** — subclass `NewsSource`, branch in `news.py::build_news_source`.

---

## Hard-constraint checklist

| Constraint | Where it's enforced |
|---|---|
| No per-bot LLM calls | `signals.py` builds one shared layer/tick; `caching.py` dedups; loop never calls inference per-bot; recorder logs calls/tick |
| No hardcoded API URLs in bot logic | bots only see `SignalLayer`/`MarketView`; URLs live in config + `${ENV}` |
| No breaking existing repo | separate Python sidecar; `bet/` untouched; AMM mirrored, bridge is opt-in |
| Everything swappable via config | `inference/factory.py`, `market/__init__.py`, `bots/factory.py`, `news.py` all build from config |
| Runs on a low-end laptop (100–1000 bots) | stdlib-only core, shared inference, ~0.2s for 1000 bots/120 ticks |
| Deterministic replay | `rng.py` + `recorder.py` + `replay.py` (bit-identical verified) |
```
