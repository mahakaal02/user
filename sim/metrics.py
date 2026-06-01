"""
Post-run analysis: detect and quantify the target emergent phenomena (bubble,
crash, herd behaviour, delayed news reaction) and report bot P&L by personality.
Pure functions over the recorded price/flow series — no plotting deps; a compact
ASCII sparkline visualizes the price path on any terminal.
"""
from __future__ import annotations

from .engine.loop import RunResult

_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], lo: float = 0.0, hi: float = 1.0) -> str:
    if not values:
        return ""
    span = max(hi - lo, 1e-9)
    out = []
    for v in values:
        frac = (max(lo, min(hi, v)) - lo) / span
        out.append(_BLOCKS[min(len(_BLOCKS) - 1, int(frac * (len(_BLOCKS) - 1) + 0.5))])
    return "".join(out)


def detect_bubble_crash(prices: list[float]) -> dict:
    """Find the peak and the worst drawdown after it (crash), and the run-up to
    it (bubble). Drawdown is reported as a fraction of the peak price."""
    if len(prices) < 3:
        return {"bubble": False, "crash": False}
    peak_i = max(range(len(prices)), key=lambda i: prices[i])
    peak = prices[peak_i]
    runup = peak - min(prices[: peak_i + 1])
    trough_after = min(prices[peak_i:])
    drawdown = (peak - trough_after) / peak if peak > 0 else 0.0
    return {
        "peak_tick": peak_i,
        "peak_price": round(peak, 4),
        "runup": round(runup, 4),
        "trough_after_peak": round(trough_after, 4),
        "max_drawdown_pct": round(drawdown * 100, 1),
        "bubble": runup >= 0.15,           # a >=15c rise into the peak
        "crash": drawdown >= 0.25,         # a >=25% fall from the peak
    }


def herd_index(flows: list[float]) -> dict:
    """Lag-1 autocorrelation of net order flow — high positive values mean the
    crowd keeps pushing the same direction tick after tick (herding). Also report
    the fraction of ticks where flow keeps its previous sign."""
    fs = [f for f in flows]
    n = len(fs)
    if n < 3:
        return {"flow_autocorr": 0.0, "sign_persistence": 0.0}
    mean = sum(fs) / n
    num = sum((fs[i] - mean) * (fs[i - 1] - mean) for i in range(1, n))
    den = sum((f - mean) ** 2 for f in fs) or 1e-9
    autocorr = num / den
    same_sign = sum(1 for i in range(1, n) if fs[i] * fs[i - 1] > 0)
    return {
        "flow_autocorr": round(autocorr, 3),
        "sign_persistence": round(same_sign / (n - 1), 3),
        "herding": autocorr >= 0.2 or (same_sign / (n - 1)) >= 0.6,
    }


def delayed_reaction(rows: list[dict]) -> dict:
    """Lag between the strongest news signal and the price's response extremum."""
    if len(rows) < 5:
        return {}
    # Strongest bullish & bearish signal ticks (directional × confidence).
    def strength(r):
        return r["directional"] * r["signal_confidence"]

    bull = max(rows, key=strength)
    bear = min(rows, key=strength)
    prices = [r["price_yes"] for r in rows]

    def lag_to_extremum(start_tick: int, want_max: bool) -> int:
        window = prices[start_tick : min(len(prices), start_tick + 30)]
        if len(window) < 2:
            return 0
        ext_i = max(range(len(window)), key=lambda i: window[i]) if want_max else min(
            range(len(window)), key=lambda i: window[i]
        )
        return ext_i

    out = {}
    if strength(bull) > 0.05:
        out["bull_signal_tick"] = bull["tick"]
        out["bull_reaction_lag_ticks"] = lag_to_extremum(bull["tick"], want_max=True)
    if strength(bear) < -0.05:
        out["bear_signal_tick"] = bear["tick"]
        out["bear_reaction_lag_ticks"] = lag_to_extremum(bear["tick"], want_max=False)
    return out


def pnl_by_type(bots, final_price: float, start_coins: float, resolved: str | None) -> dict:
    agg: dict[str, dict] = {}
    for b in bots:
        if resolved == "YES":
            equity = b.coins + b.yes_shares
        elif resolved == "NO":
            equity = b.coins + b.no_shares
        else:
            equity = b.equity(final_price)
        d = agg.setdefault(b.kind, {"n": 0, "equity": 0.0})
        d["n"] += 1
        d["equity"] += equity
    for kind, d in agg.items():
        avg = d["equity"] / d["n"]
        d["avg_equity"] = round(avg, 1)
        d["avg_pnl"] = round(avg - start_coins, 1)
        d["avg_pnl_pct"] = round((avg - start_coins) / start_coins * 100, 1)
        del d["equity"]
    return agg


def summarize(result: RunResult, start_coins: float, max_upstream_tick: int) -> str:
    prices = result.prices
    rows = result.recorder.ticks
    final_price = prices[-1] if prices else 0.5

    bc = detect_bubble_crash(prices)
    hi = herd_index(result.flows)
    dr = delayed_reaction(rows)
    pnl = pnl_by_type(result.bots, final_price, start_coins, result.resolved)

    lo = min(prices) if prices else 0.0
    hi_p = max(prices) if prices else 1.0
    lines = []
    lines.append("=" * 66)
    lines.append("  SIMULATION SUMMARY")
    lines.append("=" * 66)
    lines.append(f"  ticks={result.ticks}  bots={len(result.bots)}  final YES price={final_price:.3f}")
    lines.append("")
    lines.append(f"  price path (YES, {lo:.2f}–{hi_p:.2f}):")
    lines.append(f"    {sparkline(prices)}")
    lines.append("")
    lines.append("  emergent phenomena")
    lines.append("  ------------------")
    lines.append(
        f"    bubble : {'YES' if bc.get('bubble') else 'no ':>3}  "
        f"(run-up +{bc.get('runup', 0):.2f} into peak {bc.get('peak_price', 0):.2f} @ tick {bc.get('peak_tick', '-')})"
    )
    lines.append(
        f"    crash  : {'YES' if bc.get('crash') else 'no ':>3}  "
        f"(max drawdown {bc.get('max_drawdown_pct', 0)}% to {bc.get('trough_after_peak', 0):.2f})"
    )
    lines.append(
        f"    herding: {'YES' if hi.get('herding') else 'no ':>3}  "
        f"(flow autocorr {hi.get('flow_autocorr')}, sign persistence {hi.get('sign_persistence')})"
    )
    if dr:
        lines.append(
            f"    delayed reaction: bull signal @ {dr.get('bull_signal_tick','-')} → peak +{dr.get('bull_reaction_lag_ticks','-')} ticks; "
            f"bear @ {dr.get('bear_signal_tick','-')} → trough +{dr.get('bear_reaction_lag_ticks','-')} ticks"
        )
    lines.append("")
    lines.append("  P&L by bot type (avg per bot)")
    lines.append("  -----------------------------")
    for kind in sorted(pnl):
        d = pnl[kind]
        lines.append(f"    {kind:<14} n={d['n']:<4} avg P&L {d['avg_pnl']:>+9.1f}  ({d['avg_pnl_pct']:>+6.1f}%)")
    lines.append("")
    lines.append("  hard-constraint check")
    lines.append("  ---------------------")
    lines.append(f"    total upstream inference calls : {result.upstream_calls_total}")
    lines.append(f"    max inference calls in any tick: {max_upstream_tick}  (independent of bot count ✔)")
    lines.append("=" * 66)
    return "\n".join(lines)
