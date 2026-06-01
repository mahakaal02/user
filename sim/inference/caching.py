"""
Per-tick caching wrapper — the mechanical enforcement of "shared inference".

HARD CONSTRAINT: no per-bot model inference. With 1000 bots that could mean 1000
identical calls per tick. This wrapper memoizes by (method, hash(input)) within a
single tick, so even if many callers ask for the sentiment of the same headline,
exactly ONE upstream call is made. The signal layer computes inference once per
tick anyway; this is the belt-and-braces guarantee, and ``upstream_calls`` lets
the engine *prove* the constraint held (it logs calls-per-tick).

Scope is reset every tick via :meth:`new_tick`, so memory stays O(distinct
inputs per tick), not O(history).
"""
from __future__ import annotations

import hashlib

from .base import InferenceClient


def _key(method: str, text: str) -> str:
    return f"{method}:{hashlib.blake2b((text or '').encode('utf-8'), digest_size=12).hexdigest()}"


class CachingInferenceClient(InferenceClient):
    def __init__(self, inner: InferenceClient, scope: str = "tick") -> None:
        self._inner = inner
        self._scope = scope
        self._cache: dict[str, dict] = {}
        self._upstream = 0
        self.name = f"cached({inner.name})"

    def new_tick(self, tick: int) -> None:
        if self._scope == "tick":
            self._cache.clear()
        self._inner.new_tick(tick)

    def _memo(self, method: str, text: str) -> dict:
        k = _key(method, text)
        hit = self._cache.get(k)
        if hit is not None:
            return hit
        self._upstream += 1
        result = getattr(self._inner, method)(text)
        self._cache[k] = result
        return result

    def sentiment(self, text: str) -> dict:
        return self._memo("sentiment", text)

    def event_extract(self, text: str) -> dict:
        return self._memo("event_extract", text)

    def reasoning(self, prompt: str) -> dict:
        return self._memo("reasoning", prompt)

    @property
    def upstream_calls(self) -> int:
        return self._upstream
