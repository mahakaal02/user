import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// The bot table is POLLED (not streamed) so 1000 bots never flood the socket —
// we fetch the top-N by the chosen sort every 1.5s.
export default function BotTable({ onOpen }) {
  const [rows, setRows] = useState([]);
  const [f, setF] = useState({ type: "", status: "", sort: "pnl" });

  useEffect(() => {
    let alive = true;
    const load = () => api.bots(f).then((d) => alive && setRows(d.bots));
    load();
    const t = setInterval(load, 1500);
    return () => { alive = false; clearInterval(t); };
  }, [f]);

  const cls = (n) => (n >= 0 ? "pos" : "neg");
  return (
    <div className="card" style={{ flex: 1 }}>
      <h3>Bots <span className="muted">({rows.length} shown)</span></h3>
      <div className="filters">
        <select onChange={(e) => setF({ ...f, type: e.target.value })}>
          <option value="">all types</option>
          {["momentum", "contrarian", "news_reactive", "overconfident", "herd", "noise"].map((t) => <option key={t}>{t}</option>)}
        </select>
        <select onChange={(e) => setF({ ...f, status: e.target.value })}>
          <option value="">all status</option><option>active</option><option>paused</option><option>dead</option>
        </select>
        <select onChange={(e) => setF({ ...f, sort: e.target.value })}>
          <option value="pnl">sort: PnL</option><option value="equity">equity</option><option value="trades">trades</option>
        </select>
      </div>
      <div className="scroll" style={{ flex: 1 }}>
        <table>
          <thead><tr><th>bot</th><th>type</th><th>bankroll</th><th>PnL</th><th>risk</th><th>st</th></tr></thead>
          <tbody>
            {rows.map((b) => (
              <tr key={b.bot_id} onClick={() => onOpen(b.bot_id)}>
                <td>{b.bot_id}</td><td>{b.type}</td><td>{b.bankroll}</td>
                <td className={cls(b.pnl)}>{(b.pnl >= 0 ? "+" : "") + b.pnl}</td>
                <td>{b.risk_level}</td>
                <td><span className={"pill " + b.status}>{b.status}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
