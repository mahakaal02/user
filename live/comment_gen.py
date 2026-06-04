"""
One-liner comment generation.

Two pluggable backends (selected in config, mirroring the inference layer):
  * ``llm``      — ask a Qwen text endpoint for the line. Two protocols, picked
                   automatically: an OpenAI-compatible chat endpoint (an api_key/
                   model is set, or the URL is ``.../chat/completions`` — e.g. a
                   hosted Qwen on PodStack) is called with Bearer auth + a chat
                   completion; otherwise the legacy ``{task:"comment", prompt}``
                   custom server (inference_server/server.py) is used.
  * ``template`` — offline, deterministic human-like one-liners. The default so
                   the fleet runs with no model; also the fallback if the LLM
                   call fails.

The comment reflects the bot's actual stance (side + outcome + conviction) and
the market, so it reads like a real retail trader reacting to their own trade.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from sim.inference.openai_chat import OpenAIChatClient, OpenAIChatError, is_openai_style

# Human-like retail one-liners (lowercase, casual — like real comment sections).
_BULL = [
    "buying YES here, momentum looks strong",
    "feeling positive about YES on this one 📈",
    "loaded up YES, looks undervalued to me",
    "YES all day, the news is clearly bullish",
    "in on YES, this is heading up",
    "easy YES, market's underpricing this",
    "adding YES, sentiment's turning",
    "YES looks like free money rn ngl",
]
_BULL_STRONG = [
    "going big on YES, this is a lock 🚀",
    "all in YES, no way this resolves NO",
    "YES YES YES, conviction trade for me",
    "backing up the truck on YES here",
]
_BEAR = [
    "taking NO, this looks overpriced",
    "fading the hype, NO for me",
    "selling into this, not convinced on YES",
    "NO here, market's too optimistic",
    "trimming, this YES rally looks tired",
    "betting NO, the numbers don't add up",
    "out of YES, taking profit",
    "NO looks like the value side tbh",
]
_BEAR_STRONG = [
    "shorting this hard, NO is the play 📉",
    "big NO, this is way overbought",
    "dumping YES, this reverses soon",
    "NO with size, hype's done here",
]


class CommentGenerator:
    def __init__(self, backend: str = "template", qwen_url: str | None = None,
                 timeout_s: float = 20.0, qwen_api_key: str | None = None,
                 qwen_model: str | None = None) -> None:
        self.backend = backend
        self.qwen_url = qwen_url
        self.qwen_api_key = qwen_api_key or ""
        self.qwen_model = qwen_model or ""
        self.timeout = timeout_s

    def generate(self, action: str, outcome: str, conviction: float, market_title: str, rng) -> str:
        bullish = (action == "BUY" and outcome == "YES") or (action == "SELL" and outcome == "NO")
        if self.backend == "llm" and self.qwen_url:
            line = self._llm(bullish, conviction, market_title)
            if line:
                return line[:200]
        return self._template(bullish, conviction, rng)

    def _template(self, bullish: bool, conviction: float, rng) -> str:
        strong = conviction >= 0.6
        if bullish:
            pool = _BULL_STRONG if strong else _BULL
        else:
            pool = _BEAR_STRONG if strong else _BEAR
        return rng.choice(pool)

    def _llm(self, bullish: bool, conviction: float, title: str) -> str | None:
        stance = "bullish (betting YES)" if bullish else "bearish (betting NO)"
        prompt = (
            f"You are a retail prediction-market trader reacting to your own trade. "
            f"Market: '{title}'. Your stance: {stance}, conviction {conviction:.0%}. "
            f"Write ONE short, casual comment (max 12 words), lowercase, no hashtags, no quotes."
        )
        if is_openai_style(self.qwen_url, self.qwen_api_key):
            return self._llm_openai(prompt)
        return self._llm_custom(prompt)

    def _llm_openai(self, prompt: str) -> str | None:
        """OpenAI-compatible chat endpoint (e.g. PodStack)."""
        try:
            text = OpenAIChatClient(
                url=self.qwen_url, api_key=self.qwen_api_key, model=self.qwen_model,
                timeout_s=self.timeout, retries=1, temperature=0.8, max_tokens=40,
            ).chat("You write short, casual, lowercase retail-trader comments.", prompt)
            text = text.strip().strip('"').strip()
            return text or None
        except (OpenAIChatError, ValueError):
            return None

    def _llm_custom(self, prompt: str) -> str | None:
        """Legacy custom server: POST {task:"comment", prompt} → {"text": ...}."""
        try:
            data = json.dumps({"task": "comment", "prompt": prompt}).encode("utf-8")
            req = urllib.request.Request(
                self.qwen_url, data=data, method="POST", headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read() or b"{}")
            text = resp.get("text") or resp.get("comment") or resp.get("output")
            return text.strip() if isinstance(text, str) and text.strip() else None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
            return None
