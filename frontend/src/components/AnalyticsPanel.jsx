import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { linePath } from "../chart.js";

const Spark = ({ vals }) => {
  if (!vals || vals.length < 2) return null;
  const lo = Math.min(...vals), hi = Math.max(...vals);
  return (
    <svg height="20" width="120" viewBox="0 0 120 20" preserveAspectRatio="none" style={{ width: 120 }}>
      <path d={linePath(vals, 120, 20, lo, hi, 2)} fill="none" strokeWidth="1.3"
        stroke={vals[vals.length - 1] >= 0 ? "var(--grn)" : "var(--red)"} />
    </svg>
  );
};

export default function AnalyticsPanel() {
  const [types, setTypes] = useState([]);
  useEffect(() => {
    const load = () => api.analytics().then((d) => setTypes(d.types));
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, []);
  const cls = (n) => (n >= 0 ? "pos" : "neg");
  return (
    <div className="card" style={{ flex: 1, minHeight: 0 }}>
      <h3>Behaviour analytics — PnL by personality <span className="muted">(bias effectiveness)</span></h3>
      <div className="scroll" style={{ flex: 1 }}>
        <table className="analytics">
          <thead><tr><th>type</th><th>avg PnL</th><th>curve</th><th>W/L</th><th>avg size</th><th>trades</th><th>delay</th><th>profit%</th></tr></thead>
          <tbody>
            {types.map((t) => (
              <tr key={t.type}>
                <td><b>{t.type}</b> <span className="muted">×{t.count}</span></td>
                <td className={cls(t.avg_pnl)}>{(t.avg_pnl >= 0 ? "+" : "") + t.avg_pnl} <span className="muted">({t.avg_pnl_pct}%)</span></td>
                <td><Spark vals={t.pnl_curve} /></td>
                <td>{t.win_loss_ratio}</td><td>{t.avg_trade_size}</td><td>{t.trades_per_bot}</td>
                <td>{t.avg_reaction_delay}</td>
                <td><div className="bar" style={{ width: Math.round(t.profit_share * 60) }} />{Math.round(t.profit_share * 100)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
