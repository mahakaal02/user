"""
Multi-bot prediction-market behavioural simulation engine.

A standalone Python service that drives a (binary YES/NO) prediction market
with a population of heterogeneous trading bots. All ML inference (sentiment,
event extraction, reasoning) is reached through a single pluggable
``InferenceClient`` interface and selected entirely from ``config.yaml`` — bot
logic never imports a model, a URL, or an SDK.

The package is intentionally dependency-light: the default offline
configuration runs on the Python standard library alone (only PyYAML is needed
to parse the YAML config, and a ``.json`` config is accepted as a fallback).
"""

__version__ = "0.1.0"
