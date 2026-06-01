"""
Build the inference client from config — the single place backends are chosen.

``config.yaml`` names a backend per *capability*::

    inference:
      sentiment_backend: finbert_api    # who answers .sentiment()
      event_backend:     qwen_api       # who answers .event_extract()
      reasoning_backend: qwen_api       # who answers .reasoning()

The :class:`CompositeInferenceClient` routes each method to the chosen backend
instance, so you can mix FinBERT for sentiment with Qwen for reasoning, or point
all three at one local backend — without touching a line of bot code. The whole
thing is then wrapped in the per-tick cache.
"""
from __future__ import annotations

from .base import InferenceClient
from .caching import CachingInferenceClient
from .local_heuristic import LocalHeuristicClient
from .remote import FinBERTAPIClient, QwenAPIClient
from .replay import ReplayInferenceClient


def _build_backend(name: str, params: dict, fallback: InferenceClient | None) -> InferenceClient:
    """Instantiate a single backend by its ``type`` (defaults to its key name)."""
    btype = (params or {}).get("type", name)
    if btype == "local_heuristic":
        return LocalHeuristicClient()
    if btype == "replay":
        return ReplayInferenceClient(params["recording"])
    if btype == "finbert_api":
        return FinBERTAPIClient(
            url=params.get("url", ""),
            timeout_s=params.get("timeout_s", 10.0),
            retries=params.get("retries", 2),
            backoff_s=params.get("backoff_s", 0.25),
            fallback=fallback,
        )
    if btype == "qwen_api":
        return QwenAPIClient(
            url=params.get("url", ""),
            timeout_s=params.get("timeout_s", 20.0),
            retries=params.get("retries", 2),
            backoff_s=params.get("backoff_s", 0.5),
            fallback=fallback,
        )
    raise ValueError(f"Unknown inference backend type: {btype!r} (for '{name}')")


class CompositeInferenceClient(InferenceClient):
    """Routes each capability to a (possibly different) backend instance."""

    name = "composite"

    def __init__(
        self,
        sentiment_backend: InferenceClient,
        event_backend: InferenceClient,
        reasoning_backend: InferenceClient,
    ) -> None:
        self._s = sentiment_backend
        self._e = event_backend
        self._r = reasoning_backend

    def sentiment(self, text: str) -> dict:
        return self._s.sentiment(text)

    def event_extract(self, text: str) -> dict:
        return self._e.event_extract(text)

    def reasoning(self, prompt: str) -> dict:
        return self._r.reasoning(prompt)

    def new_tick(self, tick: int) -> None:
        for b in {id(self._s): self._s, id(self._e): self._e, id(self._r): self._r}.values():
            b.new_tick(tick)


def build_inference_client(cfg: dict) -> InferenceClient:
    """Construct the configured, cached, composite inference client.

    ``cfg`` is the ``inference:`` section of the parsed config (a plain dict).
    """
    # A shared local fallback keeps the sim alive if a remote box is down.
    fallback = LocalHeuristicClient() if cfg.get("remote_fallback_local", True) else None

    # Instantiate each named backend once; reuse the same instance if two
    # capabilities point at the same backend name (so the cache/conn is shared).
    backends_cfg = {k: v for k, v in cfg.items() if isinstance(v, dict) and ("type" in v or k.endswith("_api") or k in ("local_heuristic", "replay"))}
    instances: dict[str, InferenceClient] = {}

    def get(name: str) -> InferenceClient:
        if name not in instances:
            params = backends_cfg.get(name, {"type": name})
            fb = None if params.get("type", name) in ("local_heuristic", "replay") else fallback
            instances[name] = _build_backend(name, params, fb)
        return instances[name]

    sentiment = get(cfg.get("sentiment_backend", "local_heuristic"))
    event = get(cfg.get("event_backend", cfg.get("reasoning_backend", "local_heuristic")))
    reasoning = get(cfg.get("reasoning_backend", "local_heuristic"))

    client: InferenceClient = CompositeInferenceClient(sentiment, event, reasoning)

    cache_cfg = cfg.get("cache", {"enabled": True})
    if cache_cfg.get("enabled", True):
        client = CachingInferenceClient(client, scope=cache_cfg.get("scope", "tick"))
    return client
