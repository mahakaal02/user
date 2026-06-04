"""
OpenAI-compatible chat-completions transport (stdlib ``urllib`` only).

This is the shared client for any LLM served behind the OpenAI
``/v1/chat/completions`` protocol — e.g. a hosted Qwen on PodStack::

    POST {url}
    Authorization: Bearer {api_key}
    Content-Type: application/json
    {"model": "<id>", "messages": [{"role": "user", "content": "..."}]}
    ->  {"choices": [{"message": {"content": "..."}}], "usage": {...}}

Three Qwen touch-points reuse it — the inference adapter (reasoning / events /
sentiment), the live comment generator, and the news relevance refiner — so the
Bearer-auth + model-id + response-parsing logic lives in exactly ONE place.

Bots and config never import a URL or key directly; everything flows from
``config.yaml`` / env / the admin "Model endpoints" panel, matching the rest of
the inference layer. No third-party SDK — same stdlib-only ethos as the project.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class OpenAIChatError(RuntimeError):
    """Raised when a chat completion cannot be obtained (network or bad shape)."""


def is_openai_style(url: str | None, api_key: str | None = None) -> bool:
    """True when a ``(url, key)`` pair should use the OpenAI chat protocol rather
    than the project's legacy custom ``{"task": ...}`` Qwen contract.

    Heuristic: an API key is present, or the URL is an OpenAI-style
    chat-completions endpoint. This lets the admin panel / live fleet auto-route
    a pasted PodStack endpoint without a separate protocol toggle — a bare
    ``http://host:8002/qwen`` with no key still hits the legacy server.
    """
    u = (url or "").strip()
    return bool((api_key or "").strip()) or "chat/completions" in u


class OpenAIChatClient:
    """Minimal chat-completions client (no third-party SDK, just ``urllib``)."""

    def __init__(
        self,
        url: str,
        api_key: str = "",
        model: str = "",
        timeout_s: float = 20.0,
        retries: int = 2,
        backoff_s: float = 0.5,
        temperature: float = 0.2,
        max_tokens: int = 256,
    ) -> None:
        if not url:
            raise ValueError("OpenAIChatClient requires a 'url'")
        self.url = url
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout_s = float(timeout_s)
        self.retries = int(retries)
        self.backoff_s = float(backoff_s)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)

    def complete(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format_json: bool = False,
    ) -> str:
        """POST a chat completion and return the assistant message content.

        Retries with linear (jitter-free, reproducible) backoff on network/5xx
        errors. Raises :class:`OpenAIChatError` if every attempt fails or the
        response carries no usable content.
        """
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        if response_format_json:
            # Honored by servers that support it; harmless to those that don't
            # (the prompt also demands JSON and the parser tolerates prose).
            payload["response_format"] = {"type": "json_object"}
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    body = json.loads(resp.read().decode("utf-8") or "{}")
                return _extract_content(body)
            except OpenAIChatError:
                raise  # a well-formed error response should not be retried
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as exc:
                last_err = exc
                if attempt < self.retries:
                    _sleep(self.backoff_s * (attempt + 1))
        raise OpenAIChatError(
            f"chat completion POST {self.url} failed after {self.retries + 1} attempts: {last_err}"
        )

    def chat(self, system: str, user: str, **kw) -> str:
        """Convenience: a single (optional system) + user turn → content string."""
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        return self.complete(msgs, **kw)


def _extract_content(body: dict) -> str:
    """Pull the assistant text out of an OpenAI-style response body."""
    if not isinstance(body, dict):
        raise OpenAIChatError("non-object chat response")
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        msg = choices[0].get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if content is None:
            content = choices[0].get("text")  # legacy /completions shape
        if isinstance(content, list):  # some servers return content as parts
            content = "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content
            )
        if isinstance(content, str):
            return content
    # Some servers return {"error": {...}} with HTTP 200 — surface it as a failure.
    if "error" in body:
        raise OpenAIChatError(str(body["error"])[:200])
    raise OpenAIChatError("chat response had no choices/content")


def _sleep(seconds: float) -> None:
    import time

    time.sleep(max(0.0, seconds))
