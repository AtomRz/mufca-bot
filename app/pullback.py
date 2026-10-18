"""
pullback.py — "wait for the average pullback" limit-entry logic.

Why this exists
---------------
After a signal fires, price very often dips against the position for a bar or
two before it starts moving the right way. With PULLBACK_ENTRY_ENABLED the bot
doesn't enter at the signal bar's close — it places a *virtual limit order*
at a depth equal to the track's own average early pullback and waits for it
(see config.py's PULLBACK section for the knobs).

What "average pullback" means here
----------------------------------
For every past signal of a given ticker / timeframe / track (long and short
pooled — "adverse" is measured against the position, so the two are
comparable) we measure the largest move AGAINST the signal price during the
first N bars after the signal bar (N = PULLBACK_EXPIRY_BARS, i.e. exactly the
window in which the limit order would be alive). Signals whose price never
went against them in that window contribute 0 — they are real signals the
limit order would have missed, and leaving them out would make the average
pullback look deeper (and the fill rate higher) than it really is.

This is deliberately NOT read from the records' max_adverse_pct: that field
covers the whole life of the trade (up to MAX_HOLD_BARS, and losers' MAE is
simply their SL distance), so it says nothing about whether the dip came
BEFORE the move — the only thing that matters for a limit order.
The samples are computed straight from OHLCV, so:
  * they are policy-free (independent of TP/SL/entry mode — no feedback loop),
  * they work on all existing history (live + backtest records),
  * they are point-in-time safe: a signal only counts once all N of its
    bars are already closed (see `last_idx`).

This module is pure (no exchange / state / config imports) so it can be unit
tested and shared by the live path (signals.open_position, bot.py) and the
backtest (signals.backtest_history).
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------
# timestamps
# ---------------------------------------------------------------------

def _record_ts_ms(ts) -> Optional[int]:
    """History record timestamp (ISO string / datetime / epoch ms) -> epoch ms."""
    if ts is None or ts == "":
        return None
    try:
        if isinstance(ts, (int, float)):
            return int(ts)
        if isinstance(ts, datetime):
            dt = ts
        else:
            s = str(ts)
            if s.isdigit():
                return int(s)
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def signal_bar_index(ts_ms: int, bar_ts: np.ndarray) -> Optional[int]:
    """Index (into bar_ts, the bars' OPEN times) of the bar a history record's
    signal formed on.

    Two timestamp conventions live side by side in signals_history.json:
      * backtest records carry the OPEN time of the signal bar -> exact match;
      * live records carry wall-clock time a few seconds after the signal bar
        CLOSED, i.e. inside the next (forming) bar -> the signal bar is the
        one before the bar that contains the timestamp.
    """
    if len(bar_ts) == 0:
        return None
    j = int(np.searchsorted(bar_ts, ts_ms, side="right")) - 1
    if j < 0:
        return None
    if int(bar_ts[j]) == int(ts_ms):
        return j
    return j - 1 if j >= 1 else None


# ---------------------------------------------------------------------
# the statistic
# ---------------------------------------------------------------------

def early_adverse_pct(side: str, entry: float, highs: np.ndarray, lows: np.ndarray,
                      j: int, n_bars: int) -> float:
    """Largest move against the position, in % of `entry`, over bars
    j+1 .. j+n_bars (>= 0; 0 means price never went against the signal
    price in that window)."""
    lo, hi = j + 1, j + n_bars + 1
    if side == "long":
        worst = float(np.min(lows[lo:hi]))
        return max(0.0, (entry - worst) / entry * 100.0)
    worst = float(np.max(highs[lo:hi]))
    return max(0.0, (worst - entry) / entry * 100.0)


def pullback_samples(df: pd.DataFrame,
                     records_by_side: Dict[str, List[dict]],
                     track: str,
                     n_bars: int,
                     last_idx: int,
                     lookback_signals: int) -> List[float]:
    """Early-adverse excursions (%), one per past signal of `track`.

    last_idx — index of the most recent CLOSED bar usable as information
    (live: the signal bar, len(df)-2; backtest: idx). A past signal counts
    only if its whole N-bar window ends at or before last_idx.

    Both sides and every exit_type are included (incl. 'open', 'cancelled'
    and 'unfilled' — a signal is a signal whatever happened to the trade);
    synthetic (!sim) records are excluded. The most recent
    `lookback_signals` samples are kept.
    """
    if n_bars < 1 or len(df) == 0:
        return []
    bar_ts = df["timestamp"].to_numpy(dtype="int64")
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")

    found: List[Tuple[int, float]] = []
    for side in ("long", "short"):
        for rec in records_by_side.get(side, []) or []:
            if rec.get("track", "a") != track or rec.get("synthetic", False):
                continue
            entry = rec.get("entry")
            if not entry or entry <= 0:
                continue
            ts_ms = _record_ts_ms(rec.get("timestamp"))
            if ts_ms is None:
                continue
            j = signal_bar_index(ts_ms, bar_ts)
            if j is None or j + n_bars > last_idx or j + n_bars >= len(df):
                continue
            found.append((j, early_adverse_pct(side, float(entry), highs, lows, j, n_bars)))

    found.sort(key=lambda x: x[0])
    return [v for _, v in found[-lookback_signals:]]


def compute_pullback_depth(df: pd.DataFrame,
                           records_by_side: Dict[str, List[dict]],
                           track: str,
                           n_bars: int,
                           last_idx: int,
                           lookback_signals: int,
                           min_samples: int,
                           mult: float,
                           sl_dist_pct: float,
                           max_sl_fraction: float,
                           min_depth_pct: float) -> Optional[Dict]:
    """How deep to place the limit order (in % below/above the signal price).

    depth = mult * mean(early adverse excursion of past signals), then capped
    at max_sl_fraction * (this signal's SL distance) so the entry can never
    end up right next to the stop.

    Returns None — meaning "enter at market as usual" — when there aren't
    enough past signals (min_samples) or the resulting depth is too shallow
    to be worth waiting for (min_depth_pct).

    Returned dict: depth_pct, mean_pct, n, est_fill_rate (share of the
    samples whose pullback reached the chosen depth), capped (bool).
    """
    samples = pullback_samples(df, records_by_side, track, n_bars, last_idx, lookback_signals)
    if len(samples) < min_samples:
        return None
    arr = np.asarray(samples, dtype="float64")
    mean_pct = float(arr.mean())
    depth = mean_pct * mult
    cap = max(0.0, max_sl_fraction) * max(0.0, sl_dist_pct)
    capped = depth > cap
    depth = min(depth, cap)
    if depth < min_depth_pct:
        return None
    return {
        "depth_pct": float(depth),
        "mean_pct": mean_pct,
        "n": int(len(arr)),
        "est_fill_rate": float((arr >= depth).mean()),
        "capped": bool(capped),
    }


def limit_price_for(side: str, signal_price: float, depth_pct: float) -> float:
    """Limit price `depth_pct` against the signal price (below for a long,
    above for a short)."""
    f = depth_pct / 100.0
    return signal_price * (1.0 - f) if side == "long" else signal_price * (1.0 + f)


# ---------------------------------------------------------------------
# live: resolving a pending limit order from 1m candles
# ---------------------------------------------------------------------

def resolve_pending(side: str, limit: float, tp1: float, expire_ms: int,
                    candles: List[Tuple[int, float, float]]) -> Optional[Tuple[str, int]]:
    """Replays 1m candles [(ts, high, low), ...] in order against a pending
    limit order. Pure function (no state, no I/O).

    Returns:
        ("fill",   ts) — price traded through the limit at this candle;
        ("missed", ts) — price reached TP1 before (or in the same candle as)
                         touching the limit: the move left without us;
        ("expired", ts) — the first candle at/after expire_ms (window over);
        None            — nothing decided yet (keep waiting).

    A candle that touches BOTH the limit and TP1 is resolved as "missed":
    a single candle can't tell which came first, and crediting the fill
    (hence a likely win) on an unprovable ordering would flatter the
    feature. Same spirit as SAME_BAR_EXIT_POLICY's "SL first".
    """
    is_long = side == "long"
    for ts, high, low in candles:
        if ts >= expire_ms:
            return ("expired", ts)
        tp1_t = (high >= tp1) if is_long else (low <= tp1)
        if tp1_t:
            return ("missed", ts)
        touched = (low <= limit) if is_long else (high >= limit)
        if touched:
            return ("fill", ts)
    return None
