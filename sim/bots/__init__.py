"""Bot personalities + population factory. Bots depend only on the shared
SignalLayer and MarketView — never on the inference backend."""
from .base import BaseBot
from .factory import REGISTRY, build_population

__all__ = ["BaseBot", "REGISTRY", "build_population"]
