"""
volume_gate.py - low-volume (thin-liquidity) entry gate.

Why: when trading is thin (weekends, holidays, dead hours) price is easy to push
around, wicks are random and breakouts fail more often - the A/U tracks have no
edge there. This gate compares the volume of the signal bar with what is normal
for THIS pair on THIS timeframe and blocks entries when it is far below it.

Metric (one number, "ratio"):

    ratio = quote volume of the signal bar / median quote volume of the PRIOR
            baseline window (default 14 days) on the same timeframe

  - quote volume = base volume x close (about USDT turnover), so a price drift
    over the baseline window does not distort the comparison;
  - median, not mean: a handful of news-spike bars must not drag the baseline up;
  - the baseline is the bars BEFORE the signal bar only (no look-ahead, and the
    bar is never part of its own yardstick - same reasoning as
    volume_indicators.calculate_relative_volume);
  - everything is computed on the signal's own timeframe from the OHLCV the bot
    already fetches - no extra requests, and live and backtest use the SAME code.

Because the baseline is a median over ALL hours of the week, weekends and
holidays come out low by themselves - there is no calendar in here, and an
unusually thin Tuesday night is caught as well.

A bar whose baseline is not available yet (too little history, zero median) has
ratio None, and None PASSES: the gate never blocks what it cannot measure.

The live gate (signals.check_signals) turns a blocked signal into a SHADOW
record (shadow.py), so what the gate keeps out stays measurable. The backtest
does not apply the gate (like ENTRY_BLOCK) - it only records the ratio on every
trigger, and signals.run_volume_autotune() picks the threshold from that.
"""

import logging
import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# a baseline shorter than this many bars is too noisy to call a median
MIN_BASELINE_BARS = 20


def infer_tf_ms(df: pd.DataFrame) -> int:
    """Bar length in ms, read from the timestamps themselves (the backtest runs in a
    worker process with no exchange object to ask). Median of the first diffs, so a
    gap in the data does not matter."""
    ts = df["timestamp"].to_numpy(dtype="int64")
    if len(ts) < 3:
        return 3600_000
    diffs = np.diff(ts[:500])
    diffs = diffs[diffs > 0]
    return int(np.median(diffs)) if len(diffs) else 3600_000


def baseline_bars(tf_ms: int, baseline_days: float) -> int:
    """How many bars of this timeframe make up `baseline_days`."""
    return max(MIN_BASELINE_BARS, int(round(float(baseline_days) * 86_400_000 / max(int(tf_ms), 1))))


def _min_periods(n_baseline: int) -> int:
    return max(MIN_BASELINE_BARS, n_baseline // 2)


def quote_volume(df: pd.DataFrame) -> pd.Series:
    """Approximate turnover per bar (base volume x close)."""
    return df["volume"].astype(float) * df["close"].astype(float)


def ratio_series(df: pd.DataFrame, tf_ms: int, baseline_days: float) -> pd.Series:
    """ratio for EVERY bar of `df` (vectorised, used by the backtest). NaN where the
    baseline is not available. Bar i is compared with bars i-N .. i-1."""
    n = baseline_bars(tf_ms, baseline_days)
    qv = quote_volume(df)
    base = qv.shift(1).rolling(window=n, min_periods=_min_periods(n)).median()
    ratio = qv / base
    ratio = ratio.where(base > 0)
    return ratio.replace([np.inf, -np.inf], np.nan)


def ratio_at(df: pd.DataFrame, idx: int, tf_ms: int, baseline_days: float) -> Optional[float]:
    """ratio of the single bar `idx` (live path - only the tail of df is touched).
    Returns exactly what ratio_series(df, ...).iloc[idx] would, or None when the
    baseline is not available / the bar has no usable volume."""
    if idx < 1 or idx >= len(df):
        return None
    n = baseline_bars(tf_ms, baseline_days)
    lo = max(0, idx - n)
    vol = df["volume"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    qv_now = vol[idx] * close[idx]
    prior = vol[lo:idx] * close[lo:idx]
    prior = prior[np.isfinite(prior)]
    if len(prior) < _min_periods(n) or not math.isfinite(qv_now):
        return None
    base = float(np.median(prior))
    if not base > 0:
        return None
    r = qv_now / base
    return r if math.isfinite(r) else None


def passes(ratio: Optional[float], threshold: float) -> bool:
    """True = the entry may go ahead. Unknown ratio and threshold <= 0 (gate off for
    this pair/timeframe) both pass."""
    if ratio is None or threshold is None or threshold <= 0:
        return True
    return ratio >= threshold


def ratio_or_none(value) -> Optional[float]:
    """float -> float, NaN/inf/None -> None (JSON-safe, 4 decimals)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if math.isfinite(f) else None


async def current_ratios(exchange, tickers: List[str], tfs: List[str], baseline_days: float) -> List[Dict]:
    """Live ratio of the last CLOSED bar of every ticker x timeframe, with the
    threshold in force and whether the gate would block right now. For !volume and
    GET /api/volume-gate/live - one OHLCV fetch per pair/timeframe, on demand."""
    import config as _cfg
    from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe

    out: List[Dict] = []
    for t in tickers:
        for f in tfs:
            row: Dict = {"ticker": t, "tf": f, "ratio": None, "threshold": _cfg.volume_gate_threshold(t, f),
                         "would_block": False, "bar_time": None}
            try:
                bars = await safe_fetch_ohlcv(exchange, t, f, limit=900)
                df = parse_ohlcv(bars) if bars else None
                if df is not None and validate_dataframe(df, 100):
                    idx = len(df) - 2  # last closed bar, same as check_signals
                    try:
                        tf_ms = int(exchange.parse_timeframe(f) * 1000)
                    except Exception:
                        tf_ms = infer_tf_ms(df)
                    row["ratio"] = ratio_or_none(ratio_at(df, idx, tf_ms, baseline_days))
                    row["bar_time"] = int(df["timestamp"].iloc[idx])
                    row["would_block"] = not passes(row["ratio"], row["threshold"])
                else:
                    row["error"] = "no data"
            except Exception as e:
                logger.warning(f"[VOLUME_GATE] live ratio failed for {t} {f}: {e}")
                row["error"] = str(e)
            out.append(row)
    return out
