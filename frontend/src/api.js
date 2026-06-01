// Thin REST helpers. Paths are same-origin in dev thanks to the Vite proxy
// (see vite.config.js) → the Python admin server on :8080.
export const get = (p) => fetch(p).then((r) => r.json());
export const post = (p, body) =>
  fetch(p, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).then((r) => r.json());

export const api = {
  bots: (f = {}) =>
    get(`/bots?limit=${f.limit || 150}&sort=${f.sort || "pnl"}&type=${f.type || ""}&status=${f.status || ""}`),
  bot: (id) => get(`/bot/${id}`),
  botAction: (id, action) => post(`/bot/${id}/${action}`),
  botUpdate: (id, params) => post(`/bot/${id}/update`, params),
  analytics: () => get("/analytics"),
  history: () => get("/market/history"),
  config: (cfg) => post("/simulation/config", cfg),
  sim: (action) => post(`/simulation/${action}`),
  pauseAll: () => post("/bots/pause_all"),
  resumeAll: () => post("/bots/resume_all"),
  stress: (on) => post("/simulation/stress", { on }),
  replay: (body) => post("/simulation/replay", body),
};
