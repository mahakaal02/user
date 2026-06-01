"""
The shared signal layer — the single seam between ML and bots.

Once per tick the engine builds ONE :class:`SignalLayer` from the tick's news by
calling the inference client a bounded number of times (one sentiment + one
event per distinct headline, plus one market-level reasoning call). All bots
then read this same object. No bot ever calls the inference client itself, so
inference cost is O(headlines), not O(bots) — the hard "no per-bot inference"
constraint holds even at 1000 bots.

The raw inference records are attached so the recorder can persist them and a
later ``ReplayInferenceClient`` can reproduce the run exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .inference.base import InferenceClient, event_directional_pressure


@dataclass
class SignalLayer:
    tick: int
    sentiment: dict                      # aggregated canonical sentiment
    events: list[dict] = field(default_factory=list)
    reasoning: dict | None = None        # one shared market-level reasoning event
    directional: float = 0.0             # net pressure on YES price, [-1, 1]
    confidence: float = 0.0              # how much to trust `directional`, [0, 1]
    news_intensity: float = 0.0          # volume/strength of news this tick
    headlines: list[str] = field(default_factory=list)
    inference_records: list[dict] = field(default_factory=list)  # for replay/logging


def build_signal_layer(
    tick: int,
    headlines: list[str],
    client: InferenceClient,
    price_yes: float,
) -> SignalLayer:
    client.new_tick(tick)

    records: list[dict] = []
    sent_acc = {"positive": 0.0, "negative": 0.0, "neutral": 0.0, "confidence": 0.0}
    events: list[dict] = []
    dir_num = 0.0
    dir_den = 0.0

    for text in headlines:
        s = client.sentiment(text)
        e = client.event_extract(text)
        records.append({"input": text, "sentiment": s, "event": e})
        for k in sent_acc:
            sent_acc[k] += s[k]
        events.append(e)
        # Event direction weighted by its confidence.
        p = event_directional_pressure(e)
        dir_num += p * e["confidence"]
        dir_den += e["confidence"]
        # Sentiment direction (positive−negative) weighted by its confidence.
        dir_num += (s["positive"] - s["negative"]) * s["confidence"]
        dir_den += s["confidence"]

    n = max(len(headlines), 1)
    sentiment = {k: round(v / n, 6) for k, v in sent_acc.items()}

    # One shared, market-level reasoning call per tick (NOT per bot). The prompt
    # summarizes state + the loudest headline; the reasoner returns an event.
    top = max(headlines, key=len) if headlines else ""
    state_prompt = (
        f"Market YES price is {price_yes:.3f}. Latest headline: '{top}'. "
        f"Aggregate sentiment positive={sentiment['positive']:.2f} "
        f"negative={sentiment['negative']:.2f}. What is the likely directional impact?"
    )
    reasoning = client.reasoning(state_prompt)
    records.append({"input": state_prompt, "reasoning": reasoning})
    r_press = event_directional_pressure(reasoning)
    dir_num += r_press * reasoning["confidence"]
    dir_den += reasoning["confidence"]

    directional = max(-1.0, min(1.0, dir_num / dir_den)) if dir_den > 0 else 0.0
    confidence = min(1.0, dir_den / (2 * n + 1)) if (2 * n + 1) else 0.0

    return SignalLayer(
        tick=tick,
        sentiment=sentiment,
        events=events,
        reasoning=reasoning,
        directional=round(directional, 6),
        confidence=round(confidence, 6),
        news_intensity=round(min(1.0, len(headlines) / 3.0), 6),
        headlines=list(headlines),
        inference_records=records,
    )
