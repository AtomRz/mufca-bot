"""
MUFCA - Smart Money Concepts study (!smcsim, /api/smcsim)

RESEARCH TOOL, NOT A SIGNAL SOURCE. Nothing in this module feeds entries,
TP/SL, confidence or any filter. It answers one question: do SMC events
(liquidity sweeps, break of structure, change of character, market-structure
shift after a sweep, fair value gaps) carry any forward edge on OUR pairs and
timeframes, compared with simply being in the market?

How the study works
  1. detect_events() walks the candles once, bar by bar, keeping only what a
     live bot would know at that bar's close (see "No look-ahead" below).
  2. For every event, the trade is "enter at the NEXT bar's open, in the
     event's direction" and three things are measured over fixed horizons:
       - the directional return at the horizon's close (%),
       - a 1-ATR race: which is hit first, +1 ATR (win) or -1 ATR (loss).
         Both inside one bar counts as a loss (conservative). Neither within
         the horizon = undecided, left out of the win rate.
  3. The same numbers are computed for EVERY bar (the unconditional baseline,
     per direction). An event only means something if it beats the baseline
     for its own direction and horizon - a rising market makes every long look
     good, so raw returns on their own prove nothing.

No look-ahead
  - A swing pivot needs `swing_n` bars on each side, so it is only usable
    `swing_n` bars AFTER it formed; it is registered on the bar that confirms it.
  - Sweeps and breaks on bar t are judged against levels confirmed BEFORE
    bar t's own pivot update.
  - The entry is the next bar's open, never the signal bar's close.
  Property that was checked when this was written: events computed on a
  truncated frame are identical to the same events on the full frame (for all
  bars before the cut). Re-check it if detect_events() is ever changed.

Parameters are fixed constants on purpose. Tuning swing length, windows or
thresholds against the same history this study reports on is exactly how a
fake edge is manufactured. Change them only with a reason decided up front.

All source comments and strings are ASCII-only.
"""

import asyncio
import logging
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Fixed study parameters (see module docstring: do not tune on the result)
# ---------------------------------------------------------------------
SWING_N = 3               # bars on each side that confirm a swing high/low
MSS_WINDOW = 8            # a break must follow a sweep within this many bars
FVG_MIN_ATR = 0.25        # a fair value gap must be at least this many ATR wide
ATR_PERIOD = 14
HORIZONS = (6, 12, 24)    # bars held after entry
RACE_ATR = 1.0            # win = +1 ATR first, loss = -1 ATR first
EVENT_COOLDOWN = 6        # same event name closer than this many bars is dropped
MIN_BARS = 200

# name -> (direction, plain-language meaning)
EVENT_INFO: Dict[str, Tuple[int, str]] = {
    "sweep_low": (1, "wick below the last swing low, closed back above it"),
    "sweep_high": (-1, "wick above the last swing high, closed back below it"),
    "bos_up": (1, "close above the last swing high, structure already up (or unknown)"),
    "bos_down": (-1, "close below the last swing low, structure already down (or unknown)"),
    "choch_up": (1, "close above the last swing high while structure was down"),
    "choch_down": (-1, "close below the last swing low while structure was up"),
    "fvg_bull": (1, "three-candle gap up (low[t] > high[t-2]) of at least FVG_MIN_ATR"),
    "fvg_bear": (-1, "three-candle gap down (high[t] < low[t-2]) of at least FVG_MIN_ATR"),
    "sweep_mss_long": (1, "sweep of a low, then a close above the last swing high within MSS_WINDOW bars"),
    "sweep_mss_short": (-1, "sweep of a high, then a close below the last swing low within MSS_WINDOW bars"),
}
# events additionally split by where price sat in the dealing range
_SPLIT_BY_ZONE = ("sweep_mss_long", "sweep_mss_short")


def study_params() -> Dict:
    return {
        "swing_n": SWING_N, "mss_window": MSS_WINDOW, "fvg_min_atr": FVG_MIN_ATR,
        "atr_period": ATR_PERIOD, "horizons": list(HORIZONS), "race_atr": RACE_ATR,
        "event_cooldown": EVENT_COOLDOWN, "entry": "next bar open",
    }


# =====================================================================
# Helpers
# =====================================================================

def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Simple rolling-mean ATR; NaN until `period` bars exist."""
    n = len(close)
    out = np.full(n, np.nan)
    if n < period:
        return out
    prev_close = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    csum = np.cumsum(np.concatenate(([0.0], tr)))
    out[period - 1:] = (csum[period:] - csum[:-period]) / period
    return out


def _num(x, digits: int = 4):
    """JSON-safe number: NaN/inf -> None."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, digits) if np.isfinite(x) else None


# =====================================================================
# Event detection
# =====================================================================

def detect_events(df: pd.DataFrame, swing_n: int = SWING_N, mss_window: int = MSS_WINDOW,
                  fvg_min_atr: float = FVG_MIN_ATR) -> Tuple[List[Tuple[int, str, float]], np.ndarray]:
    """Returns ([(bar_index, event_name, dealing_range_position), ...], atr).

    dealing_range_position = (close - last swing low) / (last swing high - last swing low),
    measured with the swings confirmed before this bar; NaN when no valid range
    exists yet. < 0.5 is "discount", >= 0.5 is "premium".

    Only closed candles may be passed in (the caller drops the forming bar).
    """
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(close)
    atr = _atr(high, low, close, ATR_PERIOD)

    events: List[Tuple[int, str, float]] = []
    last_sh = last_sl = np.nan
    sh_broken = sl_broken = True      # nothing to break until a pivot exists
    sh_swept = sl_swept = True
    trend = 0                          # +1 last break was up, -1 down, 0 unknown
    last_sweep_lo = last_sweep_hi = -10 ** 9

    for t in range(n):
        have_range = np.isfinite(last_sh) and np.isfinite(last_sl) and last_sh > last_sl
        pos = (close[t] - last_sl) / (last_sh - last_sl) if have_range else np.nan

        # 1) sweeps (wick through the level, close back inside) - both sides first,
        #    so a one-bar sweep + break can still be paired below
        if not sl_broken and not sl_swept and low[t] < last_sl and close[t] >= last_sl:
            sl_swept = True
            last_sweep_lo = t
            events.append((t, "sweep_low", pos))
        if not sh_broken and not sh_swept and high[t] > last_sh and close[t] <= last_sh:
            sh_swept = True
            last_sweep_hi = t
            events.append((t, "sweep_high", pos))

        # 2) breaks (close beyond the level)
        if not sh_broken and close[t] > last_sh:
            sh_broken = True
            events.append((t, "choch_up" if trend < 0 else "bos_up", pos))
            trend = 1
            if 0 <= t - last_sweep_lo <= mss_window:
                events.append((t, "sweep_mss_long", pos))
        if not sl_broken and close[t] < last_sl:
            sl_broken = True
            events.append((t, "choch_down" if trend > 0 else "bos_down", pos))
            trend = -1
            if 0 <= t - last_sweep_hi <= mss_window:
                events.append((t, "sweep_mss_short", pos))

        # 3) fair value gaps, known at this bar's close
        if t >= 2 and np.isfinite(atr[t]) and atr[t] > 0:
            if low[t] - high[t - 2] > fvg_min_atr * atr[t]:
                events.append((t, "fvg_bull", pos))
            if low[t - 2] - high[t] > fvg_min_atr * atr[t]:
                events.append((t, "fvg_bear", pos))

        # 4) register the pivot that THIS bar confirms (pivot bar = t - swing_n)
        p = t - swing_n
        if p >= swing_n:
            if high[p] > high[p - swing_n:p].max() and high[p] >= high[p + 1:t + 1].max():
                last_sh, sh_broken, sh_swept = high[p], False, False
            if low[p] < low[p - swing_n:p].min() and low[p] <= low[p + 1:t + 1].min():
                last_sl, sl_broken, sl_swept = low[p], False, False

    return events, atr


# =====================================================================
# Forward outcomes
# =====================================================================

def _forward_outcomes(df: pd.DataFrame, atr: np.ndarray, horizons: Tuple[int, ...],
                      race_atr: float) -> Tuple[Dict[int, Dict[str, np.ndarray]], np.ndarray, int]:
    """Per signal bar t (entry = open[t+1]): long return %, and the 1-ATR race
    result for both directions at every horizon. Only bars with a full window
    (t + max(horizons) <= n-1) and a usable ATR are valid.

    Returns (per_horizon_arrays, valid_mask, last_valid_index)."""
    open_ = df["open"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(close)
    hmax = max(horizons)
    last_t = n - 1 - hmax
    if last_t < 1:
        return {}, np.zeros(0, dtype=bool), -1

    t_idx = np.arange(0, last_t + 1)
    entry = open_[t_idx + 1]
    a = atr[t_idx]
    valid = np.isfinite(a) & (a > 0) & np.isfinite(entry) & (entry > 0)

    up_hit = np.full(len(t_idx), np.inf)   # first offset whose high reaches entry + race_atr * ATR
    dn_hit = np.full(len(t_idx), np.inf)   # first offset whose low reaches entry - race_atr * ATR
    up_level = entry + race_atr * a
    dn_level = entry - race_atr * a
    for k in range(1, hmax + 1):
        up_hit[(high[t_idx + k] >= up_level) & np.isinf(up_hit)] = k
        dn_hit[(low[t_idx + k] <= dn_level) & np.isinf(dn_hit)] = k

    out: Dict[int, Dict[str, np.ndarray]] = {}
    for h in horizons:
        ret_long = (close[t_idx + h] - entry) / entry * 100.0
        out[h] = {
            "ret_long": ret_long,
            # same-bar hit of both levels is a loss for either side (conservative)
            "win_long": (up_hit <= h) & (up_hit < dn_hit),
            "loss_long": (dn_hit <= h) & (dn_hit <= up_hit),
            "win_short": (dn_hit <= h) & (dn_hit < up_hit),
            "loss_short": (up_hit <= h) & (up_hit <= dn_hit),
        }
    return out, valid, last_t


def _sample_stats(ret: np.ndarray, win: np.ndarray, loss: np.ndarray) -> Dict:
    n = int(len(ret))
    if n == 0:
        return {"n": 0, "avg_ret": None, "se": None, "win_rate": None, "decided": 0}
    avg = float(ret.mean())
    se = float(ret.std(ddof=1) / np.sqrt(n)) if n >= 2 else None
    wins, losses = int(win.sum()), int(loss.sum())
    decided = wins + losses
    return {
        "n": n,
        "avg_ret": avg,
        "se": se,
        "win_rate": (wins / decided) if decided else None,
        "decided": decided,
    }


def _directional(fw: Dict[str, np.ndarray], direction: int, mask: np.ndarray) -> Dict:
    ret = fw["ret_long"][mask] * (1 if direction > 0 else -1)
    win = fw["win_long" if direction > 0 else "win_short"][mask]
    loss = fw["loss_long" if direction > 0 else "loss_short"][mask]
    return _sample_stats(ret, win, loss)


def _apply_cooldown(idx: List[int], cooldown: int) -> List[int]:
    kept: List[int] = []
    last = -10 ** 9
    for i in sorted(idx):
        if i - last >= cooldown:
            kept.append(i)
            last = i
    return kept


# =====================================================================
# Public: one pair / timeframe
# =====================================================================

def analyze_smc(df: pd.DataFrame, horizons: Tuple[int, ...] = HORIZONS) -> Dict:
    """Event study on one closed-candle DataFrame (timestamp, open, high, low,
    close, volume). Pure function: no I/O, no config, no state.

    Result:
      {"bars", "baseline": {"long": [stats per horizon], "short": [...]},
       "events": [{"name", "direction", "meaning", "count",
                   "horizons": [{"h", "n", "avg_ret", "se", "win_rate", "decided",
                                 "d_ret", "d_win", "z_ret", "z_win"}, ...]}, ...]}
    where d_* are differences against the unconditional baseline for the SAME
    direction and horizon, z_ret = d_ret / se of the event sample and z_win the
    binomial z of the win rate against the baseline win rate."""
    n = len(df)
    if n < MIN_BARS:
        raise ValueError(f"need at least {MIN_BARS} closed bars, got {n}")

    raw_events, atr = detect_events(df)
    fw_by_h, valid, last_t = _forward_outcomes(df, atr, horizons, RACE_ATR)
    if not fw_by_h:
        raise ValueError("not enough bars for the longest horizon")

    # baseline: every valid bar, both directions
    baseline = {"long": [], "short": []}
    base_cache: Dict[Tuple[int, int], Dict] = {}
    for h in horizons:
        for direction, key in ((1, "long"), (-1, "short")):
            st = _directional(fw_by_h[h], direction, valid)
            base_cache[(h, direction)] = st
            baseline[key].append({"h": h, **{k: _num(v) if isinstance(v, float) else v for k, v in st.items()}})

    # group event bar indices by name, apply the cooldown, then split by zone
    by_name: Dict[str, List[Tuple[int, float]]] = {}
    for idx, name, pos in raw_events:
        by_name.setdefault(name, []).append((idx, pos))

    groups: List[Tuple[str, str, int, List[int]]] = []   # (label, base_name, direction, bar indices)
    for name, rows in by_name.items():
        direction = EVENT_INFO[name][0]
        pos_by_idx = {i: p for i, p in rows}
        kept = _apply_cooldown([i for i, _ in rows], EVENT_COOLDOWN)
        groups.append((name, name, direction, kept))
        if name in _SPLIT_BY_ZONE:
            disc = [i for i in kept if np.isfinite(pos_by_idx[i]) and pos_by_idx[i] < 0.5]
            prem = [i for i in kept if np.isfinite(pos_by_idx[i]) and pos_by_idx[i] >= 0.5]
            groups.append((name + "|discount", name, direction, disc))
            groups.append((name + "|premium", name, direction, prem))

    order = {k: i for i, k in enumerate(EVENT_INFO)}
    groups.sort(key=lambda g: (order.get(g[1], 99), g[0]))

    events_out: List[Dict] = []
    for label, base_name, direction, idxs in groups:
        usable = np.array([i for i in idxs if i <= last_t], dtype=int)
        mask = np.zeros(len(valid), dtype=bool)
        if len(usable):
            mask[usable] = True
        mask &= valid
        per_h = []
        for h in horizons:
            st = _directional(fw_by_h[h], direction, mask)
            base = base_cache[(h, direction)]
            d_ret = d_win = z_ret = z_win = None
            if st["n"] and base["avg_ret"] is not None and st["avg_ret"] is not None:
                d_ret = st["avg_ret"] - base["avg_ret"]
                if st["se"]:
                    z_ret = d_ret / st["se"]
            if st["decided"] and base["win_rate"] is not None and st["win_rate"] is not None:
                d_win = st["win_rate"] - base["win_rate"]
                p0 = base["win_rate"]
                if 0 < p0 < 1:
                    z_win = d_win / np.sqrt(p0 * (1 - p0) / st["decided"])
            per_h.append({
                "h": h, "n": st["n"], "decided": st["decided"],
                "avg_ret": _num(st["avg_ret"]), "se": _num(st["se"]), "win_rate": _num(st["win_rate"]),
                "d_ret": _num(d_ret), "d_win": _num(d_win), "z_ret": _num(z_ret, 2), "z_win": _num(z_win, 2),
            })
        events_out.append({
            "name": label, "direction": direction, "meaning": EVENT_INFO[base_name][1],
            "count": int(mask.sum()), "horizons": per_h,
        })

    return {"bars": n, "baseline": baseline, "events": events_out}


# =====================================================================
# Public: all tracked pairs / timeframes (shared by !smcsim and /api/smcsim)
# =====================================================================

async def run_smc_study(exchange, tickers: List[str], tfs: List[str], num_bars: int = 3000,
                        progress: Optional[Callable[[int, int, str], None]] = None) -> Dict:
    """Fetches closed-candle history per pair/timeframe and runs analyze_smc().
    Dry run: nothing is written anywhere, no setting is touched.

    Returns {"params", "bar_coverage": [{"ticker","tf","requested","got"}],
             "skipped": [{"ticker","tf","reason"}],
             "results": [{"ticker","tf","bars","baseline","events"}]}.
    Same job contract as the other simulations (see sim_jobs.py): `progress`
    is called as progress(done, total, message)."""
    # lazy imports: signals.py is heavy and imports the simulation plumbing
    from signals import _fetch_ohlcv_paginated, _report_progress
    from utils import parse_ohlcv

    total = len(tickers) * len(tfs)
    done = 0
    bar_coverage: List[Dict] = []
    skipped: List[Dict] = []
    results: List[Dict] = []
    _report_progress(progress, 0, total, "starting")

    for t in tickers:
        for f in tfs:
            _report_progress(progress, done, total, f"fetching {t} {f}")
            try:
                bars = await asyncio.to_thread(_fetch_ohlcv_paginated, exchange, t, f, num_bars)
            except Exception as e:
                logger.error(f"run_smc_study: fetch error for {t} {f}: {e}")
                skipped.append({"ticker": t, "tf": f, "reason": f"fetch failed: {e}"})
                done += 1
                continue
            got = len(bars or [])
            bar_coverage.append({"ticker": t, "tf": f, "requested": num_bars, "got": got})
            if got < MIN_BARS + 1:
                skipped.append({"ticker": t, "tf": f, "reason": f"only {got} bars"})
                done += 1
                continue

            # the last candle is still forming: its high/low/close are not final
            df = parse_ohlcv(bars).iloc[:-1].reset_index(drop=True)
            _report_progress(progress, done, total, f"analysing {t} {f}")
            try:
                res = await asyncio.to_thread(analyze_smc, df)
            except Exception as e:
                logger.error(f"run_smc_study: analysis error for {t} {f}: {e}", exc_info=True)
                skipped.append({"ticker": t, "tf": f, "reason": f"analysis failed: {e}"})
                done += 1
                continue
            results.append({"ticker": t, "tf": f, **res})
            done += 1
            _report_progress(progress, done, total, f"finished {t} {f}")

    _report_progress(progress, total, total, "finished")
    return {"params": study_params(), "bar_coverage": bar_coverage, "skipped": skipped, "results": results}
