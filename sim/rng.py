"""
Deterministic randomness plumbing.

Every stochastic decision in the simulator pulls from a seeded ``random.Random``
derived from a single master seed. Sub-streams (per-bot, news, clearing order)
are derived by hashing a label with the master seed so they are independent yet
fully reproducible. This is what makes ``--replay`` bit-for-bit deterministic.

We deliberately avoid numpy here: the standard-library Mersenne-Twister is
reproducible across machines and keeps the "runs on a low-end laptop with no
heavy deps" promise.
"""
from __future__ import annotations

import hashlib
import random


def _derive_seed(master_seed: int, label: str) -> int:
    """Stable 63-bit sub-seed from (master_seed, label).

    Uses BLAKE2b so the mapping is identical on every platform/Python build —
    unlike ``hash()``, which is salted per-process.
    """
    h = hashlib.blake2b(f"{master_seed}:{label}".encode("utf-8"), digest_size=8)
    return int.from_bytes(h.digest(), "big") & 0x7FFFFFFFFFFFFFFF


class RngHub:
    """Factory for independent, reproducible random streams."""

    def __init__(self, master_seed: int) -> None:
        self.master_seed = int(master_seed)
        self._streams: dict[str, random.Random] = {}

    def stream(self, label: str) -> random.Random:
        """Return a named stream, creating it deterministically on first use."""
        rng = self._streams.get(label)
        if rng is None:
            rng = random.Random(_derive_seed(self.master_seed, label))
            self._streams[label] = rng
        return rng
