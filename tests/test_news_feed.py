"""News pipeline: query builder, RSS parsing, relevance filter, and the
MarketNewsFeed (primary-trust + fallback + caching) — all with stubbed network so
it runs offline. Run: python tests/test_news_feed.py"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim import news_feed as nf  # noqa: E402
from sim.news_feed import MarketNewsFeed, QueryBuilder, RelevanceFilter  # noqa: E402

_GNEWS_XML = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Mumbai Indians clinch IPL 2026 playoff spot - ESPNcricinfo</title>
<description>MI beat CSK</description><pubDate>Mon, 01 Jun 2026</pubDate></item>
<item><title>IPL 2026 schedule announced - Cricbuzz</title><description>dates</description></item>
</channel></rss>"""


class _FakeResp:
    def __init__(self, data): self._d = data.encode("utf-8")
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_query_builder_strips_scaffolding():
    qb = QueryBuilder()
    q = qb.build("Will Mumbai Indians win IPL 2026?")
    assert "Mumbai" in q and "Indians" in q and "IPL" in q and "2026" in q
    assert "Will" not in q and "?" not in q
    assert qb.build("Will OpenAI IPO this year?").lower().startswith("openai ipo")


def test_query_builder_folds_group_context():
    # A generic sub-market gets its parent group's context folded in.
    qb = QueryBuilder()
    q = qb.build("Spain", group_title="Who will win the FIFA World Cup?")
    low = q.lower()
    assert "spain" in low and "fifa" in low and "world" in low and "cup" in low
    assert q.split()[0].lower() == "spain"  # sub-market term leads
    # no group → unchanged
    assert qb.build("Spain") == "Spain"


def test_rss_parser_extracts_titles(monkeypatch=None):
    orig = nf.urllib.request.urlopen
    nf.urllib.request.urlopen = lambda req, timeout=None: _FakeResp(_GNEWS_XML)
    try:
        items = nf._fetch_rss("https://news.google.com/rss/search?q=x", 5)
    finally:
        nf.urllib.request.urlopen = orig
    assert len(items) == 2
    assert "Mumbai Indians" in items[0]["title"]
    # Google-style "Headline - Publisher" trimming
    assert nf._clean_gnews_title(items[0]["title"]) == "Mumbai Indians clinch IPL 2026 playoff spot"


def test_relevance_filter_keyword():
    rf = RelevanceFilter(backend="keyword", threshold=0.34)
    items = [
        {"title": "Mumbai Indians sign new player ahead of IPL 2026", "summary": ""},
        {"title": "Weather update: monsoon arrives early", "summary": ""},
    ]
    out = rf.rank("Mumbai Indians IPL 2026", "Will Mumbai Indians win IPL 2026?", items, max_items=5)
    assert any("Mumbai Indians" in t for t in out)
    assert all("Weather" not in t for t in out)  # off-topic dropped


def test_embedder_cosine():
    from sim.embeddings import LocalHashingEmbedder, cosine
    a, b, c = LocalHashingEmbedder().embed(["bitcoin price surge", "bitcoin price surge", "parking fees council"])
    assert cosine(a, b) > 0.99           # identical text
    assert cosine(a, c) < cosine(a, b)   # unrelated scores lower


def test_embeddings_backend_ranks_and_respects_topk():
    from sim.embeddings import LocalHashingEmbedder
    rf = RelevanceFilter(backend="embeddings", min_cosine=0.0, top_k=2, embedder=LocalHashingEmbedder())
    items = [
        {"title": "Bitcoin rallies toward $100k milestone", "summary": ""},
        {"title": "Local council debates parking fees", "summary": ""},
        {"title": "Crypto markets: BTC nears $100,000", "summary": ""},
    ]
    out = rf.rank("Bitcoin $100k", "Will Bitcoin close above $100k?", items, max_items=5)
    assert len(out) == 2                                   # top_k respected
    assert all("parking" not in t.lower() for t in out)   # irrelevant ranked out


def test_llm_refine_drops_low_scored_topk():
    from sim.embeddings import LocalHashingEmbedder
    rf = RelevanceFilter(backend="embeddings", min_cosine=-1.0, top_k=5, embedder=LocalHashingEmbedder(),
                         refine_enabled=True, refine_qwen_url="http://stub", refine_min_score=0.5)
    rf._llm_refine = lambda mt, h: 0.9 if "bitcoin" in h.lower() else 0.1  # stub the LLM judge
    items = [{"title": "Bitcoin nears $100k", "summary": ""}, {"title": "Unrelated weather news", "summary": ""}]
    out = rf.rank("Bitcoin $100k", "Will Bitcoin hit $100k?", items, max_items=5)
    assert out == ["Bitcoin nears $100k"]                 # LLM refinement kept only the relevant one


def test_feed_primary_trust_and_cache():
    calls = {"n": 0}

    def fake_fetch(url, timeout):
        calls["n"] += 1
        if "news.google.com" in url:
            return [{"title": "Mumbai Indians clinch IPL 2026 playoff spot - ESPN", "summary": ""},
                    {"title": "IPL 2026 race heats up as MI surge - Cricbuzz", "summary": ""}]
        return [{"title": "Unrelated economy headline", "summary": ""}]

    orig = nf._fetch_rss
    nf._fetch_rss = fake_fetch
    clock = {"t": 1000.0}
    try:
        feed = MarketNewsFeed(cache_ttl_s=300, now_fn=lambda: clock["t"])
        hl = feed.headlines_for("Will Mumbai Indians win IPL 2026?")
        assert len(hl) >= 2
        assert all(" - " not in h for h in hl)  # publisher suffix stripped
        n_after_first = calls["n"]
        # cached within TTL → no new fetch
        feed.headlines_for("Will Mumbai Indians win IPL 2026?")
        assert calls["n"] == n_after_first
        # past TTL → refetch
        clock["t"] += 400
        assert feed.needs_fetch("Will Mumbai Indians win IPL 2026?")
        feed.headlines_for("Will Mumbai Indians win IPL 2026?")
        assert calls["n"] > n_after_first
    finally:
        nf._fetch_rss = orig


def test_feed_falls_back_when_google_thin():
    def fake_fetch(url, timeout):
        if "news.google.com" in url:
            return []  # primary returns nothing → must use fallback
        return [{"title": "Bitcoin surges past $100k in record rally", "summary": ""},
                {"title": "Local sports roundup", "summary": ""}]

    orig = nf._fetch_rss
    nf._fetch_rss = fake_fetch
    try:
        feed = MarketNewsFeed(min_primary=2, now_fn=lambda: 1.0,
                              fallback_feeds=["https://feeds.bbci.co.uk/news/business/rss.xml"])
        hl = feed.headlines_for("Will Bitcoin close above $100k by March 31?")
        assert any("Bitcoin" in h for h in hl)
        assert all("sports roundup" not in h.lower() for h in hl)  # relevance-filtered
    finally:
        nf._fetch_rss = orig


def test_feed_graceful_on_network_error():
    def boom(url, timeout):
        raise OSError("network down")

    orig = nf._fetch_rss
    nf._fetch_rss = boom
    try:
        feed = MarketNewsFeed(now_fn=lambda: 1.0)
        assert feed.headlines_for("Will anything happen?") == []  # never raises
    finally:
        nf._fetch_rss = orig


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("News pipeline: all passed")
