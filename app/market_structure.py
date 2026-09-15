"""
MUFCA v4.0 — Market Structure Module

Computes where the significant price levels are: Volume Profile
(POC / Value Area) and Support/Resistance pivots. This module ONLY
describes market structure — it has no knowledge of confidence scoring,
adaptive TP/SL, or the A/U/B tracks. Those layers (signals.py, and later
a TP engine) consume a MarketStructure snapshot from here; this module
never reaches back into them. Keeping that boundary means chart.py and
the trading engine are structurally unable to compute different levels
with different algorithms — there is exactly one implementation of
"where is the POC" in the codebase, and everything else reads it.

Architecture:

    check_signals()
            |
            v
           df
            |
            v
     market_structure
            |
      +-----+-----+
      v     v     v
     POC   VA    S/R
      |     |     |
      +--+--+--+--+
         v     v
   confidence   TP engine   (both future stages — not wired up yet)
         |
         v
       chart
"""

import threading
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict

import config as _cfg
from utils import round_price

# =====================================================================
# 📐  SUPPORT / RESISTANCE
# =====================================================================

def _cluster_levels(levels: List[float], max_n: int, ref_price: float, tol: float = 0.005) -> List[Tuple[float, int]]:
    """Clusters nearby raw pivot prices, keeps up to max_n CLOSEST to
    ref_price (the current price).

    Returns (clustered_price, touch_count) pairs instead of plain prices —
    touch_count is how many raw pivots fell into that cluster, and is used
    as a simple strength proxy by calc_support_resistance()'s
    *_levels output (a level that formed from 5 separate pivots is more
    significant than one that formed from a single touch).
    """
    if not levels:
        return []
    levels = sorted(set(levels))
    clustered: List[Tuple[float, int]] = []
    used = [False] * len(levels)
    for i, l in enumerate(levels):
        if used[i]:
            continue
        used[i] = True  # mark the pivot element itself as consumed too, not just its cluster-mates
        cluster = [l]
        for j in range(i + 1, len(levels)):
            if not used[j] and abs(levels[j] - l) / (l + 1e-8) < tol:
                cluster.append(levels[j])
                used[j] = True
        clustered.append((float(np.mean(cluster)), len(cluster)))
    if len(clustered) <= max_n:
        return sorted(clustered, key=lambda t: t[0])
    clustered.sort(key=lambda t: abs(t[0] - ref_price))
    return sorted(clustered[:max_n], key=lambda t: t[0])


def _levels_to_dicts(pairs: List[Tuple[float, int]], ref_price: float) -> List[Dict]:
    return [
        {
            "price": round_price(price),
            "touches": touches,
            "distance_pct": round(abs(price - ref_price) / ref_price * 100, 3),
        }
        for price, touches in pairs
    ]


def calc_support_resistance(
    df: pd.DataFrame,
    pivot_window: int = 10,
    max_levels: int = 4
) -> Dict:
    """
    Support/resistance levels from Pivot Points (local min/max).

    Returns:
        {
            "support": [float, ...],        # legacy plain-price list, nearest levels first by price
            "resistance": [float, ...],
            "pivot": [],
            "support_levels": [{"price":.., "touches":.., "distance_pct":..}, ...],
            "resistance_levels": [...],      # same shape, strength-enriched — used by
                                              # get_market_structure() for anything beyond
                                              # just drawing a line, e.g. deciding whether a
                                              # level is significant enough to cap a TP target
        }
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]
    last_close = float(close.iloc[-2])  # confirmed bar

    supports: List[float] = []
    resistances: List[float] = []

    # 1. Pivot Points (local extremes)
    w = pivot_window
    for i in range(w, len(df) - w):
        hi_window = high.iloc[i - w:i + w + 1]
        lo_window = low.iloc[i - w:i + w + 1]
        if high.iloc[i] == hi_window.max():
            resistances.append(float(high.iloc[i]))
        if low.iloc[i] == lo_window.min():
            supports.append(float(low.iloc[i]))

    # Filter — keep only levels near the current price
    def near_price(levels, price, pct=0.05):
        return [l for l in levels if abs(l - price) / price < pct]

    # Direction filtering (resistance above price / support below price)
    # happens BEFORE selecting the top-N nearest, not after — otherwise
    # already-broken levels on the wrong side of price could crowd out
    # the few remaining levels that are still actually ahead of price.
    resistances = [l for l in resistances if l > last_close]
    supports = [l for l in supports if l < last_close]

    supports_ct    = _cluster_levels(near_price(supports,    last_close, 0.12), max_levels, last_close)
    resistances_ct = _cluster_levels(near_price(resistances, last_close, 0.12), max_levels, last_close)

    return {
        "support":    [price for price, _ in supports_ct],
        "resistance": [price for price, _ in resistances_ct],
        "pivot":      [],
        "support_levels":    _levels_to_dicts(supports_ct, last_close),
        "resistance_levels": _levels_to_dicts(resistances_ct, last_close),
    }


# =====================================================================
# 📊  VOLUME PROFILE (POC / Value Area)
# =====================================================================

def calc_volume_profile(
    df: pd.DataFrame,
    bins: int = 50,
    value_area_pct: float = 0.70,
) -> Dict:
    """
    Volume Profile approximated from OHLCV — there's no tick-level trade
    data available (Gate.io's public API doesn't provide it, and we don't
    want to pull the full trade stream just for this), so this is a
    TPO-style approximation, not a "real" exchange volume profile: each
    bar's volume is distributed evenly across the price bins its
    [low, high] range spans, instead of weighting toward where trades
    actually printed within the bar. This is the standard approach used by
    most retail tools that only have OHLCV, and gives a statistically
    reasonable POC/Value Area — just not pixel-identical to what a
    tick-level profile would show.

    Returns:
        {
            "poc": float | None,   # Point of Control — price bin with the most volume
            "vah": float | None,   # Value Area High
            "val": float | None,   # Value Area Low
            "bins": [{"price": float, "volume": float}, ...],  # for histogram rendering, low to high
        }
    """
    empty = {"poc": None, "vah": None, "val": None, "bins": []}
    if len(df) < 10:
        return empty

    price_min = float(df["low"].min())
    price_max = float(df["high"].max())
    if price_max <= price_min:
        return empty

    bin_width = (price_max - price_min) / bins
    volume_by_bin = np.zeros(bins)

    for row in df.itertuples():
        low, high, vol = float(row.low), float(row.high), float(row.volume)
        if vol <= 0:
            continue
        if high <= low:
            # doji / zero-range bar — dump its volume into a single bin
            idx = min(max(int((low - price_min) / bin_width), 0), bins - 1)
            volume_by_bin[idx] += vol
            continue
        first_bin = min(max(int((low - price_min) / bin_width), 0), bins - 1)
        last_bin = min(max(int((high - price_min) / bin_width), 0), bins - 1)
        n_spanned = last_bin - first_bin + 1
        volume_by_bin[first_bin:last_bin + 1] += vol / n_spanned

    total_volume = float(volume_by_bin.sum())
    if total_volume <= 0:
        return empty

    poc_idx = int(np.argmax(volume_by_bin))
    poc_price = price_min + (poc_idx + 0.5) * bin_width

    # Expand the Value Area outward from the POC bin — at each step, add
    # whichever neighbor (above or below) has more volume — until we've
    # covered value_area_pct of the total.
    target_volume = total_volume * value_area_pct
    covered_volume = float(volume_by_bin[poc_idx])
    lo_idx = hi_idx = poc_idx
    while covered_volume < target_volume and (lo_idx > 0 or hi_idx < bins - 1):
        vol_below = float(volume_by_bin[lo_idx - 1]) if lo_idx > 0 else -1.0
        vol_above = float(volume_by_bin[hi_idx + 1]) if hi_idx < bins - 1 else -1.0
        if vol_above >= vol_below:
            hi_idx += 1
            covered_volume += float(volume_by_bin[hi_idx])
        else:
            lo_idx -= 1
            covered_volume += float(volume_by_bin[lo_idx])

    val_price = price_min + lo_idx * bin_width
    vah_price = price_min + (hi_idx + 1) * bin_width

    bins_out = [
        {"price": round_price(price_min + (i + 0.5) * bin_width), "volume": round(float(volume_by_bin[i]), 4)}
        for i in range(bins)
    ]

    return {
        "poc": round_price(poc_price),
        "vah": round_price(vah_price),
        "val": round_price(val_price),
        "bins": bins_out,
    }


def classify_price_location(
    price: float,
    poc: Optional[float],
    vah: Optional[float],
    val: Optional[float],
) -> str:
    """Where price sits relative to the Value Area. "unknown" when VP
    couldn't be computed (not enough bars, or disabled)."""
    if vah is None or val is None:
        return "unknown"
    if price > vah:
        return "above_vah"
    if price < val:
        return "below_val"
    return "in_value_area"


# =====================================================================
# 🧊  MARKET STRUCTURE SNAPSHOT (cache + entry point for future consumers)
# =====================================================================

@dataclass(frozen=True)
class MarketStructure:
    """Immutable snapshot of market structure for one ticker/timeframe,
    as of one confirmed closed bar. Consumers (confidence scoring, a
    future TP engine) read this; they never compute POC/VA/S-R themselves.
    """
    ticker: str
    timeframe: str
    bar_time: int
    last_close: float
    poc: Optional[float]
    vah: Optional[float]
    val: Optional[float]
    price_location: str  # "above_vah" | "in_value_area" | "below_val" | "unknown"
    support_levels: List[Dict] = field(default_factory=list)     # [{"price","touches","distance_pct"}, ...]
    resistance_levels: List[Dict] = field(default_factory=list)


# 🆕 Cache keyed on the last CONFIRMED closed bar's own timestamp, not a
# wall-clock TTL. A TTL doesn't guarantee the cached snapshot matches the
# latest closed candle — a slow scan cycle can outlive a short TTL while
# the bar hasn't actually changed yet, and a fast one can return a stale
# snapshot from within the TTL window even though a new bar just closed.
# Recomputing exactly once per new closed bar is both more correct and,
# since nothing here needs to be known more often than that, strictly
# less work than polling on a timer would be over the same period.
_ms_cache: Dict[Tuple[str, str], Tuple[int, "MarketStructure"]] = {}
_ms_cache_lock = threading.Lock()


def clear_market_structure_cache():
    """Resets the market-structure cache — call after a manual history
    reset or similar bulk state change that could otherwise leave a stale
    snapshot behind for a ticker/timeframe."""
    global _ms_cache
    with _ms_cache_lock:
        _ms_cache = {}


def get_market_structure(
    df: pd.DataFrame,
    ticker: str,
    timeframe: str,
    sr_df: Optional[pd.DataFrame] = None,
) -> MarketStructure:
    """Returns the MarketStructure snapshot for this ticker/timeframe,
    computed from df's last CONFIRMED closed bar (iloc[-2] — matching the
    no-repainting rule the rest of the signal pipeline already follows).

    Cached per (ticker, timeframe); see the _ms_cache comment above for
    why the cache key is the closed bar's timestamp rather than a TTL.

    sr_df — optionally a longer lookback than df to compute support/
    resistance from (mirrors chart.py's build_chart(), which computes S/R
    from a fuller history than what's actually displayed, since pivots
    need more context than a short chart window gives them). Defaults to
    df itself when not given.
    """
    if len(df) < 10:
        raise ValueError("get_market_structure: df needs at least 10 rows")

    bar_time = int(df["timestamp"].iloc[-2])
    cache_key = (ticker, timeframe)

    with _ms_cache_lock:
        cached = _ms_cache.get(cache_key)
        if cached is not None and cached[0] == bar_time:
            return cached[1]

    last_close = float(df["close"].iloc[-2])

    # 🆕 FIX (external review, P1): df's last row is the still-forming,
    # unclosed candle. bar_time/last_close above already correctly read
    # iloc[-2] (the last CONFIRMED bar) — but vp_window used to be built
    # from the full df via df.tail(...), which still included that
    # unclosed row: calc_volume_profile() would distribute the live
    # candle's own high/low/volume into its bins. That's a live-repaint
    # leak — the snapshot cached under bar_time could come out differently
    # depending on where price happened to be mid-candle the first time it
    # was computed for that bar, then stay wrong for the rest of the
    # candle's duration (the cache holds it until the NEXT bar closes).
    # Slicing off the unclosed row before building vp_window fixes this.
    #
    # calc_support_resistance() below is NOT given the same treatment: it
    # has its own internal iloc[-2] convention (see its docstring/code),
    # designed around chart.py's build_chart() passing it a df that still
    # includes the live candle — pre-trimming here would double-trim and
    # shift its internal "last_close" reference back by one extra bar.
    # calc_support_resistance()'s own -2 indexing already keeps it off the
    # unclosed candle correctly; only the caller-side df.tail() here (which
    # has no such built-in protection) needed the explicit fix.
    confirmed_df = df.iloc[:-1]

    vp = {"poc": None, "vah": None, "val": None, "bins": []}
    if _cfg.VP_ENABLED:
        vp_window = confirmed_df.tail(min(_cfg.VP_LOOKBACK, len(confirmed_df)))
        vp = calc_volume_profile(vp_window, bins=_cfg.VP_BINS, value_area_pct=_cfg.VP_VALUE_AREA_PCT)

    sr_source = sr_df if sr_df is not None and len(sr_df) > len(df) else df
    sr = calc_support_resistance(sr_source, pivot_window=_cfg.SR_PIVOT_WINDOW, max_levels=_cfg.SR_MAX_LEVELS)

    snapshot = MarketStructure(
        ticker=ticker,
        timeframe=timeframe,
        bar_time=bar_time,
        last_close=last_close,
        poc=vp.get("poc"),
        vah=vp.get("vah"),
        val=vp.get("val"),
        price_location=classify_price_location(last_close, vp.get("poc"), vp.get("vah"), vp.get("val")),
        support_levels=sr.get("support_levels", []),
        resistance_levels=sr.get("resistance_levels", []),
    )

    with _ms_cache_lock:
        _ms_cache[cache_key] = (bar_time, snapshot)

    return snapshot
