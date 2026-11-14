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

Four ways to read "is this bar thin?" (METRICS) - all oriented the same way, HIGH =
liquid, a bar is blocked when its value is BELOW the threshold:

  bar    the ratio above (one bar vs the baseline median)
  smooth mean quote volume of the last SMOOTH_BARS bars (incl. the signal bar) / the
         same baseline - one quiet bar in a busy stretch does not block, a quiet
         stretch does
  thin   price impact per unit of volume: baseline median of (high-low)/close/qv
         divided by the same figure of the signal bar. Low = the bar moved a lot on
         little volume (the manipulation fingerprint), whatever the volume level
  how    quote volume vs the median of the SAME slot of the week (e.g. Sat 14:00)
         over the previous HOW_WEEKS weeks - a normally quiet weekend slot is
         compared with other weekend slots, so only an unusually quiet one blocks.
         Needs HOW_MIN_SAMPLES weeks of history (not available on short live
         windows for small timeframes - see metric_available)

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

METRICS = ("bar", "smooth", "thin", "how")
DEFAULT_METRIC = "bar"
SMOOTH_BARS = 3
HOW_WEEKS = 8
HOW_MIN_SAMPLES = 3
WEEK_MS = 7 * 86_400_000
# the live path fetches this many bars per pair/timeframe (signals.check_signals)
LIVE_FETCH_BARS = 900


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


def week_slots(tf_ms: int) -> Optional[int]:
    """Bars per week when the timeframe divides a week (1m..1d), else None."""
    tf_ms = int(tf_ms)
    if tf_ms <= 0 or WEEK_MS % tf_ms:
        return None
    return WEEK_MS // tf_ms


def metric_available(metric: str, tf_ms: int, live_bars: int = LIVE_FETCH_BARS) -> bool:
    """Can `metric` be computed on the live window of `live_bars` bars? (always
    True except 'how', which needs HOW_MIN_SAMPLES whole weeks behind the bar.)"""
    if metric not in METRICS:
        return False
    if metric != "how":
        return True
    period = week_slots(tf_ms)
    return period is not None and period * HOW_MIN_SAMPLES + 2 <= live_bars


def _clean(x: pd.Series) -> pd.Series:
    return x.replace([np.inf, -np.inf], np.nan)


def _bar(df, tf_ms, baseline_days) -> pd.Series:
    n = baseline_bars(tf_ms, baseline_days)
    qv = quote_volume(df)
    base = qv.shift(1).rolling(window=n, min_periods=_min_periods(n)).median()
    return _clean((qv / base).where(base > 0))


def _smooth(df, tf_ms, baseline_days) -> pd.Series:
    n = baseline_bars(tf_ms, baseline_days)
    qv = quote_volume(df)
    base = qv.shift(1).rolling(window=n, min_periods=_min_periods(n)).median()
    recent = qv.rolling(window=SMOOTH_BARS, min_periods=SMOOTH_BARS).mean()
    return _clean((recent / base).where(base > 0))


def _thin(df, tf_ms, baseline_days) -> pd.Series:
    n = baseline_bars(tf_ms, baseline_days)
    qv = quote_volume(df)
    close = df["close"].astype(float)
    rng = (df["high"].astype(float) - df["low"].astype(float)) / close
    illiq = _clean(rng / qv.where(qv > 0)).where(rng > 0)  # price move per unit of turnover
    base = illiq.shift(1).rolling(window=n, min_periods=_min_periods(n)).median()
    return _clean((base / illiq).where((base > 0) & (illiq > 0)))


def _how(df, tf_ms, baseline_days) -> pd.Series:
    period = week_slots(tf_ms)
    if period is None:
        return pd.Series(np.nan, index=df.index)
    qv = quote_volume(df)
    lags = pd.concat([qv.shift(period * k) for k in range(1, HOW_WEEKS + 1)], axis=1)
    cnt = lags.notna().sum(axis=1)
    base = lags.median(axis=1, skipna=True).where(cnt >= HOW_MIN_SAMPLES)
    return _clean((qv / base).where(base > 0))


_IMPL = {"bar": _bar, "smooth": _smooth, "thin": _thin, "how": _how}


def metric_series(df: pd.DataFrame, tf_ms: int, baseline_days: float, metric: str = DEFAULT_METRIC) -> pd.Series:
    """`metric` for EVERY bar of `df` (vectorised, used by the backtest). NaN where
    the baseline is not available. Bar i only ever sees bars <= i (no look-ahead)."""
    if metric not in _IMPL:
        raise ValueError(f"unknown volume metric {metric!r}")
    return _IMPL[metric](df, tf_ms, baseline_days)


def metric_frame(df: pd.DataFrame, tf_ms: int, baseline_days: float) -> pd.DataFrame:
    """All METRICS side by side (columns = metric names) - one pass for the tuner."""
    return pd.DataFrame({m: metric_series(df, tf_ms, baseline_days, m) for m in METRICS}, index=df.index)


def _span(tf_ms: int, baseline_days: float, metric: str) -> int:
    n = baseline_bars(tf_ms, baseline_days)
    if metric == "how":
        period = week_slots(tf_ms) or 1
        return period * HOW_WEEKS + 1
    return n + SMOOTH_BARS + 1


def metric_at(df: pd.DataFrame, idx: int, tf_ms: int, baseline_days: float,
              metric: str = DEFAULT_METRIC) -> Optional[float]:
    """`metric` of the single bar `idx` (live path - only the needed tail of df is
    computed). Returns exactly what metric_series(df, ...).iloc[idx] would, or None
    when the baseline is not available."""
    if idx < 1 or idx >= len(df):
        return None
    lo = max(0, idx - _span(tf_ms, baseline_days, metric))
    tail = df.iloc[lo:idx + 1]
    try:
        v = float(metric_series(tail, tf_ms, baseline_days, metric).iloc[-1])
    except Exception:
        return None
    return v if math.isfinite(v) else None


def ratio_series(df: pd.DataFrame, tf_ms: int, baseline_days: float) -> pd.Series:
    """Back-compat alias: the 'bar' metric."""
    return metric_series(df, tf_ms, baseline_days, "bar")


def ratio_at(df: pd.DataFrame, idx: int, tf_ms: int, baseline_days: float) -> Optional[float]:
    """Back-compat alias: the 'bar' metric of one bar."""
    return metric_at(df, idx, tf_ms, baseline_days, "bar")


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
    """Live values of the last CLOSED bar of every ticker x timeframe: all four
    metrics ("values", None where not computable), the metric/threshold in force
    ("metric", "threshold", "ratio" = the value of that metric) and whether the gate
    would block right now. For !volume and GET /api/volume-gate/live - one OHLCV fetch
    per pair/timeframe, on demand."""
    import config as _cfg
    from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe

    out: List[Dict] = []
    for t in tickers:
        for f in tfs:
            metric, thr = _cfg.volume_gate_rule(t, f)
            row: Dict = {"ticker": t, "tf": f, "metric": metric, "ratio": None, "threshold": thr,
                         "values": {m: None for m in METRICS}, "would_block": False, "bar_time": None}
            try:
                bars = await safe_fetch_ohlcv(exchange, t, f, limit=LIVE_FETCH_BARS)
                df = parse_ohlcv(bars) if bars else None
                if df is not None and validate_dataframe(df, 100):
                    idx = len(df) - 2  # last closed bar, same as check_signals
                    try:
                        tf_ms = int(exchange.parse_timeframe(f) * 1000)
                    except Exception:
                        tf_ms = infer_tf_ms(df)
                    row["values"] = {m: ratio_or_none(metric_at(df, idx, tf_ms, baseline_days, m)) for m in METRICS}
                    row["ratio"] = row["values"].get(metric)
                    row["bar_time"] = int(df["timestamp"].iloc[idx])
                    row["would_block"] = not passes(row["ratio"], thr)
                else:
                    row["error"] = "no data"
            except Exception as e:
                logger.warning(f"[VOLUME_GATE] live value failed for {t} {f}: {e}")
                row["error"] = str(e)
            out.append(row)
    return out
