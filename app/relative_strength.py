"""
MUFCA v4.0 — Relative Strength Module

Answers a different question than HTF bias does: not "is this asset's own
trend bullish", but "is this asset currently stronger or weaker than a
benchmark" (by default, BTC — the natural benchmark for alts). Computed
from the ratio series (asset_close / benchmark_close), not from comparing
two independent bull/bear flags — "ETH bullish AND BTC bullish" measures
correlation of direction, not relative strength.

Deliberately simple for now: a plain return-over-N-bars + SMA trend on
the ratio, not FRAMA-on-ratio. This is an already highly adaptive system
(FRAMA, K-means MFI clustering, Hurst, adaptive TP/SL, on-chain bias...);
stacking one more adaptive layer on top of the ratio before checking
whether the SIMPLE version of this metric actually carries information on
real data would make it hard to tell whether a result came from the
market or from the layer. Upgrade to calc_relative_strength_frama() (not
implemented yet) only once the simple version is validated as useful.

Like market_structure.py, this module has no opinion on signal quality —
it returns numbers and a trend label; how much weight that carries in a
confidence score is a decision for signals.py, not for here.
"""

import threading
import logging
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Dict, Tuple

import ccxt
from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe

logger = logging.getLogger(__name__)

# Ratio bars needed = lookback window + a small margin for the SMA itself
# to be defined at the start of that window.
_RS_FETCH_LIMIT = 100
_RS_MIN_ROWS = 30


def default_benchmark_for(ticker: str, benchmark: str = "BTC/USDT") -> Optional[str]:
    """The natural benchmark for a ticker — BTC/USDT for everything else,
    None for BTC/USDT itself (comparing BTC to BTC is meaningless; BTC's
    own absolute HTF bias already covers that case). Callers should treat
    None as "no relative-strength benchmark applies here", not as
    neutral/bearish/bullish.
    """
    return None if ticker == benchmark else benchmark


# =====================================================================
# 📐  CORE METRIC
# =====================================================================

def calc_relative_strength(
    asset_closes: pd.Series,
    benchmark_closes: pd.Series,
    lookback: int = 20,
) -> Optional[Dict]:
    """Computes a simple relative-strength read from two aligned close-price
    series (same length, same bar order, index-aligned — see
    get_relative_strength() for how the two are aligned by timestamp
    before reaching here).

    Returns:
        {
            "ratio": float,       # last asset_close / benchmark_close
            "return_pct": float,  # % change in the ratio over `lookback` bars
            "sma": float,         # simple moving average of the ratio over `lookback` bars
            "above_sma": bool,    # ratio currently above its own SMA
            "trend": "up" | "down" | "flat",
        }
    or None if there isn't enough aligned history to compute it.
    """
    if len(asset_closes) != len(benchmark_closes):
        return None
    if len(asset_closes) < lookback + 1:
        return None

    ratio = asset_closes.reset_index(drop=True) / benchmark_closes.reset_index(drop=True)
    window = ratio.tail(lookback)

    last_ratio = float(ratio.iloc[-1])
    # 🆕 FIX (external review): this used to take first_ratio from
    # window.iloc[0] — the first of the LAST `lookback` bars — which spans
    # only lookback-1 intervals to last_ratio, not a genuine lookback-bar
    # return (e.g. lookback=20 measured a 19-bar change). The
    # `len(asset_closes) < lookback + 1` guard above already assumes one
    # extra bar exists for exactly this reason; iloc[-lookback-1] is that
    # bar — the reference point exactly `lookback` bars before the last
    # one. window (used only for the SMA below) is unaffected — the SMA is
    # deliberately still over the last `lookback` bars, not lookback+1.
    first_ratio = float(ratio.iloc[-lookback - 1])
    if first_ratio == 0:
        return None

    return_pct = (last_ratio - first_ratio) / abs(first_ratio) * 100
    sma = float(window.mean())
    above_sma = last_ratio > sma

    if return_pct > 0 and above_sma:
        trend = "up"
    elif return_pct < 0 and not above_sma:
        trend = "down"
    else:
        trend = "flat"  # mixed signal: e.g. recent bounce still below a falling SMA

    return {
        "ratio": last_ratio,
        "return_pct": round(return_pct, 4),
        "sma": sma,
        "above_sma": above_sma,
        "trend": trend,
    }


# =====================================================================
# 🧊  CACHED ENTRY POINT (fetches the benchmark, aligns, computes)
# =====================================================================

_rs_cache: Dict[Tuple[str, str, str], Tuple[int, Dict]] = {}
_rs_cache_lock = threading.Lock()


def clear_relative_strength_cache():
    """Resets the relative-strength cache."""
    global _rs_cache
    with _rs_cache_lock:
        _rs_cache = {}


async def get_relative_strength(
    exchange: ccxt.Exchange,
    asset_df: pd.DataFrame,
    asset_ticker: str,
    timeframe: str,
    benchmark_ticker: Optional[str] = None,
    lookback: int = 20,
) -> Optional[Dict]:
    """Fetches benchmark_ticker's own OHLCV and computes asset_ticker's
    relative strength against it, using the already-fetched asset_df
    (the same df check_signals() already has — no second fetch for the
    asset itself, only one new fetch for the benchmark).

    Cached per (asset_ticker, benchmark_ticker, timeframe), keyed on the
    asset's own last confirmed closed bar timestamp — same bar-identity
    approach as market_structure.get_market_structure(), for the same
    reason (a wall-clock TTL doesn't guarantee the cached read still
    matches the latest closed candle).

    Returns None (not a neutral/bullish/bearish value) when: no benchmark
    applies (see default_benchmark_for()), the benchmark fetch fails, or
    there isn't enough aligned history yet — callers must not treat None
    as a directional read.
    """
    if benchmark_ticker is None:
        benchmark_ticker = default_benchmark_for(asset_ticker)
    if benchmark_ticker is None:
        return None
    if len(asset_df) < 2:
        return None

    asset_bar_time = int(asset_df["timestamp"].iloc[-2])
    cache_key = (asset_ticker, benchmark_ticker, timeframe)

    with _rs_cache_lock:
        cached = _rs_cache.get(cache_key)
        if cached is not None and cached[0] == asset_bar_time:
            return cached[1]

    try:
        bars = await safe_fetch_ohlcv(exchange, benchmark_ticker, timeframe, limit=_RS_FETCH_LIMIT)
        if not bars:
            return None
        benchmark_df = parse_ohlcv(bars)
        if not validate_dataframe(benchmark_df, _RS_MIN_ROWS):
            return None
    except Exception as e:
        logger.warning(f"Relative strength ({asset_ticker} vs {benchmark_ticker} {timeframe}): {e}")
        return None

    # Only confirmed closed bars on both sides (drop each series' own
    # still-forming last bar), then align by timestamp — the two were
    # fetched independently a moment apart, so positional alignment alone
    # isn't reliable if one exchange call returned one bar more/fewer than
    # the other.
    asset_closed = asset_df.iloc[:-1][["timestamp", "close"]].rename(columns={"close": "asset_close"})
    benchmark_closed = benchmark_df.iloc[:-1][["timestamp", "close"]].rename(columns={"close": "benchmark_close"})
    merged = pd.merge(asset_closed, benchmark_closed, on="timestamp", how="inner").sort_values("timestamp")

    result = calc_relative_strength(merged["asset_close"], merged["benchmark_close"], lookback=lookback)
    if result is None:
        return None

    result["asset"] = asset_ticker
    result["benchmark"] = benchmark_ticker
    result["bar_time"] = asset_bar_time

    with _rs_cache_lock:
        _rs_cache[cache_key] = (asset_bar_time, result)

    return result
