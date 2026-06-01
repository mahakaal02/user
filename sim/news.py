"""
News feeds. Each source yields the headlines for a given tick; the engine passes
them to the (shared) inference pipeline.

Three sources, all behind one interface and chosen from config:
  * ``synthetic`` — deterministic scripted shocks + a seeded background drizzle.
    The default. Produces a reproducible narrative (a bullish shock, then a
    bearish one) that, combined with reaction delays and herding, yields the
    target emergent behaviours (bubble → crash → contrarian correction).
  * ``stored``    — replay headlines from a JSONL file ({"tick":N,"headlines":[…]}).
  * ``rss``       — optional live RSS via ``feedparser`` (guarded import); fetched
    once and spread across ticks so runs stay bounded.
"""
from __future__ import annotations

import abc
import json

# Background pool: mild, mixed headlines that create noise without strong signal.
_DRIZZLE = [
    "Markets little changed in quiet trading",
    "Analysts mixed on quarterly outlook",
    "Trading volume steady ahead of data",
    "Investors weigh sector rotation",
    "Volatility subdued as traders await news",
    "Sentiment cautious amid light calendar",
]

_BULL_POOL = [
    "Central bank signals surprise interest rate cut",
    "Stimulus package boosts growth optimism",
    "Earnings beat expectations across the board",
    "Markets rally to record on bullish momentum",
    "Upgrade wave lifts sentiment as inflation eases",
]
_BEAR_POOL = [
    "Recession fears mount as data disappoints",
    "Major issuer warns of credit default risk",
    "Sharp selloff accelerates; bearish sentiment spikes",
    "Funds slash exposure amid crash worries",
    "Rate hike shock triggers downgrade wave",
]

# Default narrative = quiet open → sustained BULL regime → sustained BEAR regime.
# Sustained one-way news is what lets trend-followers inflate a real multi-tick
# bubble before contrarians/exhaustion break it.
_DEFAULT_REGIMES = [
    {"start": 12, "end": 28, "pool": _BULL_POOL, "prob": 0.85},
    {"start": 46, "end": 64, "pool": _BEAR_POOL, "prob": 0.9},
]


class NewsSource(abc.ABC):
    @abc.abstractmethod
    def headlines(self, tick: int) -> list[str]:
        ...


class SyntheticNewsSource(NewsSource):
    def __init__(
        self,
        rng,
        shocks: dict[int, list[str]] | None = None,
        regimes: list[dict] | None = None,
        drizzle_prob: float = 0.3,
    ) -> None:
        self._rng = rng
        # JSON/YAML give string keys; coerce to int ticks.
        self._shocks = {int(k): v for k, v in (shocks or {}).items()}
        self._regimes = regimes if regimes is not None else _DEFAULT_REGIMES
        self._drizzle_prob = drizzle_prob

    def headlines(self, tick: int) -> list[str]:
        out: list[str] = []
        # One-off scripted shocks (optional, exact-tick).
        if tick in self._shocks:
            out.extend(self._shocks[tick])
        # Sustained regimes drive the bubble/crash narrative.
        for reg in self._regimes:
            if reg["start"] <= tick < reg["end"] and self._rng.random() < reg.get("prob", 0.85):
                pool = reg["pool"] if isinstance(reg["pool"], list) else _BULL_POOL
                out.append(self._rng.choice(pool))
        # Background noise, quieter so the regimes dominate.
        if self._rng.random() < self._drizzle_prob:
            out.append(self._rng.choice(_DRIZZLE))
        return out


class StoredNewsSource(NewsSource):
    def __init__(self, path: str) -> None:
        self._by_tick: dict[int, list[str]] = {}
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                t = int(row["tick"])
                hs = row.get("headlines") or ([row["text"]] if "text" in row else [])
                self._by_tick.setdefault(t, []).extend(hs)

    def headlines(self, tick: int) -> list[str]:
        return list(self._by_tick.get(tick, []))


class RssNewsSource(NewsSource):
    """Live RSS, spread across ticks. Optional — needs ``feedparser``."""

    def __init__(self, urls: list[str], per_tick: int = 1) -> None:
        try:
            import feedparser  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "RSS news source needs feedparser: pip install feedparser"
            ) from exc
        self._titles: list[str] = []
        for url in urls:
            feed = feedparser.parse(url)
            self._titles.extend(e.get("title", "") for e in feed.entries)
        self._per_tick = max(1, per_tick)

    def headlines(self, tick: int) -> list[str]:
        if not self._titles:
            return []
        start = (tick * self._per_tick) % len(self._titles)
        return [self._titles[(start + i) % len(self._titles)] for i in range(self._per_tick)]


def build_news_source(cfg: dict, rng) -> NewsSource:
    kind = cfg.get("source", "synthetic")
    if kind == "synthetic":
        return SyntheticNewsSource(
            rng,
            shocks=cfg.get("shocks"),
            regimes=cfg.get("regimes"),
            drizzle_prob=cfg.get("drizzle_prob", 0.3),
        )
    if kind == "stored":
        return StoredNewsSource(cfg["path"])
    if kind == "rss":
        return RssNewsSource(cfg["urls"], per_tick=cfg.get("per_tick", 1))
    raise ValueError(f"Unknown news source: {kind!r}")
