// The single live connection. Opens EventSource('/stream') and dispatches every
// tick into the store. EventSource auto-reconnects, so there's no manual retry.
import { useEffect } from "react";
import { useStore } from "./store.jsx";

export function useLiveSocket() {
  const { dispatch } = useStore();
  useEffect(() => {
    const es = new EventSource("/stream");
    es.onopen = () => dispatch({ type: "open" });
    es.onerror = () => dispatch({ type: "close" });
    es.onmessage = (ev) => {
      const payload = JSON.parse(ev.data);
      if (payload.type === "hello") dispatch({ type: "hello", payload });
      else if (payload.type === "tick") dispatch({ type: "tick", payload });
    };
    return () => es.close();
  }, [dispatch]);
}
