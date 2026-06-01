"""
Reference inference server — runs on the "friend's machine" (or any GPU box).

Exposes the two HTTP endpoints the adapters expect:

    POST /finbert   {"text": "..."}                      → sentiment schema
    POST /qwen      {"task":"reasoning|event|sentiment", "prompt"/"text": "..."}
                                                          → event / sentiment schema

This file is intentionally self-contained and model-optional: if `transformers`
/ a Qwen client are installed it uses them; otherwise it falls back to a tiny
heuristic so the whole Mode-A/Mode-B pipeline is runnable end-to-end without a
GPU. Swap the marked sections for real model calls in production.

Run:
    pip install -r inference_server/requirements.txt
    uvicorn inference_server.server:app --host 0.0.0.0 --port 8001   # finbert
    # (point QWEN at another port, or serve both from one process behind a proxy)
"""
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Prediction-market inference server")


# --------------------------------------------------------------------------- #
#  FinBERT sentiment
# --------------------------------------------------------------------------- #
class SentimentReq(BaseModel):
    text: str


_finbert = None


def _load_finbert():
    """Lazy-load the real FinBERT pipeline; None if transformers isn't present."""
    global _finbert
    if _finbert is not None:
        return _finbert
    try:  # real model path
        from transformers import pipeline  # type: ignore

        _finbert = pipeline("text-classification", model="ProsusAI/finbert", top_k=None)
    except Exception:  # pragma: no cover - heuristic fallback
        _finbert = False
    return _finbert


@app.post("/finbert")
def finbert(req: SentimentReq) -> dict:
    pipe = _load_finbert()
    if pipe:  # real FinBERT → normalize labels to the standard schema
        scores = {d["label"].lower(): float(d["score"]) for d in pipe(req.text)[0]}
        pos, neg, neu = scores.get("positive", 0.0), scores.get("negative", 0.0), scores.get("neutral", 0.0)
        return {"positive": pos, "negative": neg, "neutral": neu, "confidence": max(pos, neg, neu)}
    # Heuristic fallback (no GPU needed) — same canonical schema.
    t = req.text.lower()
    pos = sum(w in t for w in ("surge", "rally", "beat", "cut", "growth", "record"))
    neg = sum(w in t for w in ("crash", "miss", "recession", "default", "selloff", "fear"))
    total = pos + neg + 1.0
    return {
        "positive": pos / total,
        "negative": neg / total,
        "neutral": 1.0 / total,
        "confidence": min(1.0, 0.5 + abs(pos - neg) / total),
    }


# --------------------------------------------------------------------------- #
#  Qwen reasoning / event extraction
# --------------------------------------------------------------------------- #
class QwenReq(BaseModel):
    task: str = "reasoning"
    prompt: str | None = None
    text: str | None = None
    market: str | None = None     # for task="relevance"
    headline: str | None = None   # for task="relevance"


@app.post("/qwen")
def qwen(req: QwenReq) -> dict:
    content = req.prompt or req.text or ""
    # === Real model path: prompt Qwen to emit STRICT JSON, then return it. ===
    #   resp = qwen_client.chat(system=SCHEMA_INSTRUCTION, user=content)
    #   return json.loads(resp)              # adapter also tolerates fenced JSON
    # Heuristic fallback below so the pipeline runs without the model:
    t = content.lower()
    if req.task == "sentiment":
        return finbert(SentimentReq(text=content))
    if req.task == "comment":
        # The live bot fleet (live/comment_gen.py) asks for a one-liner. A real
        # Qwen generates it from the prompt; this heuristic keeps the path
        # runnable without a model — returns {"text": "..."}.
        up = ("yes)" in t) or ("bullish" in t)
        return {"text": "looks like a solid yes here, momentum's there" if up
                else "fading this one, no looks like better value"}
    if req.task == "relevance":
        # The news pipeline (sim/news_feed.py RelevanceFilter, backend=qwen) asks
        # how relevant a headline is to a market, 0..1. A real Qwen/embeddings
        # model judges this; heuristic fallback = market/headline token overlap.
        import re as _re
        mt = {w for w in _re.findall(r"[a-z0-9]+", (req.market or "").lower()) if len(w) > 2}
        ht = {w for w in _re.findall(r"[a-z0-9]+", (req.headline or "").lower()) if len(w) > 2}
        score = (len(mt & ht) / len(mt)) if mt else 0.0
        return {"score": round(score, 3)}
    bullish = any(w in t for w in ("cut", "stimulus", "beat", "rally", "growth"))
    bearish = any(w in t for w in ("recession", "default", "selloff", "crash", "hike"))
    if bearish and not bullish:
        return {"event": "negative_shock", "impact": {"market_down": 0.8}, "confidence": 0.7}
    if bullish:
        return {"event": "positive_shock", "impact": {"market_up": 0.8}, "confidence": 0.7}
    return {"event": "none", "impact": {}, "confidence": 0.3}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "finbert_loaded": bool(_finbert)}
