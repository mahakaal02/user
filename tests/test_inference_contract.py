"""Inference contract: every backend returns the standardized schema; the
composite routes per capability; the cache deduplicates within a tick.
Run: python tests/test_inference_contract.py"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.inference import build_inference_client  # noqa: E402
from sim.inference.base import normalize_event, normalize_sentiment  # noqa: E402
from sim.inference.local_heuristic import LocalHeuristicClient  # noqa: E402


def test_sentiment_schema_is_normalized():
    s = LocalHeuristicClient().sentiment("Markets surge on surprise rate cut")
    assert set(s) == {"positive", "negative", "neutral", "confidence"}
    assert abs(s["positive"] + s["negative"] + s["neutral"] - 1.0) < 1e-6
    assert 0.0 <= s["confidence"] <= 1.0


def test_event_schema_is_normalized():
    e = LocalHeuristicClient().event_extract("Central bank announces a rate cut")
    assert set(e) == {"event", "impact", "confidence"}
    assert isinstance(e["impact"], dict)
    assert e["event"] == "interest_rate_cut"


def test_normalizers_tolerate_garbage():
    s = normalize_sentiment({"positive": "nonsense", "negative": None})
    assert abs(sum([s["positive"], s["negative"], s["neutral"]]) - 1.0) < 1e-6
    e = normalize_event({"impact": {"market_up": 5.0}})  # out-of-range clamps to 1
    assert e["impact"]["market_up"] == 1.0


def test_cache_dedupes_within_a_tick():
    client = build_inference_client(
        {
            "sentiment_backend": "local_heuristic",
            "event_backend": "local_heuristic",
            "reasoning_backend": "local_heuristic",
            "cache": {"enabled": True, "scope": "tick"},
        }
    )
    client.new_tick(0)
    for _ in range(50):
        client.sentiment("identical headline")  # 50 calls, same input
    assert client.upstream_calls == 1, client.upstream_calls
    client.new_tick(1)  # new tick resets the memo
    client.sentiment("identical headline")
    assert client.upstream_calls == 2


def test_composite_routes_each_capability():
    # Route sentiment and reasoning to (here) the same local backend, but prove
    # the composite dispatches all three methods without error and normalized.
    client = build_inference_client(
        {
            "sentiment_backend": "local_heuristic",
            "event_backend": "local_heuristic",
            "reasoning_backend": "local_heuristic",
            "cache": {"enabled": False},
        }
    )
    assert set(client.sentiment("rally")) == {"positive", "negative", "neutral", "confidence"}
    assert set(client.reasoning("state: price 0.6")) == {"event", "impact", "confidence"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("Inference contract: all passed")
