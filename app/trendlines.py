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

Also here (same tab, same informational-only rule): a Nadaraya-Watson Envelope (`nw_envelope`: the
smoothed/repainting variant as on TradingView, and a causal variant), EMA 50 / EMA 200 of the closes and three
single/two-bar candle patterns (Pin Bar, Engulfing, Inside Bar), see `ema_series`,
`ma_summary` and `detect_candle_patterns`. They are pure geometry of closed bars: they say
what the candles look like, not what price will do next.
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

EMA_PERIODS = (50, 200)
EMA_WARMUP_BARS = 400      # extra daily bars fetched beyond the displayed ones (EMA 200 settling, NWE window)

# Nadaraya-Watson Envelope (LuxAlgo-style): Gaussian kernel regression of the closes, bands = fit +/- mult x MAE.
NWE = {"h": 8.0, "mult": 3.0, "window": 500, "mae_len": 499, "mae_min": 100}

# Candle-pattern rules (fixed on purpose, not fitted). ATR is the absolute Wilder ATR(14) of the bar.
CANDLE = {
    "min_range_atr": 0.8,      # pin bar / engulfing: the bar's range must be at least this many ATR
    "pin_body_max": 0.30,      # pin bar: body <= this fraction of the bar's range
    "pin_wick_min": 0.60,      # pin bar: the long wick >= this fraction of the range ...
    "pin_nose_max": 0.20,      # ... and the opposite wick <= this fraction of the range
    "eng_prev_body_min": 0.25, # engulfing: the engulfed (previous) body >= this many ATR
    "inside_mother_atr": 0.8,  # inside bar: the mother (previous) bar's range >= this many ATR
}

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
# EMA + candle patterns (informational)
# =====================================================================
def _atr_abs(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    prev = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    return pd.Series(tr).ewm(alpha=1.0 / max(1, period), adjust=False).mean().to_numpy()


def ema_series(close: np.ndarray, period: int) -> np.ndarray:
    """Standard EMA (alpha = 2/(period+1), seeded with the first close). The first `period - 1`
    values are NaN: the seed still dominates there, so they are not drawn."""
    out = pd.Series(close).ewm(span=period, adjust=False).mean().to_numpy().astype(float)
    out[:max(0, period - 1)] = np.nan
    return out


def ma_summary(close: np.ndarray, ts_s: np.ndarray, ema: Dict[int, np.ndarray]) -> Dict:
    """Where the last closed bar sits against EMA 50 / 200 and the latest 50/200 cross."""
    last = len(close) - 1
    out: Dict = {"periods": list(EMA_PERIODS)}
    for per in EMA_PERIODS:
        v = ema[per][last]
        out[f"ema{per}"] = None if not math.isfinite(v) else _px(v)
        out[f"price_vs_ema{per}_pct"] = None if not math.isfinite(v) else round(float((close[last] / v - 1.0) * 100.0), 2)
    e50, e200 = ema[50], ema[200]
    out["ema50_vs_ema200"] = None
    out["cross"] = None
    ok = np.isfinite(e50) & np.isfinite(e200)
    if ok[last]:
        out["ema50_vs_ema200"] = "above" if e50[last] > e200[last] else "below"
        diff = np.where(ok, e50 - e200, np.nan)
        sign = np.sign(diff)
        # last index where the sign differs from the current one = the bar before the latest cross
        idx = np.where(np.isfinite(sign) & (sign != sign[last]))[0]
        if len(idx):
            cross_i = int(idx[-1]) + 1
            out["cross"] = {
                "type": "golden" if sign[last] > 0 else "death",
                "time": int(ts_s[cross_i]),
                "bars_ago": int(last - cross_i),
            }
    return out


def detect_candle_patterns(df: pd.DataFrame, since: int = 1) -> List[Dict]:
    """Pin Bar, Engulfing and Inside Bar on CLOSED bars. Returns [{time, type, dir}], oldest first;
    dir is 'bull' | 'bear' | 'neutral'. `since` = first bar index that may carry a pattern.

    Rules (all geometric, thresholds in CANDLE):
      pin bar   - range >= min_range_atr x ATR; body <= 30% of the range; one wick >= 60% of the range
                  and the opposite wick <= 20%. Long LOWER wick = 'bull' pin, long UPPER wick = 'bear' pin.
      engulfing - opposite colours; the body of this bar covers the whole previous body (open and close
                  both at or beyond it, at least one strictly beyond); previous body >= 0.25 ATR and this
                  bar's range >= min_range_atr x ATR. Green over red = 'bull', red over green = 'bear'.
      inside    - high < previous high and low > previous low (strictly inside the previous bar's range),
                  previous bar's range >= 0.8 ATR so a quiet mother bar does not count. Always 'neutral'.
    A bar can carry both a pin bar and an engulfing (two entries) - never an inside bar and either of those."""
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    ts = (df["timestamp"].to_numpy(dtype=np.int64) // 1000)
    atr = _atr_abs(h, l, c)
    K = CANDLE
    out: List[Dict] = []
    for i in range(max(1, since), len(df)):
        rng = h[i] - l[i]
        if not (rng > 0 and atr[i] > 0):
            continue
        body = abs(c[i] - o[i])
        upper = h[i] - max(o[i], c[i])
        lower = min(o[i], c[i]) - l[i]
        t = int(ts[i])
        big = rng >= K["min_range_atr"] * atr[i]

        if big and body <= K["pin_body_max"] * rng:
            if lower >= K["pin_wick_min"] * rng and upper <= K["pin_nose_max"] * rng:
                out.append({"time": t, "type": "pin_bar", "dir": "bull"})
            elif upper >= K["pin_wick_min"] * rng and lower <= K["pin_nose_max"] * rng:
                out.append({"time": t, "type": "pin_bar", "dir": "bear"})

        pb_top, pb_bot = max(o[i - 1], c[i - 1]), min(o[i - 1], c[i - 1])
        prev_body = pb_top - pb_bot
        cur_top, cur_bot = max(o[i], c[i]), min(o[i], c[i])
        prev_red, prev_green = c[i - 1] < o[i - 1], c[i - 1] > o[i - 1]
        cur_red, cur_green = c[i] < o[i], c[i] > o[i]
        engulf = (big and prev_body >= K["eng_prev_body_min"] * atr[i - 1]
                  and cur_top >= pb_top and cur_bot <= pb_bot and (cur_top > pb_top or cur_bot < pb_bot))
        if engulf and prev_red and cur_green:
            out.append({"time": t, "type": "engulfing", "dir": "bull"})
        elif engulf and prev_green and cur_red:
            out.append({"time": t, "type": "engulfing", "dir": "bear"})
        elif (h[i] < h[i - 1] and l[i] > l[i - 1]
              and (h[i - 1] - l[i - 1]) >= K["inside_mother_atr"] * atr[i - 1]):
            out.append({"time": t, "type": "inside_bar", "dir": "neutral"})
    return out


def _nwe_side(close: np.ndarray, ts_s: np.ndarray, mid: np.ndarray, mae: np.ndarray, show_from: int,
              base: int = 0) -> Dict:
    """Serialise one NWE variant. `mid`/`mae` are aligned with close[base:]; points before `show_from`
    (absolute bar index) are dropped."""
    up, lo = mid + mae, mid - mae
    pts = {"mid": [], "upper": [], "lower": []}
    for k in range(len(mid)):
        i = base + k
        if i < show_from or not (math.isfinite(mid[k]) and math.isfinite(mae[k])):
            continue
        t = int(ts_s[i])
        pts["mid"].append({"time": t, "value": _px(mid[k])})
        pts["upper"].append({"time": t, "value": _px(up[k])})
        pts["lower"].append({"time": t, "value": _px(lo[k])})
    last = len(close) - 1
    kl = last - base
    summary = None
    if 0 <= kl < len(mid) and math.isfinite(mid[kl]) and math.isfinite(mae[kl]):
        c, u, l_ = float(close[last]), float(up[kl]), float(lo[kl])
        if c > u:
            pos, pct = "above", None
        elif c < l_:
            pos, pct = "below", None
        else:
            pos, pct = "inside", round((c - l_) / (u - l_), 3) if u > l_ else None
        summary = {
            "mid": _px(mid[kl]), "upper": _px(u), "lower": _px(l_), "half_width": _px(mae[kl]),
            "position": pos, "position_pct": pct,
            "dist_to_upper_pct": round((u / c - 1.0) * 100.0, 2),
            "dist_to_lower_pct": round((l_ / c - 1.0) * 100.0, 2),
        }
    return {**pts, "summary": summary}


def nw_envelope(close: np.ndarray, ts_s: np.ndarray, show_from: int, h: float = None, mult: float = None,
                window: int = None) -> Optional[Dict]:
    """Nadaraya-Watson Envelope on raw closes (as the TradingView indicator does): a Gaussian-kernel
    regression w(k) = exp(-k^2 / (2 h^2)) and bands = fit +/- mult x mean|close - fit|.

    'smooth'  (the indicator's "repainting smoothing" mode): every bar of the last `window` closed bars is
              fitted with a kernel that looks both ways inside that window, and the band half-width is one
              number, mult x MAE over the window. The fit of the newest bars therefore CHANGES when a new
              bar closes (that is what 'repainting' means) - the curve is smooth but not final.
    'causal'  (endpoint): each bar's fit uses only that bar and earlier ones, the half-width is a rolling
              mult x SMA(|close - fit|, 499). A drawn value never changes later, but it lags more and
              the newest bars are noisier.
    Returns None with fewer than `mae_min` closes."""
    h = float(NWE["h"] if h is None else h)
    mult = float(NWE["mult"] if mult is None else mult)
    window = int(NWE["window"] if window is None else window)
    n = len(close)
    if n < NWE["mae_min"] or not np.isfinite(close).all():
        return None
    y_all = close.astype(float)

    # smooth: both-sided kernel inside the last `window` closes
    w_n = min(window, n)
    base = n - w_n
    y = y_all[base:]
    idx = np.arange(w_n)
    d = (idx[:, None] - idx[None, :]).astype(float)
    wgt = np.exp(-(d * d) / (2.0 * h * h))
    mid_s = (wgt @ y) / wgt.sum(axis=1)
    mae_s = np.full(w_n, float(np.mean(np.abs(y - mid_s))) * mult)

    # causal: one-sided kernel (truncated at 6h, weight there < 1e-7) + rolling MAE
    kmax = int(math.ceil(6 * h))
    ker = np.exp(-(np.arange(kmax + 1, dtype=float) ** 2) / (2.0 * h * h))
    num = np.convolve(y_all, ker)[:n]
    den = np.convolve(np.ones(n), ker)[:n]
    mid_c = num / den
    mae_c = pd.Series(np.abs(y_all - mid_c)).rolling(int(NWE["mae_len"]), min_periods=int(NWE["mae_min"])).mean().to_numpy() * mult

    return {
        "params": {"h": h, "mult": mult, "window": w_n},
        "smooth": _nwe_side(y_all, ts_s, mid_s, mae_s, show_from, base=base),
        "causal": _nwe_side(y_all, ts_s, mid_c, mae_c, show_from, base=0),
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
    fetch_limit = max(int(params["lookback"]) + 120, candles_limit + EMA_WARMUP_BARS)
    bars = await safe_fetch_ohlcv(exchange, symbol, "1d", limit=fetch_limit)
    df = parse_ohlcv(bars)
    if not validate_dataframe(df, min_rows=MIN_BARS + 1):
        raise ValueError(f"Not enough daily data for {symbol}")

    closed = df.iloc[:-1].reset_index(drop=True)      # the last row is the still-forming daily bar
    ta = analyze_cached(symbol, closed, **params)

    sr = calc_support_resistance(
        closed, pivot_window=cfg.SR_PIVOT_WINDOW, max_levels=cfg.SR_MAX_LEVELS,
        ref_price=float(closed["close"].iloc[-1]),
    )
    # EMA + candle patterns on CLOSED bars only (the forming bar never moves them)
    c_close = closed["close"].to_numpy(dtype=float)
    c_ts = (closed["timestamp"].to_numpy(dtype=np.int64) // 1000)
    ema = {per: ema_series(c_close, per) for per in EMA_PERIODS}
    show_from = len(closed) - max(0, candles_limit - 1)       # closed bars shown = candles_limit - forming bar
    ema_json = {
        f"ema{per}": [{"time": int(c_ts[i]), "value": _px(ema[per][i])}
                      for i in range(max(0, show_from), len(closed)) if math.isfinite(ema[per][i])]
        for per in EMA_PERIODS
    }
    candle_patterns = detect_candle_patterns(closed, since=max(1, show_from))
    nwe = nw_envelope(c_close, c_ts, max(0, show_from))

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
        "ema": ema_json,
        "ma": ma_summary(c_close, c_ts, ema),
        "candle_patterns": candle_patterns,
        "nwe": nwe,
    }
