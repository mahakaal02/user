"""Market layer: ported AMM/CLOB, a swappable gateway, sim + HTTP backends."""
from .gateway import MarketGateway
from .types import Fill, MarketView, Order


def build_market(cfg: dict) -> MarketGateway:
    """Construct the market backend from the ``market:`` config section."""
    mode = cfg.get("mode", "sim")
    if mode == "sim":
        from .sim_market import SimMarket

        sim = cfg.get("sim", {})
        return SimMarket(
            yes_shares=sim.get("yes_shares", 1000.0),
            no_shares=sim.get("no_shares", 1000.0),
            history_len=sim.get("history_len", 64),
        )
    if mode == "http":
        from .http_market import HttpMarket

        http = cfg.get("http", {})
        return HttpMarket(
            base_url=http["base_url"],
            market_id=http["market_id"],
            trade_path=http.get("trade_path", "/api/trade"),
            state_path=http.get("state_path", "/api/markets/{id}/state"),
            auth_header=http.get("auth_header"),
            timeout_s=http.get("timeout_s", 10.0),
        )
    raise ValueError(f"Unknown market mode: {mode!r}")


__all__ = ["MarketGateway", "Order", "Fill", "MarketView", "build_market"]
