"""
Config loading — the single source of truth for which backends run.

* Accepts ``.yaml``/``.yml`` (needs PyYAML) or ``.json`` (stdlib only), so a
  zero-install environment can still run via JSON.
* Substitutes ``${VAR}`` / ``${VAR:-default}`` from the environment recursively,
  so URLs, ports and secrets are injected at deploy time and NEVER hardcoded —
  this is what makes Mode A ↔ Mode B (local vs remote inference) a pure
  config/env change with no code edits.
* Validates that the four required sections are present.
"""
from __future__ import annotations

import json
import os
import re

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_REQUIRED = ("inference", "market", "bots", "sim")


def _subst_env(value):
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            var, default = m.group(1), m.group(2)
            return os.environ.get(var, default if default is not None else m.group(0))

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _subst_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst_env(v) for v in value]
    return value


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PyYAML not installed. Either `pip install pyyaml` or use the "
                "JSON config (e.g. config.offline.json)."
            ) from exc
        raw = yaml.safe_load(text)
    elif path.endswith(".json"):
        raw = json.loads(text)
    else:
        raise ValueError(f"Unsupported config extension: {path}")

    cfg = _subst_env(raw)
    missing = [s for s in _REQUIRED if s not in cfg]
    if missing:
        raise ValueError(f"Config {path} missing required section(s): {missing}")
    return cfg


def apply_overrides(cfg: dict, *, ticks: int | None = None, seed: int | None = None) -> dict:
    """CLI overrides win over file values (handy for sweeps)."""
    if ticks is not None:
        cfg["sim"]["ticks"] = ticks
    if seed is not None:
        cfg["sim"]["seed"] = seed
    return cfg
