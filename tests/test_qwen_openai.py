"""OpenAI-compatible Qwen adapter (e.g. PodStack): the chat transport speaks the
right wire shape (Bearer auth + model + messages), parses choices, normalizes
every capability to the canonical schema, and falls back to local on failure.
No network — urlopen is stubbed. Run: python tests/test_qwen_openai.py"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.inference.local_heuristic import LocalHeuristicClient  # noqa: E402
from sim.inference.openai_chat import (  # noqa: E402
    OpenAIChatClient,
    OpenAIChatError,
    is_openai_style,
)
from sim.inference.remote import QwenChatClient  # noqa: E402


# --------------------------------------------------------------------------- #
#  A tiny urlopen stub that records the last request and returns a canned body.
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._b = body

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Stub:
    """Context-managed monkeypatch of urllib.request.urlopen."""

    def __init__(self, content=None, *, raise_url=False, raw_body=None) -> None:
        self.content = content
        self.raise_url = raise_url
        self.raw_body = raw_body
        self.last_req = None
        self._orig = None

    def __enter__(self):
        self._orig = urllib.request.urlopen

        def fake(req, timeout=None):
            self.last_req = req
            if self.raise_url:
                raise urllib.error.URLError("stubbed network down")
            if self.raw_body is not None:
                return _FakeResp(self.raw_body)
            body = {"choices": [{"message": {"content": self.content}}],
                    "usage": {"total_tokens": 7}}
            return _FakeResp(json.dumps(body).encode("utf-8"))

        urllib.request.urlopen = fake
        return self

    def __exit__(self, *a):
        urllib.request.urlopen = self._orig
        return False

    def headers(self) -> dict:
        return {k.lower(): v for k, v in (self.last_req.headers or {}).items()}


# --------------------------------------------------------------------------- #
#  Tests
# --------------------------------------------------------------------------- #
def test_is_openai_style_detection():
    assert is_openai_style("https://x/api/v1/chat/completions") is True   # by URL
    assert is_openai_style("http://host:8002/qwen", "psk_abc") is True    # by key
    assert is_openai_style("http://host:8002/qwen") is False             # legacy
    assert is_openai_style("", "") is False


def test_chat_client_sends_bearer_model_and_messages():
    with _Stub(content="hello there") as stub:
        out = OpenAIChatClient(url="https://x/chat/completions", api_key="testkey",
                               model="m-123").chat("sys", "hi")
        assert out == "hello there"
        sent = json.loads(stub.last_req.data.decode("utf-8"))
        assert sent["model"] == "m-123"
        assert sent["messages"] == [{"role": "system", "content": "sys"},
                                    {"role": "user", "content": "hi"}]
        assert stub.headers().get("authorization") == "Bearer testkey"


def test_no_key_sends_no_auth_header():
    with _Stub(content="x") as stub:
        OpenAIChatClient(url="https://x/chat/completions", model="m").chat("", "hi")
        assert "authorization" not in stub.headers()


def test_reasoning_normalized_from_chat_json():
    content = '{"event": "rate_cut", "impact": {"market_up": 0.8}, "confidence": 0.7}'
    with _Stub(content=content):
        c = QwenChatClient(url="https://x/chat/completions", api_key="k", model="m")
        e = c.reasoning("price 0.6, headline: central bank cuts rates")
    assert set(e) == {"event", "impact", "confidence"}
    assert e["event"] == "rate_cut"
    assert e["impact"]["market_up"] == 0.8
    assert 0.0 <= e["confidence"] <= 1.0


def test_sentiment_normalized_and_renormalized():
    content = '{"positive": 0.6, "negative": 0.1, "neutral": 0.3, "confidence": 0.8}'
    with _Stub(content=content):
        s = QwenChatClient(url="https://x/chat/completions", api_key="k", model="m").sentiment("rally")
    assert set(s) == {"positive", "negative", "neutral", "confidence"}
    assert abs(s["positive"] + s["negative"] + s["neutral"] - 1.0) < 1e-6


def test_event_tolerates_fenced_json():
    content = "```json\n{\"event\": \"crash\", \"impact\": {\"market_down\": 0.9}, \"confidence\": 0.6}\n```"
    with _Stub(content=content):
        e = QwenChatClient(url="https://x/chat/completions", api_key="k", model="m").event_extract("selloff")
    assert e["event"] == "crash"
    assert e["impact"]["market_down"] == 0.9


def test_fallback_to_local_on_network_error():
    fb = LocalHeuristicClient()
    with _Stub(raise_url=True):
        c = QwenChatClient(url="https://x/chat/completions", api_key="k", model="m", retries=1, fallback=fb)
        e = c.reasoning("central bank announces a rate cut")
        s = c.sentiment("markets surge")
    # Came from the local heuristic, still canonical schema.
    assert set(e) == {"event", "impact", "confidence"}
    assert set(s) == {"positive", "negative", "neutral", "confidence"}


def test_no_fallback_raises_on_network_error():
    with _Stub(raise_url=True):
        c = QwenChatClient(url="https://x/chat/completions", api_key="k", model="m", retries=0, fallback=None)
        try:
            c.reasoning("x")
        except OpenAIChatError:
            pass
        else:
            raise AssertionError("expected OpenAIChatError without a fallback")


def test_error_body_surfaces_as_failure():
    with _Stub(raw_body=json.dumps({"error": {"message": "bad key"}}).encode()):
        try:
            OpenAIChatClient(url="https://x/chat/completions", api_key="k", model="m", retries=0).chat("", "hi")
        except OpenAIChatError as e:
            assert "bad key" in str(e)
        else:
            raise AssertionError("expected OpenAIChatError on error body")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("Qwen OpenAI adapter: all passed")
