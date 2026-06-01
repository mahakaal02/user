# Bot Admin Panel — React SPA (optional)

The built-in `admin/static/dashboard.html` is the zero-build working dashboard.
This `frontend/` is the **same panel as a React/Vite app**, for teams who want a
component-based SPA. It talks to the identical REST + SSE API, so the Python
backend is unchanged.

```bash
# 1) start the backend (serves the API on :8080)
python ../run_admin.py --config ../config.offline.yaml --port 8080

# 2) start the React dev server (proxies the API → :8080)
npm install
npm run dev            # http://localhost:5173
```

## State management

One-way data flow:

```
EventSource('/stream')  ──useLiveSocket──▶  useReducer store (store.jsx)  ──▶  components
controls (buttons/sliders)  ──api.js POST──▶  backend  ──▶  next SSE tick updates the store
```

* `store.jsx` — the single source of truth (a `useReducer` + context). The SSE
  `tick` action appends to bounded series (price/sentiment/volume) and replaces
  the market/types/control snapshots.
* `useLiveSocket.js` — opens the one `EventSource` and dispatches ticks. Browser
  auto-reconnect; no manual retry logic.
* `api.js` — typed REST helpers. The high-frequency bot table and analytics are
  **polled** (1.5s / 3s) rather than streamed, so 1000 bots never flood the
  socket.

## Components (map 1:1 to the dashboard)

| File | Role |
|---|---|
| `App.jsx` | layout + opens the bot modal |
| `components/GlobalControls.jsx` | play/pause, reset, news, volatility, liquidity, chaos, stress |
| `components/BotTable.jsx` | filterable bot list (polls `/bots`) |
| `components/MarketChart.jsx` | price / sentiment / volume (SVG, from the store) |
| `components/AnalyticsPanel.jsx` | per-type PnL curve, win/loss, bias effectiveness |
| `components/EventFeed.jsx` | live events + trades stream |
| `components/BotModal.jsx` | full state, equity curve, live param edit, pause/reset |

Charts use `src/chart.js` (tiny SVG helpers) — no charting dependency.
