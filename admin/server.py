"""
Admin server — stdlib ``http.server`` (ThreadingHTTPServer) exposing the control
REST API, a Server-Sent-Events live stream, and the dashboard. No third-party
deps. SSE (server→client) carries the high-frequency feed; controls are ordinary
POSTs (client→server). This is the same realtime shape the `bet` app uses.

Endpoints
  GET  /                      dashboard (static HTML)
  GET  /bots                  ?type=&status=&sort=&limit=   → bot table
  GET  /bot/{id}              full bot detail (+ equity curve, memory)
  GET  /market/state | /market/history | /market/events
  GET  /analytics             per-type behavioural analytics
  GET  /stream                SSE live feed (tick/market/trades/news/events)
  POST /bot/{id}/pause | resume | reset | update
  POST /bots/pause_all | resume_all
  POST /simulation/pause | resume | reset
  POST /simulation/config     {news_enabled,speed,volatility,liquidity,aggression_mult,chaos,mode}
  POST /simulation/stress     {on}        (bonus: global aggression)
  POST /simulation/replay     {path} | {action:"seek","idx":N}   (bonus)
"""
from __future__ import annotations

import json
import os
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .manager import LiveSimulation

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
SIM: LiveSimulation | None = None  # set by serve()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # quieter logging
    def log_message(self, fmt, *args):
        pass

    # -- helpers ----------------------------------------------------------- #
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            return self._send_json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def do_OPTIONS(self):  # CORS preflight (for a separate React dev server)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- GET --------------------------------------------------------------- #
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        sim = SIM

        if path == "/" or path == "/index.html":
            return self._send_file(os.path.join(STATIC_DIR, "dashboard.html"), "text/html; charset=utf-8")
        if path == "/stream":
            return self._stream()

        if path == "/bots":
            with sim._lock:
                rows = sim.bots_table(
                    type_filter=q.get("type", [None])[0],
                    status_filter=q.get("status", [None])[0],
                    sort=q.get("sort", ["pnl"])[0],
                    limit=int(q.get("limit", ["2000"])[0]),
                )
            return self._send_json({"bots": rows, "count": len(rows)})

        if path.startswith("/bot/"):
            bot_id = path[len("/bot/"):]
            with sim._lock:
                detail = sim.bot_detail(bot_id)
            return self._send_json(detail or {"error": "no such bot"}, 200 if detail else 404)

        if path == "/market/state":
            with sim._lock:
                return self._send_json(sim.market_state())
        if path == "/market/history":
            with sim._lock:
                return self._send_json({"history": sim.market_history()})
        if path == "/market/events":
            with sim._lock:
                return self._send_json({"events": sim.market_events()})
        if path == "/events":
            with sim._lock:
                return self._send_json({"events": sim.event_log()})
        if path == "/analytics":
            with sim._lock:
                return self._send_json(sim.analytics())

        return self._send_json({"error": "not found", "path": path}, 404)

    # -- POST -------------------------------------------------------------- #
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        sim = SIM

        with sim._lock:
            # --- per-bot controls ---
            if path.startswith("/bot/"):
                rest = path[len("/bot/"):]
                bot_id, _, action = rest.partition("/")
                bot = sim.bots_by_id.get(bot_id)
                if bot is None:
                    return self._send_json({"error": "no such bot"}, 404)
                if action == "pause":
                    if bot.status != "dead":
                        bot.status = "paused"
                elif action == "resume":
                    if bot.status != "dead":
                        bot.status = "active"
                elif action == "reset":
                    bot.reset_state()
                    sim._base[bot.id]["bias"] = bot.bias  # keep base in sync
                elif action == "update":
                    bot.set_params(**body)
                    # keep base snapshot consistent so global sliders compose
                    if "aggressiveness" in body:
                        sim._base[bot.id]["aggressiveness"] = bot.aggressiveness
                    if "bias" in body:
                        sim._base[bot.id]["bias"] = bot.bias
                else:
                    return self._send_json({"error": "unknown action"}, 404)
                return self._send_json({"ok": True, "bot": bot.snapshot(sim._price())})

            # --- bulk bot controls ---
            if path == "/bots/pause_all":
                for b in sim.bots:
                    if b.status == "active":
                        b.status = "paused"
                return self._send_json({"ok": True})
            if path == "/bots/resume_all":
                for b in sim.bots:
                    if b.status == "paused":
                        b.status = "active"
                return self._send_json({"ok": True})

            # --- simulation controls ---
            if path == "/simulation/pause":
                sim.running = False
                return self._send_json({"ok": True, "control": sim.control_snapshot()})
            if path == "/simulation/resume":
                sim.running = True
                return self._send_json({"ok": True, "control": sim.control_snapshot()})
            if path == "/simulation/reset":
                sim.reset_sim()
                return self._send_json({"ok": True, "control": sim.control_snapshot()})

            if path == "/simulation/config":
                if "news_enabled" in body:
                    sim.news_enabled = bool(body["news_enabled"])
                if "speed" in body:
                    sim.speed = max(0.2, min(60.0, float(body["speed"])))
                if "volatility" in body:
                    sim.volatility = max(0.0, float(body["volatility"]))
                    sim.apply_globals()
                if "liquidity" in body:
                    sim.set_liquidity(float(body["liquidity"]))
                if "aggression_mult" in body:
                    sim.aggression_mult = max(0.1, float(body["aggression_mult"]))
                    sim.apply_globals()
                if "chaos" in body:
                    sim.chaos = max(0.0, min(1.0, float(body["chaos"])))
                    sim.apply_globals()
                if "mode" in body and body["mode"] in ("live", "replay"):
                    sim.mode = body["mode"]
                return self._send_json({"ok": True, "control": sim.control_snapshot()})

            if path == "/simulation/stress":  # bonus: global aggression
                sim.aggression_mult = 2.5 if body.get("on", True) else 1.0
                sim.apply_globals()
                return self._send_json({"ok": True, "control": sim.control_snapshot()})

            if path == "/simulation/replay":  # bonus: replay-as-video
                action = body.get("action")
                if action == "seek":
                    sim.replay_seek(int(body.get("idx", 0)))
                elif action == "exit":
                    sim.mode = "live"
                else:
                    path_arg = body.get("path")
                    if not path_arg or not os.path.exists(path_arg):
                        return self._send_json({"error": "replay file not found"}, 400)
                    sim.load_replay(path_arg)
                return self._send_json({"ok": True, "control": sim.control_snapshot()})

        return self._send_json({"error": "not found", "path": path}, 404)

    # -- SSE --------------------------------------------------------------- #
    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = SIM.subscribe()
        try:
            self._sse(self.wfile, {"type": "hello", "control": SIM.control_snapshot()})
            while True:
                try:
                    payload = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._sse(self.wfile, payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            SIM.unsubscribe(q)

    @staticmethod
    def _sse(wfile, payload: dict):
        wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
        wfile.flush()


def serve(config_path: str, host: str = "127.0.0.1", port: int = 8080, autostart: bool = True):
    global SIM
    SIM = LiveSimulation(config_path)
    SIM.running = autostart
    SIM.start_thread()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Bot Admin Panel → http://{host}:{port}   (config: {config_path}, {len(SIM.bots)} bots)")
    print("  REST: /bots /bot/{id} /market/* /analytics   SSE: /stream")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SIM.shutdown()
        httpd.server_close()
