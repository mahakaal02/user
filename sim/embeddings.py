"""
Embeddings for the relevance cheap-filter.

    RSS → embeddings (cheap filter) → top-K → LLM (refine) → bots

Two backends:
  * remote — POST {"texts": [...]} to an embedding endpoint → {"vectors": [[...]]}.
             A real *semantic* model (sentence-transformers / a Qwen-embedding
             server / OpenAI-style /embeddings). The reference inference_server
             serves `/embed`. Set `embeddings.url`. This is the one that catches
             paraphrase ("S-1 filing" ↔ "IPO").
  * local  — dependency-free signed feature-hashing of word unigrams + char
             trigrams into a fixed-dim, L2-normalized vector. Cheap and fully
             offline, but LEXICAL (handles morphology/typos, not true paraphrase).
             The default when no url is set, and the graceful fallback if the
             remote endpoint is unreachable.

cosine() assumes L2-normalized vectors, so it's just the dot product.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request

_TOK = re.compile(r"[a-z0-9]+")


def _hash_idx_sign(feature: str, dim: int) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")
    return h % dim, (1.0 if (h >> 1) & 1 else -1.0)


def _l2(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


def cosine(a: list[float], b: list[float]) -> float:
    """Dot product — correct cosine for L2-normalized vectors."""
    return sum(x * y for x, y in zip(a, b))


class LocalHashingEmbedder:
    """Offline, dependency-free lexical embedding (signed feature hashing)."""

    name = "local_hashing"

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for w in _TOK.findall(text.lower()):
            if len(w) > 2:  # word unigram
                i, s = _hash_idx_sign("w:" + w, self.dim)
                v[i] += s
            padded = f"#{w}#"  # char trigrams (morphology / typo tolerance)
            for k in range(len(padded) - 2):
                i, s = _hash_idx_sign("c:" + padded[k : k + 3], self.dim)
                v[i] += s
        return _l2(v)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class RemoteEmbedder:
    """Calls a real embedding endpoint; falls back to local hashing on any error
    so the pipeline never breaks."""

    name = "remote"

    def __init__(self, url: str, timeout_s: float = 10.0, dim_fallback: int = 512) -> None:
        self.url = url
        self.timeout = timeout_s
        self._local = LocalHashingEmbedder(dim_fallback)

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            data = json.dumps({"texts": texts}).encode("utf-8")
            req = urllib.request.Request(self.url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read() or b"{}")
            vecs = resp.get("vectors") or resp.get("embeddings")
            if not vecs or len(vecs) != len(texts):
                raise ValueError("bad embedding response")
            return [_l2([float(x) for x in v]) for v in vecs]
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
            return self._local.embed(texts)


def build_embedder(cfg: dict | None):
    cfg = cfg or {}
    url = cfg.get("url")
    if url:
        return RemoteEmbedder(url, timeout_s=cfg.get("timeout_s", 10), dim_fallback=cfg.get("dim", 512))
    return LocalHashingEmbedder(dim=cfg.get("dim", 512))
