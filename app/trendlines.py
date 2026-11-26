"""
MUFCA v4.3 - Daily trend lines + forming pattern (web "TA" tab).

VISUAL / INFORMATIONAL ONLY. Nothing in the signal path (signals.py, filters,
confidence) reads this module, and it has not been measured against trade
outcomes. Pattern recognition is partly subjective, so every rule below is a
fixed, explicit threshold instead of a fitted one.

Method (CLOSED daily bars only - the forming bar is never used, so a line can only
change when a new bar closes or a new pivot is confirmed):

 1. Pivots: fractal swing highs/lows, confirmed `pivot_n` bars after they form.
 2. Trend lines: for each side (pivot highs -> resistance, pivot lows -> support)
    every pair of pivots defines a candidate line in LOG price space (so a line
    means the same thing at 3k and at 100k). A candidate is kept only if
      - at least `min_touches` pivots sit on it (within `touch_atr` x ATR%),
      - no close went beyond it (by more than `break_atr` x ATR%) and no wick
        went beyond it (by more than `wick_atr` x ATR%) before the last
        `break_recent` bars - the most recent bars are where a breakout may show,
      - its last touch is not stale and price is still near it.
 3. Pattern: the best compatible resistance + support pair (lines that do not
    cross inside their common span) is classified by slopes and convergence into
    triangle / wedge / channel / flag / range, with the apex and a
    status (forming, breakout up, breakout down).
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Defaults; config.py exposes the same names with a TA_ prefix (see params_from_config).
DEFAULTS: Dict[str, float] = {
    "pivot_n": 4,            # bars each side for a fractal pivot (confirmed pivot_n bars later)
    "lookback": 240,         # closed bars searched for pivots
    "min_touches": 3,        # pivots that must sit on a line (anchors included)
    "touch_atr": 0.5,        # a pivot "touches" the line within this many ATR% (log distance)
    "wick_atr": 1.0,         # max wick excursion beyond a line before its last `break_recent` bars
    "break_atr": 0.5,        # a close this many ATR% beyond a line = a break
    "break_recent": 5,       # bars at the end where a break is reported instead of rejecting the line
    "min_span": 10,          # min bars between the two anchor pivots of a line
    "max_pivots": 14,        # newest pivots per side considered as anchors
    "max_stale": 45,         # max bars since the line's last touch
    "max_dist_atr": 8.0,     # price must be within this many ATR% of a line
    "top_lines": 5,          # candidate lines per side kept for pair selection
    "min_gap_atr": 1.0,      # min width (ATR%) between the two lines over their common span
    "min_pattern_bars": 20,  # min common span of the two lines for a pattern
    "min_alternations": 3,   # touches of the two lines, in time order, must switch side this many times
    "flat_atr": 1.0,         # a line is "flat" if it moves less than max(this many ATR%, flat_width x start width)
    "flat_width": 0.2,       # ... over the pattern
    "converge_ratio": 0.8,   # width now / width at start below this = converging
    "diverge_ratio": 1.25,   # ... above this = widening (not reported), in between = parallel
    "flag_max_bars": 30,     # a parallel channel this short can be a flag
    "pole_bars": 15,         # bars before the pattern that measure the flag pole
    "pole_atr": 5.0,         # min pole move (ATR%) for a flag
    "project_bars": 30,      # how far a line is projected past the last bar (stops at the apex)
    "atr_period": 14,
}

MIN_BARS = 60  # fewer closed bars than this: nothing is computed

_CACHE: Dict[tuple, Dict] = {}
_CACHE_MAX = 16


def params_from_config(cfg) -> Dict[str, float]:
    """DEFAULTS overridden by the TA_* constants of the config module `cfg` (whichever exist)."""
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        v = getattr(cfg, "TA_" + key.upper(), None)
        if v is not None:
            out[key] = v
    return out


# =====================================================================
# Building blocks
# =====================================================================
def _atr_frac(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Wilder ATR as a fraction of the close (~ log distance for small moves)."""
    prev = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    atr = pd.Series(tr).ewm(alpha=1.0 / max(1, period), adjust=False).mean().to_numpy()
    return atr / close


def find_pivots(high: np.ndarray, low: np.ndarray, n: int, start: int = 0) -> Tuple[List[int], List[int]]:
    """Fractal pivots over all bars: a high that beats the `n` bars before it and is not beaten by
    the `n` after it (and the mirror for lows). A pivot needs `n` bars on its right, so the newest
    `n` bars never hold a pivot - that is the confirmation delay, and why nothing repaints."""
    last = len(high) - 1
    highs: List[int] = []
    lows: List[int] = []
    for i in range(max(n, start), last - n + 1):
        if high[i] > high[i - n:i].max() and high[i] >= high[i + 1:i + n + 1].max():
            highs.append(i)
        if low[i] < low[i - n:i].min() and low[i] <= low[i + 1:i + n + 1].min():
            lows.append(i)
    return highs, lows


def _candidate_lines(side: str, piv: List[int], y_ext: np.ndarray, y_close: np.ndarray,
                     a: float, last: int, p: Dict[str, float]) -> List[Dict]:
    """All valid lines for one side, best first. side: 'resistance' (y_ext = log highs) or
    'support' (y_ext = log lows). Lines live in log-price space: L(i) = y[ia] + m * (i - ia)."""
    sign = 1.0 if side == "resistance" else -1.0
    tol = p["touch_atr"] * a
    wick_tol = p["wick_atr"] * a
    brk = p["break_atr"] * a
    recent = int(p["break_recent"])
    piv = piv[-int(p["max_pivots"]):]
    out: List[Dict] = []
    for ai in range(len(piv)):
        for bi in range(ai + 1, len(piv)):
            ia, ib = piv[ai], piv[bi]
            if ib - ia < p["min_span"]:
                continue
            m = (y_ext[ib] - y_ext[ia]) / (ib - ia)
            xs = np.arange(ia, last + 1)
            line = y_ext[ia] + m * (xs - ia)
            # strict region: no excursion beyond the line. The last `recent` bars are exempt so a fresh
            # break is reported as a break (and the line is dropped only once the break is older).
            strict_end = max(last - recent, ib)
            k = strict_end - ia + 1
            if (sign * (y_ext[ia:strict_end + 1] - line[:k])).max() > wick_tol:
                continue
            if (sign * (y_close[ia:strict_end + 1] - line[:k])).max() > brk:
                continue
            touches = [j for j in piv if j >= ia and abs(y_ext[j] - line[j - ia]) <= tol]
            if len(touches) < p["min_touches"]:
                continue
            since_touch = last - touches[-1]
            if since_touch > p["max_stale"]:
                continue
            dist = y_close[last] - line[-1]
            if abs(dist) > p["max_dist_atr"] * a:
                continue
            span = touches[-1] - touches[0]
            score = len(touches) * 10.0 + min(span, 150) / 10.0 - since_touch / 10.0
            out.append({
                "side": side, "sign": sign, "ia": ia, "m": float(m), "y0": float(y_ext[ia]),
                "touches": touches, "first": touches[0], "score": score, "strict_end": strict_end,
            })
    out.sort(key=lambda c: c["score"], reverse=True)
    return out[:int(p["top_lines"])]


def _line_y(c: Dict, i) -> np.ndarray | float:
    return c["y0"] + c["m"] * (np.asarray(i, dtype=float) - c["ia"])


def _pick_pair(res: List[Dict], sup: List[Dict], a: float, last: int, p: Dict[str, float]) -> Optional[Tuple[Dict, Dict]]:
    """Best resistance + support pair whose lines stay `min_gap_atr` apart over their common span, last
    at least `min_pattern_bars`, and whose touches alternate between the two lines."""
    best, best_score = None, -1e9
    for r in res:
        for s in sup:
            start = max(r["first"], s["first"])
            if last - start < p["min_pattern_bars"]:
                continue
            xs = np.arange(start, last + 1)
            gap = _line_y(r, xs) - _line_y(s, xs)
            if gap.min() < p["min_gap_atr"] * a:
                continue
            # a real pattern is price bouncing between the two lines: touches must alternate in time
            seq = sorted([(j, 0) for j in r["touches"] if j >= start] + [(j, 1) for j in s["touches"] if j >= start])
            if sum(1 for k in range(len(seq) - 1) if seq[k][1] != seq[k + 1][1]) < p["min_alternations"]:
                continue
            score = r["score"] + s["score"]
            if score > best_score:
                best, best_score = (r, s), score
    return best


def _classify(r: Dict, s: Dict, start: int, last: int, a: float, y_close: np.ndarray,
              p: Dict[str, float]) -> Tuple[Optional[str], Optional[float]]:
    """Pattern type id (None = lines do not form a named pattern) and bars-to-apex (None unless converging)."""
    T = last - start
    g0 = float(_line_y(r, start) - _line_y(s, start))
    g1 = float(_line_y(r, last) - _line_y(s, last))
    ratio = g1 / g0 if g0 > 0 else 1.0
    flat = max(p["flat_atr"] * a, p["flat_width"] * g0)   # log move below which a line counts as flat
    dr = r["m"] * T                          # line move over the pattern (log)
    ds = s["m"] * T
    r_flat, s_flat = abs(dr) < flat, abs(ds) < flat

    if ratio <= p["converge_ratio"]:
        rate = (g0 - g1) / T
        apex = g1 / rate if rate > 0 else None
        if r_flat and ds > 0:
            return "ascending_triangle", apex
        if s_flat and dr < 0:
            return "descending_triangle", apex
        if dr > 0 and ds > 0 and not r_flat:
            return "rising_wedge", apex
        if dr < 0 and ds < 0 and not s_flat:
            return "falling_wedge", apex
        return "symmetrical_triangle", apex
    if ratio >= p["diverge_ratio"]:
        return None, None          # widening lines: not reported as a pattern (mostly noise); lines still drawn

    # parallel lines
    avg = (dr + ds) / 2.0
    if abs(avg) < flat:
        return "range", None
    if T <= p["flag_max_bars"]:
        pole_start = max(0, start - int(p["pole_bars"]))
        if start > pole_start:
            pole = (y_close[start] - y_close[pole_start]) / a
            if pole >= p["pole_atr"] and avg < 0:
                return "bull_flag", None
            if pole <= -p["pole_atr"] and avg > 0:
                return "bear_flag", None
    return ("channel_up" if avg > 0 else "channel_down"), None


def _px(v: float) -> float:
    return round(float(v), 8)


def _line_json(c: Dict, ts_s: np.ndarray, bar_s: int, last: int, y_close: np.ndarray, a: float,
               end_idx: int, p: Dict[str, float]) -> Dict:
    """Serializable line + its break status. end_idx = projection end (bar index, may be > last)."""
    def time_at(i: int) -> int:
        return int(ts_s[i]) if i <= last else int(ts_s[last] + (i - last) * bar_s)

    sign = c["sign"]
    brk = p["break_atr"] * a
    first_i = c["first"]
    now = float(_line_y(c, last))

    broken_at = None
    for i in range(c["strict_end"] + 1, last + 1):
        if sign * (y_close[i] - _line_y(c, i)) > brk:
            broken_at = i
            break
    beyond_now = sign * (y_close[last] - now) > brk
    if broken_at is None:
        status, since = "intact", None
    else:
        status = "broken" if beyond_now else "reclaimed"
        since = last - broken_at

    dist = float(y_close[last] - now)
    return {
        "side": c["side"],
        "touches": len(c["touches"]),
        "touch_points": [{"time": time_at(j), "price": _px(math.exp(_line_y(c, j)))} for j in c["touches"]],
        "start": {"time": time_at(first_i), "price": _px(math.exp(_line_y(c, first_i)))},
        "end": {"time": time_at(last), "price": _px(math.exp(now))},
        "projection": {"time": time_at(end_idx), "price": _px(math.exp(_line_y(c, end_idx)))},
        "slope_pct_per_bar": round((math.exp(c["m"]) - 1.0) * 100.0, 4),
        "dist_pct": round((math.exp(dist) - 1.0) * 100.0, 2),   # + = last close above the line
        "dist_atr": round(dist / a, 2),
        "status": status,
        "bars_since_break": since,
    }


# =====================================================================
# Public API
# =====================================================================
def analyze(df: pd.DataFrame, **overrides) -> Dict:
    """Trend lines + pattern from CLOSED daily candles.

    df: columns timestamp (ms), open, high, low, close, volume; last row = last closed bar.
    Returns {"lines": [...], "pattern": {...} | None, "atr_pct", "reason", "params"}; every time is
    unix seconds. Never raises on thin/odd data - it returns empty lines with a `reason`."""
    p = dict(DEFAULTS)
    p.update({k: v for k, v in overrides.items() if k in DEFAULTS and v is not None})
    empty = {"lines": [], "pattern": None, "atr_pct": None, "reason": None, "params": p}

    n_all = len(df)
    if n_all < MIN_BARS:
        empty["reason"] = "insufficient_data"
        return empty

    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    if not (np.isfinite(high).all() and np.isfinite(low).all() and np.isfinite(close).all()) \
            or (low <= 0).any():
        empty["reason"] = "bad_data"
        return empty

    ts_s = (df["timestamp"].to_numpy(dtype=np.int64) // 1000)
    bar_s = int(np.median(np.diff(ts_s))) if n_all > 1 else 86400
    last = n_all - 1
    start = max(0, n_all - int(p["lookback"]))

    atr_f = _atr_frac(high, low, close, int(p["atr_period"]))
    a = float(np.median(atr_f[start:]))
    if not (a > 0 and math.isfinite(a)):
        empty["reason"] = "bad_data"
        return empty
    empty["atr_pct"] = round(a * 100.0, 3)

    y_hi, y_lo, y_cl = np.log(high), np.log(low), np.log(close)
    hi_piv, lo_piv = find_pivots(high, low, int(p["pivot_n"]), start)
    res = _candidate_lines("resistance", hi_piv, y_hi, y_cl, a, last, p)
    sup = _candidate_lines("support", lo_piv, y_lo, y_cl, a, last, p)
    if not res and not sup:
        empty["reason"] = "no_lines"
        return empty

    pair = _pick_pair(res, sup, a, last, p) if (res and sup) else None
    chosen = list(pair) if pair else ([res[0]] if res else []) + ([sup[0]] if sup else [])

    pattern = None
    apex_bars: Optional[float] = None
    ptype = None
    pstart = None
    if pair:
        r, s = pair
        pstart = max(r["first"], s["first"])
        ptype, apex_bars = _classify(r, s, pstart, last, a, y_cl, p)
        if ptype is None:
            pair = None

    proj = int(p["project_bars"])
    end_idx = last + proj
    if apex_bars is not None:
        end_idx = last + max(1, min(proj, int(apex_bars)))
    lines = [_line_json(c, ts_s, bar_s, last, y_cl, a, end_idx, p) for c in chosen]

    if pair:
        r, s = pair
        rl = next(l for l in lines if l["side"] == "resistance")
        sl = next(l for l in lines if l["side"] == "support")
        up = rl["status"] == "broken"
        down = sl["status"] == "broken"
        if up and not down:
            status, since = "breakout_up", rl["bars_since_break"]
        elif down and not up:
            status, since = "breakout_down", sl["bars_since_break"]
        else:
            status, since = "forming", None
        r_now, s_now = float(_line_y(r, last)), float(_line_y(s, last))
        width = r_now - s_now
        T = last - pstart
        pattern = {
            "type": ptype,
            "status": status,
            "bars_since_break": since,
            "bars": int(T),
            "start_time": int(ts_s[pstart]),
            "position": round(float((y_cl[last] - s_now) / width), 3) if width > 0 else None,  # 0 = support, 1 = resistance
            "width_pct": round((math.exp(width) - 1.0) * 100.0, 2),
            "resistance_touches": rl["touches"],
            "support_touches": sl["touches"],
            "bars_to_apex": int(apex_bars) if apex_bars is not None else None,
            "apex_progress": round(T / (T + apex_bars), 3) if apex_bars is not None else None,
            "apex_time": int(ts_s[last] + int(apex_bars) * bar_s) if apex_bars is not None else None,
        }

    return {
        "lines": lines,
        "pattern": pattern,
        "atr_pct": round(a * 100.0, 3),
        "reason": None if pattern else ("no_pattern" if lines else "no_lines"),
        "params": p,
    }


def analyze_cached(symbol: str, df: pd.DataFrame, **overrides) -> Dict:
    """analyze() memoized per (symbol, last closed bar, params): between two daily closes the answer
    cannot change, so repeated dashboard refreshes cost nothing."""
    p = dict(DEFAULTS)
    p.update({k: v for k, v in overrides.items() if k in DEFAULTS and v is not None})
    key = (symbol, int(df["timestamp"].iloc[-1]), len(df), tuple(sorted(p.items())))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    res = analyze(df, **overrides)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = res
    return res


def clear_cache() -> None:
    _CACHE.clear()


async def get_ta_data(exchange, symbol: str, cfg, candles_limit: int = 300) -> Dict:
    """Everything the web TA tab needs from one daily OHLCV fetch: candles (the forming bar is
    included, flagged), trend lines + pattern (closed bars only) and daily S/R for context."""
    from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe  # lazy: keeps analyze() importable without ccxt
    from market_structure import calc_support_resistance

    params = params_from_config(cfg)
    fetch_limit = int(params["lookback"]) + 120
    bars = await safe_fetch_ohlcv(exchange, symbol, "1d", limit=max(fetch_limit, candles_limit + 20))
    df = parse_ohlcv(bars)
    if not validate_dataframe(df, min_rows=MIN_BARS + 1):
        raise ValueError(f"Not enough daily data for {symbol}")

    closed = df.iloc[:-1].reset_index(drop=True)      # the last row is the still-forming daily bar
    ta = analyze_cached(symbol, closed, **params)

    sr = calc_support_resistance(
        closed, pivot_window=cfg.SR_PIVOT_WINDOW, max_levels=cfg.SR_MAX_LEVELS,
        ref_price=float(closed["close"].iloc[-1]),
    )
    tail = df.tail(candles_limit)
    n_tail = len(tail)
    candles = [
        {
            "time": int(row.timestamp // 1000),
            "open": _px(row.open), "high": _px(row.high), "low": _px(row.low), "close": _px(row.close),
            "forming": bool(i == n_tail - 1),
        }
        for i, row in enumerate(tail.itertuples())
    ]
    return {
        "symbol": symbol,
        "timeframe": "1d",
        "as_of": int(closed["timestamp"].iloc[-1] // 1000),
        "candles": candles,
        "lines": ta["lines"],
        "pattern": ta["pattern"],
        "atr_pct": ta["atr_pct"],
        "reason": ta["reason"],
        "support": sr["support"],
        "resistance": sr["resistance"],
    }
