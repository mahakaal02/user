import React from "react";
import { useStore } from "../store.jsx";
import { api } from "../api.js";

export default function GlobalControls() {
  const { state, dispatch } = useStore();
  const c = state.control || {};
  const cfg = (k, v) => api.config({ [k]: v });
  return (
    <div className="topbar">
      <h1>🧪 Bot Admin Panel</h1>
      <span className="badge">
        <span className={"dot" + (state.connected ? " on" : "")} /> {state.connected ? "live" : "…"}
      </span>
      <span className="badge">tick <b>{c.tick ?? "–"}</b></span>
      <span className="badge">bots <b>{c.n_bots ?? "–"}</b></span>
      <span className="badge" style={{ color: c.mode === "replay" ? "var(--yel)" : "var(--mut)" }}>{c.mode}</span>
      <button className={c.running ? "" : "primary"} onClick={() => api.sim(c.running ? "pause" : "resume")}>
        {c.running ? "⏸ Pause" : "▶ Resume"}
      </button>
      <button className="danger" onClick={() => { if (confirm("Reset simulation?")) { api.sim("reset"); dispatch({ type: "resetSeries" }); } }}>⟲ Reset</button>
      <button className="sm" onClick={() => api.pauseAll()}>⏸ Pause all</button>
      <button className="sm" onClick={() => api.resumeAll()}>▶ Resume all</button>
      <div className="spacer" />
      <span className="ctl">news <button className="sm" onClick={() => cfg("news_enabled", !c.news_enabled)}>{c.news_enabled ? "ON" : "OFF"}</button></span>
      <span className="ctl">speed <input type="range" min="1" max="30" value={c.speed || 6} onChange={(e) => cfg("speed", +e.target.value)} /> {c.speed}</span>
      <span className="ctl">vol <input type="range" min="0" max="4" step="0.1" value={c.volatility ?? 1} onChange={(e) => cfg("volatility", +e.target.value)} /> {(c.volatility ?? 1).toFixed(1)}</span>
      <span className="ctl">liq <input type="range" min="0.2" max="4" step="0.1" value={c.liquidity_scale ?? 1} onChange={(e) => cfg("liquidity", +e.target.value)} /> {(c.liquidity_scale ?? 1).toFixed(1)}</span>
      <span className="ctl">chaos <input type="range" min="0" max="1" step="0.05" value={c.chaos ?? 0} onChange={(e) => cfg("chaos", +e.target.value)} /> {(c.chaos ?? 0).toFixed(2)}</span>
      <button className="sm" onClick={() => api.stress((c.aggression_mult || 1) < 2)}>🔥 Stress</button>
    </div>
  );
}
