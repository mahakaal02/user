"""
Remote HTTP inference adapters: FinBERT (sentiment) and Qwen (reasoning/events).

Both speak plain JSON over HTTP using only the standard library (``urllib``), so
there is no third-party client dependency. Each adapter:

  * reads its URL/timeout from config (NEVER hardcoded, NEVER imported by bots),
  * POSTs a small JSON body to the friend's machine / inference server,
  * normalizes whatever comes back into the canonical schema.

The expected server contract is documented in ``inference_server/server.py``
(a runnable FastAPI reference) and in the README. Because the adapter normalizes
defensively, a server that returns slightly different keys still works.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import InferenceClient, normalize_event, normalize_sentiment


class RemoteInferenceError(RuntimeError):
    pass


def _post_json(url: str, payload: dict, timeout_s: float, retries: int, backoff_s: float) -> dict:
    """POST ``payload`` as JSON and return the parsed JSON response.

    Retries with linear backoff on network/5xx errors. Raises
    :class:`RemoteInferenceError` if every attempt fails.
    """
    data = json.dumps(payload).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            last_err = exc
            if attempt < retries:
                # Deterministic backoff (no jitter) so behaviour is reproducible.
                _sleep(backoff_s * (attempt + 1))
    raise RemoteInferenceError(f"POST {url} failed after {retries + 1} attempts: {last_err}")


def _sleep(seconds: float) -> None:
    import time

    time.sleep(max(0.0, seconds))


class _RemoteBase(InferenceClient):
    def __init__(
        self,
        url: str,
        timeout_s: float = 10.0,
        retries: int = 2,
        backoff_s: float = 0.25,
        fallback: InferenceClient | None = None,
    ) -> None:
        if not url:
            raise ValueError(f"{type(self).__name__} requires a 'url' in config")
        self.url = url
        self.timeout_s = float(timeout_s)
        self.retries = int(retries)
        self.backoff_s = float(backoff_s)
        # Optional graceful degradation: if the remote box is unreachable, fall
        # back to a local client instead of crashing the whole simulation.
        self.fallback = fallback

    def _call(self, payload: dict) -> dict:
        return _post_json(self.url, payload, self.timeout_s, self.retries, self.backoff_s)


class FinBERTAPIClient(_RemoteBase):
    """Remote FinBERT sentiment service.

    Server contract::

        POST {url}
        {"text": "<headline>"}
        ->  {"positive": .., "negative": .., "neutral": .., "confidence": ..}
            (or HuggingFace-style [{"label":"positive","score":..}, ...])
    """

    name = "finbert_api"

    def sentiment(self, text: str) -> dict:
        try:
            raw = self._call({"text": text})
        except RemoteInferenceError:
            if self.fallback:
                return self.fallback.sentiment(text)
            raise
        return normalize_sentiment(_coerce_finbert(raw))

    # FinBERT is sentiment-only; route reasoning/events elsewhere via the
    # composite client. These exist so the interface is total.
    def event_extract(self, text: str) -> dict:
        if self.fallback:
            return self.fallback.event_extract(text)
        raise NotImplementedError("FinBERT backend does not do event extraction")

    def reasoning(self, prompt: str) -> dict:
        if self.fallback:
            return self.fallback.reasoning(prompt)
        raise NotImplementedError("FinBERT backend does not do reasoning")


class QwenAPIClient(_RemoteBase):
    """Remote Qwen reasoning/event server (an LLM behind an HTTP endpoint).

    Server contract::

        POST {url}
        {"task": "reasoning"|"event"|"sentiment", "prompt"|"text": "..."}
        ->  reasoning/event: {"event":..,"impact":{..},"confidence":..}
            sentiment:        {"positive":..,"negative":..,"neutral":..,"confidence":..}

    A real implementation prompts Qwen to emit strict JSON; the adapter parses
    and normalizes it. ``_coerce_json`` tolerates the model wrapping JSON in
    prose or markdown fences.
    """

    name = "qwen_api"

    def reasoning(self, prompt: str) -> dict:
        try:
            raw = self._call({"task": "reasoning", "prompt": prompt})
        except RemoteInferenceError:
            if self.fallback:
                return self.fallback.reasoning(prompt)
            raise
        return normalize_event(_coerce_json(raw))

    def event_extract(self, text: str) -> dict:
        try:
            raw = self._call({"task": "event", "text": text})
        except RemoteInferenceError:
            if self.fallback:
                return self.fallback.event_extract(text)
            raise
        return normalize_event(_coerce_json(raw))

    def sentiment(self, text: str) -> dict:
        try:
            raw = self._call({"task": "sentiment", "text": text})
        except RemoteInferenceError:
            if self.fallback:
                return self.fallback.sentiment(text)
            raise
        return normalize_sentiment(_coerce_json(raw))


# --------------------------------------------------------------------------- #
# Response coercion helpers — tolerate the common real-world response shapes.
# --------------------------------------------------------------------------- #
def _coerce_finbert(raw) -> dict:
    """Accept either the canonical dict or HuggingFace pipeline list output."""
    if isinstance(raw, dict) and "positive" in raw:
        return raw
    if isinstance(raw, list):  # [{"label":"positive","score":0.8}, ...]
        out = {}
        for item in raw:
            label = str(item.get("label", "")).lower()
            score = item.get("score", 0.0)
            if "pos" in label:
                out["positive"] = score
            elif "neg" in label:
                out["negative"] = score
            elif "neu" in label:
                out["neutral"] = score
        out.setdefault("confidence", max(out.values(), default=0.0))
        return out
    return {}


def _coerce_json(raw) -> dict:
    """Qwen may return a dict already, or a string containing JSON (possibly in
    a ```json fence). Extract the first JSON object we can find."""
    if isinstance(raw, dict) and ("event" in raw or "impact" in raw or "positive" in raw):
        return raw
    text = raw.get("text") if isinstance(raw, dict) else raw
    if not isinstance(text, str):
        return raw if isinstance(raw, dict) else {}
    s = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(s)
    except ValueError:
        start, end = s.find("{"), s.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(s[start : end + 1])
            except ValueError:
                return {}
        return {}
