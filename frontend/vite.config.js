import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Proxy the admin API (and the SSE stream) to the Python server on :8080, so the
// React app can use same-origin paths in dev. `python run_admin.py` must be up.
const target = "http://127.0.0.1:8080";
const api = ["/bots", "/bot", "/market", "/analytics", "/events", "/simulation", "/stream"];

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(api.map((p) => [p, { target, changeOrigin: true }])),
  },
});
