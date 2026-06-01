"""
Run recorder — writes one JSONL row per tick and captures everything needed for
deterministic replay.

Each row carries: tick, price, net flow, volume, per-tick upstream inference call
count (to *prove* the no-per-bot-inference constraint), the directional signal,
and the raw inference records (so ``ReplayInferenceClient`` can reproduce a run
that used remote models). The file is both the audit log and the replay tape.
"""
from __future__ import annotations

import json
from typing import TextIO


class Recorder:
    def __init__(self, path: str | None) -> None:
        self._fh: TextIO | None = open(path, "w", encoding="utf-8") if path else None
        self.ticks: list[dict] = []

    def log_tick(
        self,
        tick: int,
        price_yes: float,
        signal,
        net_flow: float,
        volume: float,
        n_orders: int,
        upstream_calls_total: int,
        upstream_calls_tick: int,
    ) -> None:
        row = {
            "tick": tick,
            "price_yes": round(price_yes, 6),
            "directional": signal.directional,
            "signal_confidence": signal.confidence,
            "net_flow": round(net_flow, 4),
            "volume": round(volume, 4),
            "orders": n_orders,
            "upstream_calls_total": upstream_calls_total,
            "upstream_calls_tick": upstream_calls_tick,
            "headlines": signal.headlines,
            # Raw inference for replay — kept compact.
            "inference": signal.inference_records,
        }
        self.ticks.append(row)
        if self._fh:
            self._fh.write(json.dumps(row) + "\n")
            self._fh.flush()

    @property
    def prices(self) -> list[float]:
        return [r["price_yes"] for r in self.ticks]

    @property
    def flows(self) -> list[float]:
        return [r["net_flow"] for r in self.ticks]

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
