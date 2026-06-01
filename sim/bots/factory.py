"""
Build the bot population from the ``bots:`` config section.

Each population entry names a ``type`` and a ``count`` plus optional ``params``.
Every bot gets:
  * a deterministic private RNG stream (seeded from the master seed + its id), so
    a 1000-bot run is fully reproducible; and
  * small per-bot trait jitter (bias, aggressiveness, reaction delay) drawn from
    that stream, so individuals within a personality are heterogeneous without
    sacrificing determinism.

Unknown param keys are filtered out (against each class's constructor signature)
so a typo in config can't crash a long run.
"""
from __future__ import annotations

import inspect

from ..rng import RngHub
from .base import BaseBot
from .contrarian import ContrarianBot
from .herd import HerdBot
from .momentum import MomentumBot
from .news_reactive import NewsReactiveBot
from .noise import NoiseBot
from .overconfident import OverconfidentBot

REGISTRY: dict[str, type[BaseBot]] = {
    "momentum": MomentumBot,
    "contrarian": ContrarianBot,
    "news_reactive": NewsReactiveBot,
    "overconfident": OverconfidentBot,
    "herd": HerdBot,
    "noise": NoiseBot,
}


def _accepted(cls: type) -> set[str]:
    names: set[str] = set()
    for klass in cls.__mro__:
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        try:
            names |= set(inspect.signature(init).parameters)
        except (TypeError, ValueError):
            continue
    names.discard("self")
    names.discard("args")
    names.discard("kwargs")
    return names


def build_population(cfg: dict, rng_hub: RngHub) -> list[BaseBot]:
    defaults = cfg.get("defaults", {})
    jitter = cfg.get("jitter", {"bias": 0.15, "aggressiveness": 0.3, "reaction_spread": 2})
    bots: list[BaseBot] = []
    idx = 0

    for entry in cfg.get("population", []):
        btype = entry["type"]
        cls = REGISTRY.get(btype)
        if cls is None:
            raise ValueError(f"Unknown bot type {btype!r}; known: {sorted(REGISTRY)}")
        count = int(entry.get("count", 0))
        merged = {**defaults, **entry.get("params", {})}
        accepted = _accepted(cls)

        for _ in range(count):
            bot_id = f"{btype}-{idx}"
            idx += 1
            rng = rng_hub.stream(f"bot:{bot_id}")

            params = {k: v for k, v in merged.items() if k in accepted}
            # Deterministic per-bot heterogeneity.
            params["bias"] = params.get("bias", 0.0) + rng.uniform(-jitter["bias"], jitter["bias"])
            base_aggr = params.get("aggressiveness", 0.15)
            params["aggressiveness"] = max(
                0.01, base_aggr * (1.0 + rng.uniform(-jitter["aggressiveness"], jitter["aggressiveness"]))
            )
            base_delay = int(params.get("reaction_delay", 0))
            params["reaction_delay"] = base_delay + rng.randint(0, int(jitter["reaction_spread"]))

            bots.append(cls(bot_id, rng, **params))

    return bots
