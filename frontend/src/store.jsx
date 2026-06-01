// Single source of truth for live state. One EventSource feeds this reducer
// (see useLiveSocket.js); components read via the `useStore` hook and mutate the
// world through api.js (never by writing here directly). This keeps a clean
// one-way data flow: SSE → reducer → UI, controls → REST → SSE.
import React, { createContext, useContext, useReducer } from "react";

const MAX = 180;
const initial = {
  connected: false,
  control: {},
  market: {},
  price: [],
  sentiment: [],
  volume: [],
  events: [], // newest first
  trades: [], // newest first
  types: [],
};

function reducer(s, a) {
  switch (a.type) {
    case "open":
      return { ...s, connected: true };
    case "close":
      return { ...s, connected: false };
    case "hello":
      return { ...s, control: a.payload.control || s.control };
    case "tick": {
      const p = a.payload;
      const m = p.market;
      const push = (arr, v) => [...arr, v].slice(-MAX);
      return {
        ...s,
        control: p.control || s.control,
        market: m,
        types: p.types && p.types.length ? p.types : s.types,
        price: push(s.price, m.price),
        sentiment: push(s.sentiment, m.sentiment ? m.sentiment.positive : 0),
        volume: push(s.volume, m.volume || 0),
        events: [...(p.events || []).reverse(), ...s.events].slice(0, 120),
        trades: [...(p.trades || []).slice(0, 8), ...s.trades].slice(0, 120),
      };
    }
    case "resetSeries":
      return { ...s, price: [], sentiment: [], volume: [] };
    default:
      return s;
  }
}

const Ctx = createContext(null);
export function StoreProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initial);
  return <Ctx.Provider value={{ state, dispatch }}>{children}</Ctx.Provider>;
}
export const useStore = () => useContext(Ctx);
