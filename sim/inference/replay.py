"""
Replay inference backend — deterministic re-runs even with remote models.

Determinism with a live LLM is impossible (sampling, load, network). The fix:
record every inference result the first time, then replay it. The engine's
recorder writes one ``signals`` row per tick; ``ReplayInferenceClient`` reads
those rows back and serves the exact sentiment/event payloads that were seen,
keyed by the input text. This gives bit-for-bit replay of a run that originally
used FinBERT + Qwen, without calling them again.
"""
from __future__ import annotations

import hashlib
import json

from .base import InferenceClient, normalize_event, normalize_sentiment


def _h(text: str) -> str:
    return hashlib.blake2b((text or "").encode("utf-8"), digest_size=12).hexdigest()


class ReplayInferenceClient(InferenceClient):
    name = "replay"

    def __init__(self, recording_path: str) -> None:
        self._sent: dict[str, dict] = {}
        self._event: dict[str, dict] = {}
        self._reason: dict[str, dict] = {}
        self._load(recording_path)

    def _load(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                for rec in row.get("inference", []):
                    text = rec.get("input", "")
                    if "sentiment" in rec:
                        self._sent[_h(text)] = rec["sentiment"]
                    if "event" in rec:
                        self._event[_h(text)] = rec["event"]
                    if "reasoning" in rec:
                        self._reason[_h(text)] = rec["reasoning"]

    # Return recorded payloads VERBATIM (they were already normalized when
    # recorded). Re-normalizing here would shift the 6th decimal and compound
    # into divergence over a long run, breaking bit-for-bit replay. Only a
    # genuine cache miss falls back to a normalized default.
    def sentiment(self, text: str) -> dict:
        v = self._sent.get(_h(text))
        return dict(v) if v is not None else normalize_sentiment(None)

    def event_extract(self, text: str) -> dict:
        v = self._event.get(_h(text))
        return dict(v) if v is not None else normalize_event(None)

    def reasoning(self, prompt: str) -> dict:
        v = self._reason.get(_h(prompt))
        return dict(v) if v is not None else normalize_event(None)
