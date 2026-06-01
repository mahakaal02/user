import React from "react";
import { useStore } from "../store.jsx";

export default function EventFeed() {
  const { state } = useStore();
  return (
    <>
      <div className="card" style={{ flex: 1 }}>
        <h3>Live event feed</h3>
        <div className="scroll feed" style={{ flex: 1 }}>
          {state.events.map((e, i) => (
            <div key={i} className={"row " + e.kind}>
              <span className="muted">t{e.tick}</span>{" "}
              {e.kind === "news" ? <>📰 <b>{e.text}</b></> : e.kind === "price" ? <>📈 {e.text}</> : <>🤖 {e.text}</>}
            </div>
          ))}
        </div>
      </div>
      <div className="card" style={{ flex: 1 }}>
        <h3>Trades stream <span className="muted">{state.market.trades || 0} fills/tick</span></h3>
        <div className="scroll feed" style={{ flex: 1 }}>
          {state.trades.map((tr, i) => {
            const dir = (tr.side === "BUY" && tr.outcome === "YES") || (tr.side === "SELL" && tr.outcome === "NO");
            return (
              <div key={i} className="row trade">
                <span className="muted">{tr.bot_id}</span>{" "}
                <b className={dir ? "pos" : "neg"}>{tr.side} {tr.outcome}</b> {tr.shares}@{tr.price}
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}
