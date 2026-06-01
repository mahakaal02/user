"""
Live Kalki Exchange bot fleet.

Drives the REAL `bet` prediction market over its internal service API
(/api/internal/{markets,bot-users,trade,comment}, Bearer-authenticated). It
reuses the simulator's bot personalities to decide trades across every open
market (existing + newly created), and posts LLM-generated one-liner comments.

Nothing here touches the inference *models* directly per-bot: per-market signals
are computed once and shared, exactly like the offline simulator.
"""

__version__ = "0.1.0"
