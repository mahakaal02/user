import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { linePath } from "../chart.js";

export default function BotModal({ id, onClose }) {
  const [b, setB] = useState(null);
  const [edit, setEdit] = useState({});
  const load = () => api.bot(id).then(setB);
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [id]);
  if (!b || b.error) return null;
  const p = b.params;
  const cls = (n) => (n >= 0 ? "pos" : "neg");
  const fmt = (n) => (n >= 0 ? "+" : "") + n;
  const val = (k, d) => (edit[k] !== undefined ? edit[k] : d);
  const apply = () =>
    api.botUpdate(id, {
      aggressiveness: +val("aggressiveness", p.aggressiveness),
      bias: +val("bias", p.bias),
      trade_prob: +val("trade_prob", p.trade_prob),
      reaction_delay: +val("reaction_delay", p.reaction_delay),
      coins: +val("coins", b.bankroll),
    }).then(load);

  const c = b.equity_curve || [];
  const lo = Math.min(...c, 0), hi = Math.max(...c, 0);

  return (
    <div className="overlay show" onClick={(e) => e.target.classList.contains("overlay") && onClose()}>
      <div className="modal">
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <div>
            <h2>{b.bot_id} <span className={"pill " + b.status}>{b.status}</span></h2>
            <span className="muted">{b.type} · risk {b.risk_level} · last: {b.last_action || "—"}</span>
          </div>
          <button className="sm" onClick={onClose}>✕</button>
        </div>
        <div className="kpis" style={{ margin: "12px 0" }}>
          <div className="kpi"><div className={"v " + cls(b.pnl)}>{fmt(b.pnl)}</div><div className="l">PnL ({b.pnl_pct}%)</div></div>
          <div className="kpi"><div className="v">{b.bankroll}</div><div className="l">bankroll</div></div>
          <div className="kpi"><div className="v">{b.realized_pnl}</div><div className="l">realized</div></div>
          <div className="kpi"><div className="v">{b.trades}</div><div className="l">trades</div></div>
          <div className="kpi"><div className="v">{b.wins}/{b.losses}</div><div className="l">win/loss</div></div>
        </div>
        <h3 className="muted" style={{ fontSize: 11 }}>EQUITY CURVE</h3>
        <svg height="70" viewBox="0 0 640 70" preserveAspectRatio="none">
          <path d={linePath(c, 640, 70, lo, hi, 4)} fill="none" strokeWidth="1.6"
            stroke={c[c.length - 1] >= 0 ? "var(--grn)" : "var(--red)"} />
        </svg>
        <h3 className="muted" style={{ fontSize: 11, marginTop: 8 }}>PARAMETERS — live edit</h3>
        <div className="row2">
          {[["aggressiveness", "risk / aggressiveness", p.aggressiveness, 0.01],
            ["bias", "bias strength", p.bias, 0.05],
            ["trade_prob", "trade frequency (prob)", p.trade_prob, 0.05],
            ["reaction_delay", "reaction delay (ticks)", p.reaction_delay, 1],
            ["coins", "bankroll (sim only)", b.bankroll, 50]].map(([k, label, dv, step]) => (
            <div className="field" key={k}>
              <label>{label}</label>
              <input type="number" step={step} defaultValue={dv} onChange={(e) => setEdit({ ...edit, [k]: e.target.value })} />
            </div>
          ))}
          <div className="field"><label>memory (recent signal)</label><input readOnly value={`[${(b.memory || []).slice(-6).join(", ")}]`} /></div>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          <button className="primary" onClick={apply}>Apply params</button>
          <button onClick={() => api.botAction(id, b.status === "paused" ? "resume" : "pause").then(load)}>
            {b.status === "paused" ? "▶ Resume" : "⏸ Pause"} bot
          </button>
          <button className="danger" onClick={() => api.botAction(id, "reset").then(load)}>⟲ Reset bot</button>
        </div>
      </div>
    </div>
  );
}
