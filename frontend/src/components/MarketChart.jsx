import React from "react";
import { useStore } from "../store.jsx";
import { linePath, bars } from "../chart.js";

const Kpi = ({ v, l, cls }) => (
  <div className="kpi"><div className={"v " + (cls || "")}>{v}</div><div className="l">{l}</div></div>
);

export default function MarketChart() {
  const { state } = useStore();
  const m = state.market || {};
  const W = 600, H = 180;
  const volHi = Math.max(...state.volume, 1);
  const mid = H - 4 - 0.5 * (H - 8);
  const fmt = (n) => (n >= 0 ? "+" : "") + (n ?? 0).toFixed(1);
  return (
    <div className="card">
      <h3>Market — YES probability <span className="muted">{m.price != null ? `= ${(m.price * 100).toFixed(1)}%` : ""}</span></h3>
      <div className="kpis" style={{ marginBottom: 8 }}>
        <Kpi v={m.price != null ? m.price.toFixed(3) : "–"} l="YES price" cls={m.price >= 0.5 ? "pos" : "neg"} />
        <Kpi v={Math.round(m.volume || 0)} l="volume/tick" />
        <Kpi v={fmt(m.net_flow)} l="net flow" cls={m.net_flow >= 0 ? "pos" : "neg"} />
        <Kpi v={m.sentiment ? "+" + m.sentiment.positive.toFixed(2) : "–"} l="sentiment" />
        <Kpi v={m.liquidity ? Math.round(m.liquidity / 1000) + "k" : "–"} l="liquidity" />
      </div>
      <svg height="180" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        {bars(state.volume, W, H, volHi).map((b, i) => (
          <rect key={i} x={b.x} y={b.y} width={b.w} height={b.h} fill="var(--yel)" opacity="0.25" />
        ))}
        <line x1="0" y1={mid} x2={W} y2={mid} stroke="#243044" strokeDasharray="3 4" />
        <path d={linePath(state.sentiment, W, H, 0, 1)} fill="none" stroke="var(--pur)" strokeWidth="1" opacity="0.7" />
        <path d={linePath(state.price, W, H, 0, 1)} fill="none" stroke="var(--acc)" strokeWidth="2" />
      </svg>
      <div className="legend">
        <span><i className="sw" style={{ background: "var(--acc)" }} />price</span>
        <span><i className="sw" style={{ background: "var(--pur)" }} />sentiment(+)</span>
        <span><i className="sw" style={{ background: "var(--yel)" }} />volume</span>
      </div>
    </div>
  );
}
