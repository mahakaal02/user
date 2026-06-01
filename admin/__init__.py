"""Bot Admin Panel — stdlib HTTP + SSE control center over the sim engine."""
from .manager import LiveSimulation
from .server import serve

__all__ = ["LiveSimulation", "serve"]
