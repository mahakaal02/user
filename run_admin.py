#!/usr/bin/env python3
"""
Launch the Bot Admin Panel.

    python run_admin.py                                  # offline config, :8080
    python run_admin.py --config config.offline.yaml --port 8080
    python run_admin.py --paused                         # start paused

Then open http://127.0.0.1:8080 in a browser. Stdlib only (+ PyYAML for the
YAML config); no build step, no external services.
"""
from __future__ import annotations

import argparse

from admin.server import serve


def main() -> None:
    ap = argparse.ArgumentParser(description="Bot Admin Panel (live simulation control center)")
    ap.add_argument("--config", default="config.offline.yaml")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--paused", action="store_true", help="start with the simulation paused")
    args = ap.parse_args()
    serve(args.config, host=args.host, port=args.port, autostart=not args.paused)


if __name__ == "__main__":
    main()
