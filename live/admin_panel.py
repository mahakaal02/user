"""
Local-only fleet admin panel.

A small control surface for the LIVE bot fleet, served over stdlib HTTP. It:
  * DISPLAYS which markets the fleet is trading, each with its market URL, and
  * lets an operator PASTE a market URL to add that market to the fleet's active
    set (the bots then trade it alongside the auto-discovered markets — without
    stopping work on the others).

SAFETY (by design): the "add market" endpoint accepts **local** URLs ONLY —
localhost / loopback / private-network / ``*.local``. Any public host, including
kalki.bet, is rejected. This is the sandbox build: it cannot be pointed at a
production market. The display is read-only. The server binds to 127.0.0.1.
"""
from __future__ import annotations

import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def is_local_url(url: str) -> bool:
    """True only for http(s) URLs whose host is loopback / private / link-local /
    ``*.local`` / ``localhost``. A public hostname (e.g. kalki.bet) returns False."""
    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.lower()
    if host in ("localhost", "ip6-localhost"):
        return True
    if host.endswith(".local") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False  # a real (public) hostname is NOT local → reject


def slug_from_url(url: str) -> str:
    """Last path segment of the URL (the market slug)."""
    path = urlparse((url or "").strip()).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


class FleetAdminPanel:
    """Serves the panel HTML/JSON and bridges it to a running ``FleetRunner``."""

    def __init__(self, runner, base_url: str, host: str = "127.0.0.1", port: int = 8090,
                 market_url_template: str = "{base}/en/markets/{slug}") -> None:
        self.runner = runner
        self.base_url = (base_url or "").rstrip("/")
        self.host = host
        self.port = port
        # base_url already includes the app basePath (…/markets); the page route is
        # /{locale}/markets/{slug}. Configurable in case the deploy differs.
        self.market_url_template = market_url_template
        self._httpd = None

    # -- bridge helpers ---------------------------------------------------- #
    def market_url(self, market: dict) -> str:
        slug = market.get("slug") or market.get("id")
        return self.market_url_template.format(base=self.base_url, slug=slug)

    def resolve(self, url: str) -> dict | None:
        """Resolve a market URL → its live market dict by matching the slug."""
        slug = slug_from_url(url)
        if not slug:
            return None
        for m in self.runner.client.list_markets():
            if m.get("slug") == slug or m.get("id") == slug:
                return m
        return None

    def add_market(self, url: str) -> dict:
        if not is_local_url(url):
            return {"ok": False, "error": "rejected — only local/loopback URLs are accepted "
                                          "(public hosts like kalki.bet are blocked by design)"}
        if self.runner is None:
            return {"ok": False, "error": "fleet not ready yet — try again once it has connected"}
        m = self.resolve(url)
        if not m:
            return {"ok": False, "error": "no matching open market on the configured instance"}
        added = self.runner.inject_market(m)
        return {"ok": True, "added": added, "id": m["id"], "title": m.get("title", ""),
                "note": "added to the fleet's active set" if added else "already active"}

    def state(self) -> dict:
        if self.runner is None:
            return {"base_url": self.base_url, "status": "fleet starting…",
                    "markets": [], "stats": {}, "bots": 0}
        markets = self.runner.active_markets()
        for m in markets:
            m["url"] = self.market_url(m)
        return {"base_url": self.base_url, "status": "running",
                "markets": markets, "stats": dict(self.runner.stats), "bots": len(self.runner.bots)}

    # -- server ------------------------------------------------------------ #
    def serve_in_thread(self) -> None:
        panel = self

        class _H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _send(self, body: bytes, ct: str = "application/json", code: int = 200) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/") in ("", "/"):
                    return self._send(_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                if self.path.startswith("/state"):
                    return self._send(json.dumps(panel.state()).encode("utf-8"))
                return self._send(b'{"error":"not found"}', code=404)

            def do_POST(self):
                if self.path.rstrip("/") == "/markets/add":
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    try:
                        body = json.loads(self.rfile.read(n) or b"{}")
                    except ValueError:
                        body = {}
                    return self._send(json.dumps(panel.add_market(body.get("url", ""))).encode("utf-8"))
                return self._send(b'{"error":"not found"}', code=404)

        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), _H)
        except OSError as e:
            print(f"  !! fleet admin panel could NOT bind {self.host}:{self.port} ({e}) — "
                  f"is it already running? try --admin <other-port>")
            return
        threading.Thread(target=self._httpd.serve_forever, name="fleet-admin", daemon=True).start()
        print(f"  fleet admin panel → http://{self.host}:{self.port}  "
              f"(display + LOCAL-only market injection)")


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Fleet admin (local)</title>
<style>
 body{font:14px system-ui,Segoe UI,Roboto,sans-serif;margin:2rem;background:#0b0e14;color:#cdd6f4}
 h2 span{font-size:12px;vertical-align:middle}
 table{border-collapse:collapse;width:100%;margin-top:8px}
 td,th{padding:6px 10px;border-bottom:1px solid #313244;text-align:left}
 a{color:#89b4fa;word-break:break-all} code{color:#94e2d5}
 .pill{font-size:11px;padding:1px 7px;border-radius:9px;background:#313244;color:#cdd6f4}
 .inj{background:#a6e3a1;color:#11111b}
 input{width:58%;padding:8px;background:#181825;color:#cdd6f4;border:1px solid #313244;border-radius:6px}
 button{padding:8px 14px;background:#89b4fa;color:#11111b;border:0;border-radius:6px;cursor:pointer}
 .warn{color:#fab387;font-size:12px;margin:6px 0} #msg{margin-left:10px;font-size:13px}
</style></head><body>
<h2>Fleet admin <span class="pill">local-only</span></h2>
<div class="warn">Paste a market URL on your LOCAL instance to add it to the fleet's active set.
Public hosts (e.g. kalki.bet) are rejected by design; the bots keep trading the other markets.</div>
<p>instance <code id="base">—</code> · <b id="status">connecting…</b> · bots <b id="bots">0</b> · trades <b id="trades">0</b> · rejects <b id="rejects">0</b></p>
<form id="f"><input id="url" placeholder="http://localhost:3100/markets/en/markets/&lt;slug&gt;"><button>Add market</button><span id="msg"></span></form>
<h3>Markets the fleet is trading</h3>
<table><thead><tr><th>Market</th><th>YES</th><th>URL</th><th></th></tr></thead><tbody id="rows"></tbody></table>
<script>
async function refresh(){
  try{
    const s = await (await fetch('/state')).json();
    base.textContent = s.base_url; bots.textContent = s.bots; status.textContent = s.status||'running';
    trades.textContent = (s.stats.trades||0); rejects.textContent = (s.stats.rejects||0);
    rows.innerHTML = (s.markets||[]).map(m =>
      `<tr><td>${m.title||m.id}</td><td>${m.yesPrice??'—'}</td>`+
      `<td><a href="${m.url}" target="_blank" rel="noopener">${m.url}</a></td>`+
      `<td>${m.injected?'<span class="pill inj">added</span>':''}</td></tr>`).join('');
  }catch(e){ /* fleet not ready yet */ }
}
f.onsubmit = async e => {
  e.preventDefault(); msg.textContent = '…';
  const r = await (await fetch('/markets/add',{method:'POST',headers:{'Content-Type':'application/json'},
            body: JSON.stringify({url: url.value.trim()})})).json();
  msg.textContent = r.ok ? ('✓ '+(r.added?'added':'already active')+': '+(r.title||r.id)) : ('✗ '+r.error);
  if(r.ok){ url.value=''; refresh(); }
};
refresh(); setInterval(refresh, 2000);
</script></body></html>"""
