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

Pooling across tracks
---------------------
A track with fewer than `min_samples` signals of its own on a pair/timeframe
(typical for A and B, which fire rarely) would never get a limit order at all.
With `pool_fallback` the average is then measured over the signals of ALL
tracks of that pair/timeframe instead (a bar that several tracks signalled
on counts once per side). This borrows U's pullback behaviour for tracks
whose entries may behave differently — `!pullsim` shows per track whether
that actually helps; switch it off to keep the strict per-track rule.

Depth selection modes
---------------------
"mean" places the limit at mult x the average pullback above. Its weakness is
that the average mixes two very different kinds of signal: the ones that
later lose usually dip hard and early, the ones that win dip less. Every
signal gets the same limit order, and a deeper limit costs nothing on the
losers (they trade through it anyway) but makes more winners leave without
us. The average therefore tends to be deeper than what pays.

"optimize" skips the average. For the signal at hand it replays every past
signal's price path (OHLCV only, so no feedback from how those signals were
really traded) against this signal's own TP1 / SL distances for a grid of
depths and keeps the depth with the best expected PnL per signal. A signal
that is missed or expires counts as 0, so the price of waiting is paid
inside the objective. If no depth beats entering at market, the answer is
"market". The objective is a simplified TP1-vs-SL expectancy (no TP2 leg, no
fees); `!pullsim` runs the full backtest and stays the judge.

Stop placement
--------------
`shifted_sl()` implements the "keep_distance" stop mode: the stop moves with
the better entry so the risk per trade stays what it was at the signal price
(instead of the stop staying at its absolute level and the entry creeping
closer to it, which turns every deeper fill into a tighter stop).

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
    # 🆕 FIX: np.min/np.max propagate NaN, and max(0.0, nan) is 0.0 — a single
    # NaN candle in the window silently turned the whole sample into "no pullback"
    # (and dragged the mean/fill-rate estimate). NaN-aware reductions; a window
    # with no valid candle at all is reported as nan so the caller can skip it.
    window = lows[lo:hi] if side == "long" else highs[lo:hi]
    if window.size == 0 or not np.isfinite(window).any():
        return float("nan")
    if side == "long":
        worst = float(np.nanmin(window))
        return max(0.0, (entry - worst) / entry * 100.0)
    worst = float(np.nanmax(window))
    return max(0.0, (worst - entry) / entry * 100.0)


def signal_points(df: pd.DataFrame,
                  records_by_side: Dict[str, List[dict]],
                  track: Optional[str],
                  horizon: int,
                  last_idx: int) -> List[Tuple[int, str, float]]:
    """(signal bar index, side, signal price) of every past signal of `track`
    whose `horizon` bars after the signal bar are all closed by `last_idx`,
    oldest first. Shared by pullback_samples() (horizon = wait window) and the
    depth optimizer (horizon = wait window + hold period).

    Both sides and every exit_type are included (incl. 'open', 'cancelled'
    and 'unfilled' — a signal is a signal whatever happened to the trade);
    synthetic (!sim) records are excluded.

    track=None pools every track of the pair/timeframe; a signal bar that
    more than one track fired on (same side) is counted once.
    """
    if horizon < 1 or len(df) == 0:
        return []
    bar_ts = df["timestamp"].to_numpy(dtype="int64")
    found: List[Tuple[int, str, float]] = []
    seen = set()
    for side in ("long", "short"):
        for rec in records_by_side.get(side, []) or []:
            if rec.get("synthetic", False):
                continue
            if track is not None and rec.get("track", "a") != track:
                continue
            # 🆕 FIX: a hand-edited / legacy record with a non-numeric entry
            # raised TypeError here and took the whole pullback calculation
            # (and with it the signal) down.
            try:
                entry = float(rec.get("entry"))
            except (TypeError, ValueError):
                continue
            if not np.isfinite(entry) or entry <= 0:
                continue
            ts_ms = _record_ts_ms(rec.get("timestamp"))
            if ts_ms is None:
                continue
            j = signal_bar_index(ts_ms, bar_ts)
            if j is None or j + horizon > last_idx or j + horizon >= len(df):
                continue
            if track is None:
                if (side, j) in seen:
                    continue
                seen.add((side, j))
            found.append((j, side, entry))

    found.sort(key=lambda x: x[0])
    return found


def pullback_samples(df: pd.DataFrame,
                     records_by_side: Dict[str, List[dict]],
                     track: Optional[str],
                     n_bars: int,
                     last_idx: int,
                     lookback_signals: int) -> List[float]:
    """Early-adverse excursions (%), one per past signal of `track`.

    last_idx — index of the most recent CLOSED bar usable as information
    (live: the signal bar, len(df)-2; backtest: idx). A past signal counts
    only if its whole N-bar window ends at or before last_idx. Which signals
    count (sides, exit types, synthetic, pooling) — see signal_points().
    The most recent `lookback_signals` samples are kept.
    """
    if n_bars < 1 or len(df) == 0:
        return []
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")

    out: List[float] = []
    for j, side, entry in signal_points(df, records_by_side, track, n_bars, last_idx):
        adv = early_adverse_pct(side, entry, highs, lows, j, n_bars)
        if not np.isfinite(adv):
            continue
        out.append(adv)
    return out[-lookback_signals:]


# ---------------------------------------------------------------------
# depth optimizer: choose the depth from history instead of the average
# ---------------------------------------------------------------------

# Candidate limit depths tried between min_depth_pct and the SL cap (the
# "enter at market" alternative is always evaluated on top of these).
OPT_GRID_POINTS = 12
# The best limit depth must beat entering at market by more than this many
# percentage points of PnL per signal, otherwise the optimizer says "market".
OPT_MIN_EDGE_PCT = 0.0


def _signed_paths(side: str, entry: float, highs: np.ndarray, lows: np.ndarray,
                  closes: np.ndarray, j: int, n_bars: int
                  ) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """The price path over bars j+1 .. j+n_bars after a past signal, as signed
    returns relative to the signal price where POSITIVE always means in favour
    of the position: (best excursion per bar, worst excursion per bar, close
    per bar). Long and short become mirror images, so one simulator serves
    both. None when the window is incomplete or holds a non-finite candle
    (such a sample is skipped rather than guessed at)."""
    lo, hi = j + 1, j + n_bars + 1
    h, l, c = highs[lo:hi], lows[lo:hi], closes[lo:hi]
    if h.size < n_bars or not (np.isfinite(h).all() and np.isfinite(l).all() and np.isfinite(c).all()):
        return None
    if side == "long":
        return h / entry - 1.0, l / entry - 1.0, c / entry - 1.0
    return 1.0 - l / entry, 1.0 - h / entry, 1.0 - c / entry


def _history_paths(df: pd.DataFrame,
                   records_by_side: Dict[str, List[dict]],
                   track: Optional[str],
                   n_bars: int,
                   hold_bars: int,
                   last_idx: int,
                   lookback_signals: int) -> List[Tuple[np.ndarray, np.ndarray, np.ndarray, bool]]:
    """(best, worst, close, is_long) price paths of the most recent
    `lookback_signals` past signals, each followed for n_bars + hold_bars bars.

    A past signal counts only once that whole horizon is closed (see
    signal_points) — the outcome of a signal is simply not known earlier, and
    using it before that would be look-ahead. The paths come straight from
    OHLCV, so they are independent of how those signals were actually traded
    (no feedback loop from the entry mode, TP or SL policy)."""
    horizon = n_bars + hold_bars
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")
    closes = df["close"].to_numpy(dtype="float64")
    out: List[Tuple[np.ndarray, np.ndarray, np.ndarray, bool]] = []
    for j, side, entry in reversed(signal_points(df, records_by_side, track, horizon, last_idx)):
        path = _signed_paths(side, entry, highs, lows, closes, j, horizon)
        if path is None:
            continue
        out.append((path[0], path[1], path[2], side == "long"))
        if len(out) >= lookback_signals:
            break
    out.reverse()
    return out


def _simulate_depths(fav: np.ndarray, adv: np.ndarray, cl: np.ndarray, n_bars: int,
                     tp1: float, sl: float, depths: np.ndarray, is_long: bool,
                     keep_distance: bool) -> Tuple[np.ndarray, np.ndarray]:
    """What one past signal would have paid for each candidate limit depth.

    fav / adv / cl — the signed path from _signed_paths(); tp1 / sl — the
    TP1 and SL distances as fractions of the signal price; depths — fractions
    of the signal price, depths[i] == 0 meaning "enter at the signal close".

    Rules, identical to the backtest's limit-entry logic:
      * the order lives n_bars bars; a bar that reaches TP1 before the fill,
        or in the same bar as the fill, means "missed" (the move left);
      * after the fill the stop is checked from the fill bar itself, stop
        first when a bar touches both stop and TP1;
      * TP1 stays at its absolute level; the stop either keeps its absolute
        level or shifts with the entry (keep_distance);
      * the trade is a TP1 win, an SL loss, or — when neither happens within
        the path — marked to the last close.
    A signal that is missed / expires pays 0 (what skipping a trade costs).
    Returns (PnL in % of the fill price, filled flag) per depth.
    """
    width = fav.size
    never = width + 1
    hit = np.flatnonzero(fav >= tp1)
    tp_bar = int(hit[0]) + 1 if hit.size else never
    # worst excursion seen so far inside the wait window (non-increasing),
    # negated so it is non-decreasing and searchsorted can find the first fill
    neg_run_worst = -np.minimum.accumulate(adv[:n_bars])
    pnl = np.zeros(depths.size)
    filled = np.zeros(depths.size, dtype=bool)
    for i, d in enumerate(depths):
        if d > 0.0:
            fill_bar = int(np.searchsorted(neg_run_worst, d, side="left")) + 1
            if fill_bar > n_bars or tp_bar <= fill_bar:
                continue  # expired / missed -> no trade
            stop_x = -(sl + d) if keep_distance else -sl
            first = fill_bar
        else:
            stop_x = -sl
            first = 1
        stops = np.flatnonzero(adv[first - 1:] <= stop_x)
        stop_bar = first + int(stops[0]) if stops.size else never
        if stop_bar <= tp_bar and stop_bar <= width:
            exit_x = stop_x
        elif tp_bar <= width:
            exit_x = tp1
        else:
            exit_x = float(cl[-1])
        fill_den = (1.0 - d) if is_long else (1.0 + d)
        pnl[i] = (exit_x + d) / fill_den * 100.0
        filled[i] = True
    return pnl, filled


def _best_depth(paths: List[Tuple[np.ndarray, np.ndarray, np.ndarray, bool]],
                n_bars: int,
                tp1_dist_pct: float,
                sl_dist_pct: float,
                max_sl_fraction: float,
                min_depth_pct: float,
                keep_distance: bool) -> Optional[Dict]:
    """Picks the limit depth with the best expected PnL per signal over the
    given history paths, or None when no limit depth beats entering at market.

    The depth grid runs from min_depth_pct to the same SL-fraction cap the
    average-based depth obeys. The PnL curve over the grid is lightly smoothed
    ([1, 2, 1] weights) before the argmax so a single lucky sample cannot pull
    the depth to a spike."""
    cap = max(0.0, max_sl_fraction) * max(0.0, sl_dist_pct)
    lo = max(min_depth_pct, 1e-6)
    if cap < lo:
        return None
    grid = np.linspace(lo, cap, OPT_GRID_POINTS) if cap > lo else np.array([lo])
    depths = np.concatenate(([0.0], grid)) / 100.0
    tp1 = tp1_dist_pct / 100.0
    sl = sl_dist_pct / 100.0

    pnl_sum = np.zeros(depths.size)
    fills = np.zeros(depths.size)
    pullbacks: List[float] = []
    for fav, adv, cl, is_long in paths:
        p, f = _simulate_depths(fav, adv, cl, n_bars, tp1, sl, depths, is_long, keep_distance)
        pnl_sum += p
        fills += f
        pullbacks.append(max(0.0, -float(adv[:n_bars].min())) * 100.0)
    n = len(paths)
    curve = pnl_sum / n
    fill_rate = fills / n

    limit_curve = curve[1:]
    if limit_curve.size >= 3:
        padded = np.concatenate(([limit_curve[0]], limit_curve, [limit_curve[-1]]))
        smooth = 0.25 * padded[:-2] + 0.5 * padded[1:-1] + 0.25 * padded[2:]
    else:
        smooth = limit_curve
    k = int(np.argmax(smooth))
    market_pnl = float(curve[0])
    if float(smooth[k]) <= market_pnl + OPT_MIN_EDGE_PCT:
        return None
    return {
        "depth_pct": float(grid[k]),
        "mean_pct": float(np.mean(pullbacks)),
        "n": int(n),
        "est_fill_rate": float(fill_rate[1 + k]),
        "capped": bool(grid.size > 1 and k == grid.size - 1),
        "mode": "optimize",
        "exp_pnl_pct": float(curve[1 + k]),
        "market_pnl_pct": market_pnl,
    }


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
                           min_depth_pct: float,
                           pool_fallback: bool = False,
                           depth_mode: str = "mean",
                           tp1_dist_pct: Optional[float] = None,
                           hold_bars: int = 0,
                           keep_distance: bool = False) -> Optional[Dict]:
    """How deep to place the limit order (in % below/above the signal price).

    depth_mode="mean" (default): depth = mult * mean(early adverse excursion
    of past signals), then capped at max_sl_fraction * (this signal's SL
    distance) so the entry can never end up right next to the stop.

    depth_mode="optimize": the depth is the one that would have paid best on
    the track's own history. Every past signal's price path (from OHLCV) is
    replayed against this signal's TP1 / SL distances for a grid of depths,
    counting missed and expired signals as 0, and the depth with the highest
    expected PnL per signal wins — provided it beats entering at market, else
    the answer is None (market). `mult` is not used in this mode. It needs
    tp1_dist_pct (> 0), hold_bars (>= 1) and sl_dist_pct (> 0); when any is
    missing it quietly falls back to "mean". keep_distance must match the
    sl_mode the order will actually be placed with. Only signals whose whole
    wait + hold horizon is already closed are used (no look-ahead), so the
    sample ends hold_bars further back than in "mean" mode.

    Returns None — meaning "enter at market as usual" — when there aren't
    enough past signals (min_samples) or the resulting depth is too shallow
    to be worth waiting for (min_depth_pct). With pool_fallback, a track
    that has too few signals of its own is measured over all tracks of the
    pair/timeframe instead (see the module docstring).

    Returned dict: depth_pct, mean_pct, n, est_fill_rate (share of the
    samples whose pullback reached the chosen depth), capped (bool),
    pooled (bool — measured over all tracks), mode ("mean" | "optimize"),
    and in optimize mode also exp_pnl_pct / market_pnl_pct (expected PnL per
    signal at the chosen depth vs entering at market, in %).
    """
    # 🆕 FIX: a NaN/inf input (e.g. a zero-distance SL producing nan) used to
    # slip through every comparison below and come out as depth_pct=nan, i.e. a
    # limit order at price nan. Non-finite knobs mean "no limit order".
    if not all(np.isfinite(x) for x in (mult, sl_dist_pct, max_sl_fraction, min_depth_pct)):
        return None
    if (depth_mode == "optimize" and tp1_dist_pct is not None and np.isfinite(tp1_dist_pct)
            and tp1_dist_pct > 0 and hold_bars >= 1 and sl_dist_pct > 0 and len(df) > 0):
        paths = _history_paths(df, records_by_side, track, n_bars, hold_bars, last_idx, lookback_signals)
        pooled = False
        if len(paths) < min_samples and pool_fallback:
            paths = _history_paths(df, records_by_side, None, n_bars, hold_bars, last_idx, lookback_signals)
            pooled = True
        if len(paths) < min_samples:
            return None
        best = _best_depth(paths, n_bars, float(tp1_dist_pct), sl_dist_pct,
                           max_sl_fraction, min_depth_pct, keep_distance)
        if best is None:
            return None
        best["pooled"] = pooled
        return best
    samples = pullback_samples(df, records_by_side, track, n_bars, last_idx, lookback_signals)
    pooled = False
    if len(samples) < min_samples and pool_fallback:
        samples = pullback_samples(df, records_by_side, None, n_bars, last_idx, lookback_signals)
        pooled = True
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
        "pooled": pooled,
        "mode": "mean",
    }


def limit_price_for(side: str, signal_price: float, depth_pct: float) -> float:
    """Limit price `depth_pct` against the signal price (below for a long,
    above for a short)."""
    f = depth_pct / 100.0
    return signal_price * (1.0 - f) if side == "long" else signal_price * (1.0 + f)


def shifted_sl(side: str, signal_price: float, limit: float, sl: float) -> float:
    """The "keep_distance" stop: the original stop moved by the same amount
    the entry improved, so the distance entry -> stop (the risk per trade)
    equals what it was at the signal price. Long: below the signal stop;
    short: above it."""
    gain = (signal_price - limit) if side == "long" else (limit - signal_price)
    return sl - gain if side == "long" else sl + gain


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
