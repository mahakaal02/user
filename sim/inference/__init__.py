"""Pluggable inference layer: one interface, swappable backends, per-tick cache."""
from .base import (
    InferenceClient,
    event_directional_pressure,
    normalize_event,
    normalize_sentiment,
)
from .factory import build_inference_client

__all__ = [
    "InferenceClient",
    "build_inference_client",
    "normalize_sentiment",
    "normalize_event",
    "event_directional_pressure",
]
