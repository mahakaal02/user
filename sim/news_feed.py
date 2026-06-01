"""
Real-news pipeline — per-market headlines for the signal engine.

    market title
        │  QueryBuilder         (title → focused search query)
        ▼
    Google News RSS            (primary, dynamic per-market query)
        │  (if too few hits)
        ▼
    BBC / Reuters feeds        (stability layer — reliable, low-bias)
        │  RelevanceFilter      (keyword overlap, or Qwen — pluggable)
        ▼
    headlines  →  build_signal_layer()  →  bots

Stdlib only: RSS is fetched with urllib and parsed with ElementTree (handles
both RSS 2.0 and Atom), so there is no feedparser dependency. Every network call
degrades gracefully — on any failure the caller falls back to the market title.

NOTE on "Reuters": Reuters discontinued its public RSS feeds (~2020), so the
stability layer defaults to BBC topic feeds (live + reliable). To include
Reuters, add a Google-News-scoped query feed (`...&q=<topic>+site:reuters.com`)
or any working outlet to `fallback_feeds` in config.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_UA = "Mozilla/5.0 (compatible; KalkiNewsFleet/1.0)"

# Question scaffolding + stopwords stripped when turning a market title into a query.
_QSTOP = {
    "will", "is", "are", "was", "were", "be", "been", "being", "the", "a", "an",
    "to", "of", "in", "on", "for", "by", "this", "that", "than", "at", "as",
    "do", "does", "did", "any", "other", "with", "and", "or", "it", "its",
    "their", "his", "her", "up", "down", "above", "below", "over", "under",
    "more", "less", "before", "after", "during", "who", "what", "when", "which",
}

# Live, reliable default stability feeds (BBC topic RSS).
_DEFAULT_FALLBACK = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
]

_WORD = re.compile(r"[A-Za-z0-9$%][A-Za-z0-9$%.'-]*")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _fetch_rss(url: str, timeout: float) -> list[dict]:
    """GET an RSS/Atom feed and return [{title, summary, published}]. Raises on
    network/parse failure (callers catch)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    items: list[dict] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = summary = published = ""
        for ch in node:
            lt = _local(ch.tag)
            if lt == "title" and ch.text:
                title = ch.text.strip()
            elif lt in ("description", "summary") and ch.text:
                summary = re.sub(r"<[^>]+>", "", ch.text).strip()
            elif lt in ("pubDate", "published", "updated") and ch.text:
                published = ch.text.strip()
        if title:
            items.append({"title": title, "summary": summary, "published": published})
    return items


def _clean_gnews_title(title: str) -> str:
    """Google News titles are 'Headline - Publisher' — drop the trailing source."""
    return re.sub(r"\s+-\s+[^-]+$", "", title).strip()


class QueryBuilder:
    """Market title → focused Google News query.

    A generic sub-market title (e.g. "Spain") inside a parent group ("Who will
    win the FIFA World Cup?") gets the parent's context folded in →
    "Spain win FIFA World Cup", so the query is actually about the right event.
    """

    def __init__(self, max_terms: int = 12) -> None:
        self.max_terms = max_terms

    def _keep(self, text: str) -> list[str]:
        toks = _WORD.findall(text.rstrip("?").strip())
        keep = [w for w in toks if w.lower() not in _QSTOP]
        if len(keep) < 2 and toks:  # too aggressive — fall back to raw tokens
            keep = toks
        return keep

    def build(self, title: str, category: str | None = None, group_title: str | None = None) -> str:
        keep = self._keep(title)
        if group_title:
            seen = {w.lower() for w in keep}
            for w in self._keep(group_title):  # sub-market terms first, parent context after
                if w.lower() not in seen:
                    seen.add(w.lower())
                    keep.append(w)
        return " ".join(keep[: self.max_terms]) or title.strip()


class RelevanceFilter:
    """Score a candidate headline's relevance to a market. Pluggable backend:
    'keyword' (token overlap, default, offline) or 'qwen' (LLM judge, opt-in)."""

    def __init__(self, backend: str = "keyword", threshold: float = 0.34,
                 qwen_url: str | None = None, timeout_s: float = 10.0) -> None:
        self.backend = backend
        self.threshold = threshold
        self.qwen_url = qwen_url
        self.timeout = timeout_s

    @staticmethod
    def _terms(text: str) -> set[str]:
        # Exclude stopwords so the relevance denominator counts only meaningful
        # terms (otherwise filler like "will/above/by" dilutes the score).
        return {w.lower() for w in _WORD.findall(text) if len(w) > 2 and w.lower() not in _QSTOP}

    def _kw_score(self, qterms: set[str], text: str) -> float:
        if not qterms:
            return 0.0
        return len(qterms & self._terms(text)) / len(qterms)

    def _qwen_score(self, market_title: str, headline: str) -> float:
        try:
            payload = json.dumps({"task": "relevance", "market": market_title, "headline": headline}).encode()
            req = urllib.request.Request(self.qwen_url, data=payload, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read() or b"{}")
            return float(resp.get("score", 0.0))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
            return 0.0

    def filter(self, query: str, market_title: str, items: list[dict], max_items: int,
               min_score: float | None = None) -> list[str]:
        floor = self.threshold if min_score is None else min_score
        qterms = self._terms(query) | self._terms(market_title)
        scored: list[tuple[float, str]] = []
        for it in items:
            text = it["title"] + " " + it.get("summary", "")
            score = self._kw_score(qterms, text)
            if self.backend == "qwen" and self.qwen_url and score > 0:
                score = self._qwen_score(market_title, it["title"])
            if score >= floor:
                scored.append((score, it["title"]))
        scored.sort(key=lambda s: s[0], reverse=True)
        # de-dupe preserving order
        out, seen = [], set()
        for _, t in scored:
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
            if len(out) >= max_items:
                break
        return out


class MarketNewsFeed:
    """Per-market real-news fetcher with caching + graceful fallback."""

    def __init__(self, *, hl: str = "en-US", gl: str = "US", ceid: str = "US:en",
                 fallback_feeds: list[str] | None = None, relevance: RelevanceFilter | None = None,
                 max_items: int = 5, min_primary: int = 2, cache_ttl_s: float = 300.0,
                 timeout_s: float = 6.0, apply_relevance_to_primary: bool = False,
                 now_fn=None) -> None:
        self.hl, self.gl, self.ceid = hl, gl, ceid
        self.fallback_feeds = fallback_feeds if fallback_feeds is not None else list(_DEFAULT_FALLBACK)
        self.relevance = relevance or RelevanceFilter()
        self.max_items = max_items
        self.min_primary = min_primary
        self.cache_ttl_s = cache_ttl_s
        self.timeout = timeout_s
        self.apply_relevance_to_primary = apply_relevance_to_primary
        self.qb = QueryBuilder()
        self._now = now_fn or time.time
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self.stats = {"google_ok": 0, "google_fail": 0, "fallback_used": 0}

    def _google_url(self, query: str) -> str:
        q = urllib.parse.quote(query)
        return f"https://news.google.com/rss/search?q={q}&hl={self.hl}&gl={self.gl}&ceid={self.ceid}"

    def needs_fetch(self, title: str, category: str | None = None, group_title: str | None = None) -> bool:
        """True if this market's news is missing or older than the TTL — lets the
        runner spend its per-cycle HTTP budget only on real fetches."""
        ent = self._cache.get(self.qb.build(title, category, group_title))
        return not (ent and (self._now() - ent[0]) < self.cache_ttl_s)

    def headlines_for(self, title: str, category: str | None = None, group_title: str | None = None,
                      allow_fetch: bool = True) -> list[str]:
        """Relevant real headlines for a market. ``group_title`` (the parent group,
        if any) is folded into the query so generic sub-markets resolve correctly.
        Cached for ``cache_ttl_s``; ``allow_fetch=False`` returns cache-or-empty."""
        query = self.qb.build(title, category, group_title)
        ent = self._cache.get(query)
        if ent and (self._now() - ent[0]) < self.cache_ttl_s:
            return ent[1]
        if not allow_fetch:
            return ent[1] if ent else []
        headlines = self._fetch(query, title)
        self._cache[query] = (self._now(), headlines)
        return headlines

    def _fetch(self, query: str, title: str) -> list[str]:
        # 1) primary — Google News dynamic query
        primary: list[str] = []
        try:
            raw = _fetch_rss(self._google_url(query), self.timeout)
            self.stats["google_ok"] += 1
            cleaned = [{"title": _clean_gnews_title(i["title"]), "summary": i.get("summary", "")} for i in raw]
            if self.apply_relevance_to_primary:
                primary = self.relevance.filter(query, title, cleaned, self.max_items, min_score=0.0001)
            else:
                # trust Google's ranking; just take the top, de-duped
                seen: set[str] = set()
                for c in cleaned:
                    t = c["title"]
                    if t and t.lower() not in seen:
                        seen.add(t.lower())
                        primary.append(t)
                    if len(primary) >= self.max_items:
                        break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ET.ParseError, OSError):
            self.stats["google_fail"] += 1

        if len(primary) >= self.min_primary:
            return primary[: self.max_items]

        # 2) stability fallback — BBC/Reuters feeds, strictly relevance-filtered
        fb_items: list[dict] = []
        for url in self.fallback_feeds:
            try:
                fb_items += _fetch_rss(url, self.timeout)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ET.ParseError, OSError):
                continue
        fb = self.relevance.filter(query, title, fb_items, self.max_items)
        if fb:
            self.stats["fallback_used"] += 1
        merged, seen = [], set()
        for t in primary + fb:
            if t.lower() not in seen:
                seen.add(t.lower())
                merged.append(t)
        return merged[: self.max_items]


def build_market_news_feed(cfg: dict):
    """Construct a MarketNewsFeed from the ``news_feed:`` config section, or None
    if disabled."""
    if not cfg or not cfg.get("enabled", False):
        return None
    g = cfg.get("google_news", {})
    rel_cfg = cfg.get("relevance", {})
    relevance = RelevanceFilter(
        backend=rel_cfg.get("backend", "keyword"),
        threshold=rel_cfg.get("threshold", 0.34),
        qwen_url=rel_cfg.get("qwen_url") or None,
    )
    return MarketNewsFeed(
        hl=g.get("hl", "en-US"),
        gl=g.get("gl", "US"),
        ceid=g.get("ceid", "US:en"),
        fallback_feeds=cfg.get("fallback_feeds"),
        relevance=relevance,
        max_items=cfg.get("max_items", 5),
        min_primary=cfg.get("min_primary", 2),
        cache_ttl_s=cfg.get("cache_ttl_s", 300),
        timeout_s=cfg.get("timeout_s", 6),
        apply_relevance_to_primary=rel_cfg.get("apply_to_primary", False),
    )
