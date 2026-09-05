import numpy as np
import pandas as pd
from typing import Tuple

try:
    from numba import njit
    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False

from volume_indicators import calculate_relative_volume

# =====================================================================
# 📊  BASE INDICATORS
# =====================================================================

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder's smoothing)."""
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()

def _hurst_rs(returns: np.ndarray) -> float:
    """Rescaled-range (R/S) Hurst estimate for one window of log-returns.

    For several chunk sizes k, split the window into non-overlapping chunks
    of that size, compute each chunk's rescaled range (the range of its
    mean-adjusted cumulative sum, divided by its standard deviation), and
    average across chunks. The slope of log(R/S) vs log(k) is the Hurst
    exponent: ~0.5 = random walk, >0.5 = trending/persistent (a move tends
    to keep going), <0.5 = mean-reverting/anti-persistent (a move tends to
    reverse)."""
    n = len(returns)
    if n < 16:
        return 0.5  # not enough data — neutral, not "trending" or "reverting"
    std = returns.std()
    if std == 0 or np.isnan(std):
        return 0.5  # flat/no variance — neutral

    max_k = n // 2
    sizes = sorted(set(int(s) for s in np.geomspace(8, max_k, num=6) if s >= 8))
    if len(sizes) < 2:
        return 0.5

    rs_points = []
    for k in sizes:
        n_chunks = n // k
        if n_chunks < 1:
            continue
        rs_per_chunk = []
        for c in range(n_chunks):
            chunk = returns[c * k:(c + 1) * k]
            dev = np.cumsum(chunk - chunk.mean())
            r = dev.max() - dev.min()
            s = chunk.std()
            if s > 0:
                rs_per_chunk.append(r / s)
        if rs_per_chunk:
            rs_points.append((k, float(np.mean(rs_per_chunk))))

    if len(rs_points) < 2:
        return 0.5

    log_k = np.log([p[0] for p in rs_points])
    log_rs = np.log([p[1] for p in rs_points])
    slope, _ = np.polyfit(log_k, log_rs, 1)
    return float(np.clip(slope, 0.0, 1.0))


# ---------------------------------------------------------------------
# Numba-accelerated rolling Hurst.
#
# The pure-Python `_hurst_rs` above is correct but slow when driven through
# pandas' `rolling().apply()`: each window pays a Python function-call
# overhead plus small-array numpy overhead, ~30 nested-loop iterations per
# window. Benchmarked at ~2.2s for 3000 bars / window=100 on a single
# symbol/timeframe — a real cost when this runs on every scan tick for
# several symbols. The JIT version below computes the whole rolling series
# in one compiled pass (no per-window Python overhead) and produces
# bit-identical NaN placement and results to float64 precision (verified
# against `_hurst_rs` across multiple random series). Benchmarked at ~4ms
# warm for the same 3000 bars — roughly 500x faster.
#
# If numba isn't installed, calculate_hurst() transparently falls back to
# the pandas/`_hurst_rs` path so the bot still runs, just slower.
# =====================================================================
if _HAS_NUMBA:

    @njit(cache=True)
    def _hurst_window_numba(returns: np.ndarray) -> float:
        """Same R/S Hurst estimate as `_hurst_rs`, compiled for one window."""
        n = returns.shape[0]
        if n < 16:
            return 0.5

        mean = 0.0
        for i in range(n):
            mean += returns[i]
        mean /= n

        var = 0.0
        for i in range(n):
            d = returns[i] - mean
            var += d * d
        var /= n
        std = np.sqrt(var)
        if std == 0.0 or np.isnan(std):
            return 0.5

        max_k = n // 2
        if max_k < 8:
            return 0.5

        log_start = np.log(8.0)
        log_end = np.log(float(max_k))
        step = (log_end - log_start) / 5.0

        sizes = np.empty(6, dtype=np.int64)
        count = 0
        last = -1
        for i in range(6):
            # Exact endpoints avoid float round-trip precision loss through
            # log/exp (e.g. exp(log(8.0)) landing on 7.999999... -> int() -> 7),
            # matching np.geomspace(8, max_k, num=6) exactly at i=0 and i=5.
            if i == 0:
                s = 8
            elif i == 5:
                s = max_k
            else:
                s = int(np.exp(log_start + step * i))
            if s >= 8 and s != last:
                sizes[count] = s
                count += 1
                last = s
        if count < 2:
            return 0.5

        log_k = np.empty(count, dtype=np.float64)
        log_rs = np.empty(count, dtype=np.float64)
        valid_count = 0

        for idx in range(count):
            k = sizes[idx]
            n_chunks = n // k
            if n_chunks < 1:
                continue
            rs_sum = 0.0
            rs_cnt = 0
            for c in range(n_chunks):
                start = c * k
                cmean = 0.0
                for j in range(k):
                    cmean += returns[start + j]
                cmean /= k

                cumsum = 0.0
                cmax = -1e18
                cmin = 1e18
                cvar = 0.0
                for j in range(k):
                    d = returns[start + j] - cmean
                    cumsum += d
                    if cumsum > cmax:
                        cmax = cumsum
                    if cumsum < cmin:
                        cmin = cumsum
                    cvar += d * d
                cvar /= k
                cstd = np.sqrt(cvar)
                if cstd > 0.0:
                    r = cmax - cmin
                    rs_sum += r / cstd
                    rs_cnt += 1
            if rs_cnt > 0:
                rs_mean = rs_sum / rs_cnt
                log_k[valid_count] = np.log(float(k))
                log_rs[valid_count] = np.log(rs_mean)
                valid_count += 1

        if valid_count < 2:
            return 0.5

        sx = 0.0
        sy = 0.0
        for i in range(valid_count):
            sx += log_k[i]
            sy += log_rs[i]
        mx = sx / valid_count
        my = sy / valid_count

        num = 0.0
        den = 0.0
        for i in range(valid_count):
            dx = log_k[i] - mx
            dy = log_rs[i] - my
            num += dx * dy
            den += dx * dx
        if den == 0.0:
            return 0.5
        slope = num / den

        if slope < 0.0:
            slope = 0.0
        elif slope > 1.0:
            slope = 1.0
        return slope

    @njit(cache=True)
    def _hurst_rolling_numba(log_ret: np.ndarray, window: int) -> np.ndarray:
        """Rolling Hurst over the full series, matching pandas'
        `rolling(window).apply(..., raw=True)` semantics: a window is only
        evaluated once it has zero NaNs (default min_periods == window)."""
        n = log_ret.shape[0]
        out = np.full(n, np.nan)
        if n < window:
            return out
        for end in range(window - 1, n):
            start = end - window + 1
            has_nan = False
            for i in range(start, end + 1):
                if np.isnan(log_ret[i]):
                    has_nan = True
                    break
            if has_nan:
                continue
            out[end] = _hurst_window_numba(log_ret[start:end + 1])
        return out


def calculate_hurst(df: pd.DataFrame, window: int = 100) -> pd.Series:
    """Rolling Hurst exponent from log-returns over `window` bars.

    A statistically distinct "second opinion" on market regime, alongside
    CHOP: CHOP reads range vs. trend from price action directly (highs/lows
    vs a summed true range), Hurst reads persistence/randomness from the
    structure of returns themselves — a market can be choppy-looking by
    CHOP's measure while still statistically anti-persistent (mean-reverting)
    or persistent (trending) by Hurst's, and vice versa.

    >0.5 = trending/persistent, <0.5 = mean-reverting/anti-persistent,
    ~0.5 = closest to a random walk (hardest regime to trade profitably in
    either direction). NaN for the first `window` bars while warming up.

    Uses a numba-JIT rolling implementation when available (~500x faster
    than the pandas/apply path); falls back to pure Python otherwise."""
    log_ret = np.log(df["close"] / df["close"].shift(1))
    if _HAS_NUMBA:
        values = _hurst_rolling_numba(log_ret.to_numpy(dtype=np.float64), window)
        return pd.Series(values, index=df.index)
    return log_ret.rolling(window=window).apply(_hurst_rs, raw=True)


def calculate_chop(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Choppiness Index."""
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr_sum = tr.rolling(window=length).sum()
    hh = df["high"].rolling(window=length).max()
    ll = df["low"].rolling(window=length).min()
    # ✅ FIX BUG-LO004: division-by-zero protection (HH == LL) — epsilon only
    # in the denominator. As atr_sum → 0 (a flat market) CHOP → -∞, which
    # correctly means "not choppy" → 0. Adding an epsilon inside the log (as
    # it used to be) artificially inflated CHOP on a flat market.
    range_ = hh - ll
    with np.errstate(divide="ignore", invalid="ignore"):
        chop = 100 * np.log10(atr_sum / (range_ + 1e-12)) / np.log10(length)
    # When atr_sum == 0, log10(0) = -inf → replace with 0 (not choppy at all)
    chop = pd.Series(np.where(np.isfinite(chop), chop, 0.0), index=df.index)
    return chop.clip(0, 100)

def calculate_frama(df: pd.DataFrame, length: int = 22, mult: float = 2.1) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Fractal Adaptive Moving Average."""
    # 🆕 FIX (external review): FRAMA's fractal-dimension calc splits `length`
    # into two equal halves (n1, n2, each of width length//2) plus a full-width
    # reference window (n3, width length) — a construction that only makes
    # sense symmetrically. The web config's bound for this (5-100) never
    # enforced evenness, so e.g. length=15 gave n=7 (7+7=14, not 15) — a
    # silently mismatched window rather than a crash. Normalize here, at the
    # source, rather than only in the API validator, since Discord commands
    # and other callers read _cfg.FRAMA_LEN directly and would bypass a
    # UI-only check.
    if length % 2 != 0:
        length += 1
    n = int(length / 2)
    
    hh1 = df["high"].rolling(window=n).max()
    ll1 = df["low"].rolling(window=n).min()
    n1 = (hh1 - ll1) / n
    
    hh2 = df["high"].shift(n).rolling(window=n).max()
    ll2 = df["low"].shift(n).rolling(window=n).min()
    n2 = (hh2 - ll2) / n
    
    hh3 = df["high"].rolling(window=length).max()
    ll3 = df["low"].rolling(window=length).min()
    n3 = (hh3 - ll3) / length
    
    with np.errstate(divide="ignore", invalid="ignore"):
        valid_dimen = (n1 > 0) & (n2 > 0) & (n3 > 0)
        dimen = np.where(
            valid_dimen,
            (np.log(n1 + n2 + 1e-8) - np.log(n3 + 1e-8)) / np.log(2.0),
            0.0,
        )

    alpha = np.clip(np.exp(-4.6 * (dimen - 1.0)), 0.01, 1.0)

    close = df["close"].values
    frama_ma = np.zeros(len(df))
    frama_ma[0] = close[0]
    for i in range(1, len(df)):
        frama_ma[i] = alpha[i] * close[i] + (1.0 - alpha[i]) * frama_ma[i - 1]

    # 🆕 FIX (external review): before n1/n2/n3 are all valid (the rolling
    # windows haven't filled yet), `dimen` fell back to 0.0, which makes
    # `alpha = clip(exp(4.6), ...) = 1.0` — not a warm-up placeholder, a
    # perfectly valid-looking alpha that makes frama_ma[i] == close[i]
    # exactly. Downstream code (fu/fl/fdir, and the A/U-track FRAMA
    # direction filter) had no way to tell that from a genuine "price is
    # right at its own FRAMA" reading, and could pick up a fdir bias before
    # the indicator had enough history to mean anything. Masking the
    # RECURSIVE computation itself to NaN here would be wrong — frama_ma[i]
    # depends on frama_ma[i-1], so one NaN would poison every bar after it
    # forever. Instead, let the recursion bootstrap normally using the
    # alpha=1.0 fallback internally (needed for the running EMA-style
    # accumulator to work at all), then mask the OUTPUT for the genuinely
    # invalid bars only, after the fact.
    fs = pd.Series(frama_ma, index=df.index)
    fs[~valid_dimen] = np.nan

    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    # 🆕 FIX: the FRAMA channel width used to be computed via a simple SMA
    # (tr.rolling().mean()), rather than the Wilder RMA used in the original
    # Pine Script (ta.atr(frama_len)) and already implemented in this file's
    # calculate_atr(). An A/B test against live history (the Pine indicator)
    # showed Wilder RMA gives a noticeably higher win rate (A: +2.0 pp,
    # U: +4.9 pp), so the bot was switched to the same formula — this shifts
    # when frama_bullish/frama_bearish flips, i.e. it affects every signal's entry.
    fatr = tr.ewm(alpha=1.0 / length, adjust=False).mean()
    
    fu = fs + fatr * mult
    fl = fs - fatr * mult
    
    fdir = np.zeros(len(df))
    for i in range(1, len(df)):
        if close[i] > fu.iloc[i]:
            fdir[i] = 1
        elif close[i] < fl.iloc[i]:
            fdir[i] = -1
        else:
            fdir[i] = fdir[i - 1]
    
    return fs, fu, fl, pd.Series(fdir, index=df.index)

def calculate_mfi(df: pd.DataFrame, length: int = 8) -> pd.Series:
    """Money Flow Index."""
    hlc3 = (df["high"] + df["low"] + df["close"]) / 3.0
    mf = hlc3 * df["volume"]
    pos = np.where(hlc3 > hlc3.shift(1), mf, 0.0)
    neg = np.where(hlc3 < hlc3.shift(1), mf, 0.0)
    pos_s = pd.Series(pos, index=df.index).rolling(window=length).sum()
    neg_s = pd.Series(neg, index=df.index).rolling(window=length).sum()

    # 🆕 FIX (external review): the old `ratio = pos_s / (neg_s + 1e-8)` collapses
    # the genuinely neutral case (pos_s == 0 AND neg_s == 0 — no directional flow
    # at all, e.g. hlc3 dead flat for the whole window) into MFI == 0, i.e.
    # maximally oversold — the opposite of what "no flow either way" should mean.
    # It also nudges the "no negative flow at all" case to just under 100 rather
    # than exactly 100. Handle the three edge cases explicitly; only fall back
    # to the ratio formula when there's an actual mix of both.
    mfi = pd.Series(np.nan, index=df.index)
    both_zero = (pos_s == 0) & (neg_s == 0)
    only_pos = (neg_s == 0) & (pos_s > 0)
    only_neg = (pos_s == 0) & (neg_s > 0)
    mixed = ~(both_zero | only_pos | only_neg)

    mfi[both_zero] = 50.0
    mfi[only_pos] = 100.0
    mfi[only_neg] = 0.0
    ratio = pos_s / neg_s.replace(0, np.nan)
    mfi[mixed] = 100.0 - (100.0 / (1.0 + ratio[mixed]))
    return mfi

def calculate_andean(df: pd.DataFrame, length: int = 23, sig_len: int = 6) -> Tuple[pd.Series, pd.Series]:
    """Andean Oscillator."""
    alpha = 2.0 / (length + 1)
    c = df["close"].values
    o = df["open"].values
    
    u1 = np.zeros(len(df))
    u2 = np.zeros(len(df))
    l1 = np.zeros(len(df))
    l2 = np.zeros(len(df))
    
    u1[0] = c[0]
    u2[0] = c[0]**2
    l1[0] = c[0]
    l2[0] = c[0]**2
    
    for i in range(1, len(df)):
        u1[i] = max(c[i], o[i], u1[i-1] - (u1[i-1] - c[i]) * alpha)
        u2[i] = max(c[i]**2, o[i]**2, u2[i-1] - (u2[i-1] - c[i]**2) * alpha)
        l1[i] = min(c[i], o[i], l1[i-1] + (c[i] - l1[i-1]) * alpha)
        l2[i] = min(c[i]**2, o[i]**2, l2[i-1] + (c[i]**2 - l2[i-1]) * alpha)
    
    osc = pd.Series(
        np.sqrt(np.maximum(0, l2 - l1**2)) - np.sqrt(np.maximum(0, u2 - u1**2)),
        index=df.index,
    )
    return osc, osc.ewm(span=sig_len, adjust=False).mean()

# =====================================================================
# 🕯️  HEIKIN ASHI
# =====================================================================

def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Converts regular candles into Heikin-Ashi."""
    ha = df.copy()
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open = pd.Series(index=df.index, dtype=float)
    ha_open.iloc[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2.0
    ha["open"] = ha_open
    ha["high"] = pd.concat([df["high"], ha_open, ha_close], axis=1).max(axis=1)
    ha["low"] = pd.concat([df["low"], ha_open, ha_close], axis=1).min(axis=1)
    ha["close"] = ha_close
    return ha

# =====================================================================
# 🤖  UT BOT (FIXED)
# =====================================================================

def calculate_ut_bot(
    df: pd.DataFrame,
    sensitivity: float = 1.0,
    period: int = 10,
    use_ha: bool = False
) -> Tuple[pd.Series, pd.Series]:
    """
    UT Bot by Kivanc Ozbilgic.
    
    IMPORTANT: ATR is always calculated from regular candles (real
    volatility), even when Heikin-Ashi mode is enabled for signals.
    """
    # ✅ ATR always from regular candles
    atr_normal = calculate_atr(df, period)
    n_loss = (sensitivity * atr_normal).values
    
    # Use HA or regular candles for signals
    if use_ha:
        df_ut = heikin_ashi(df)
        src = df_ut["close"].values
    else:
        df_ut = df.copy()
        src = df_ut["close"].values
    
    # Trailing stop calculation
    ts = np.zeros(len(df_ut))
    ts[0] = src[0] - n_loss[0]
    
    for i in range(1, len(df_ut)):
        prev = ts[i-1]
        if src[i] > prev and src[i-1] > prev:
            ts[i] = max(prev, src[i] - n_loss[i])
        elif src[i] < prev and src[i-1] < prev:
            ts[i] = min(prev, src[i] + n_loss[i])
        else:
            ts[i] = src[i] - n_loss[i] if src[i] > prev else src[i] + n_loss[i]
    
    ts_s = pd.Series(ts, index=df.index)
    src_s = df_ut["close"]
    
    ut_buy = (src_s > ts_s) & (src_s.shift(1) <= ts_s.shift(1))
    ut_sell = (src_s < ts_s) & (src_s.shift(1) >= ts_s.shift(1))
    
    return ut_buy, ut_sell

# =====================================================================
# 🔢  K-MEANS MFI
# =====================================================================

def run_kmeans_mfi(mfi: pd.Series, training_size: int = 800) -> Tuple[float, float]:
    """Trains K-Means for the MFI oversold/overbought levels."""
    vals = mfi.dropna().tail(training_size).values
    
    # NaN protection
    if len(vals) < 2 or np.isnan(vals).any():
        return 20.0, 80.0
    
    c1, c2 = float(vals.min()), float(vals.max())
    
    for _ in range(10):
        d1 = np.abs(vals - c1)
        d2 = np.abs(vals - c2)
        
        # Empty-cluster protection
        cl1 = vals[d1 <= d2]
        cl2 = vals[d2 < d1]
        
        if len(cl1) == 0 and len(cl2) == 0:
            break
        
        if len(cl1) == 0:
            c1 = float(np.nanmean(vals))
        else:
            c1 = float(np.nanmean(cl1))
        
        if len(cl2) == 0:
            c2 = float(np.nanmean(vals))
        else:
            c2 = float(np.nanmean(cl2))
    
    return min(c1, c2), max(c1, c2)


# =====================================================================
# 🚀  BREAKOUT SIGNAL (track "b") — squeeze + volume/range expansion
# =====================================================================
def calculate_breakout_signal(
    df: pd.DataFrame,
    atr14: pd.Series,
    atr_pct: pd.Series,
    lookback: int = 20,
    squeeze_window: int = 60,
    squeeze_percentile: float = 0.25,
    vol_spike_mult: float = 2.5,
    range_atr_mult: float = 1.5,
    close_loc_min: float = 0.70,
) -> Tuple[pd.Series, pd.Series]:
    """Volume/range breakout-out-of-squeeze detector. See config.py's
    BREAKOUT section for the methodology this draws on (Wyckoff SOS, VSA,
    NR7/squeeze, Darvas box) and why this exists as a separate track rather
    than a filter on A/U.

    Four conditions, ALL required, evaluated per-bar at index i (mirrors
    the rest of this file's style: full-series vectorized, indexed by the
    caller — not a rolling().apply(), since every piece here is either
    already a Series the caller has (atr14/atr_pct) or a cheap rolling op):

      1. Squeeze precondition — atr_pct the bar BEFORE i (not at i — i is
         the expansion bar itself) sits in the bottom `squeeze_percentile`
         of its own trailing `squeeze_window`. This is what makes it a
         breakout-from-compression rather than just any big candle in the
         middle of an already-volatile stretch.
      2. Volume AND range expand together at i — relative volume (vs its
         own 20-bar SMA) past `vol_spike_mult`, AND bar range past
         `range_atr_mult` x ATR14. VSA's point: high volume alone can mean
         absorption (someone big quietly filling into the move, capping
         it), not confirmation — the bar's range has to expand too, in the
         same conviction the volume implies.
      3. Close near the bar's own extreme — top/bottom `close_loc_min`
         share of its (high, low) range. A wide, loud bar that closes back
         near the open/opposite extreme is a rejection wick, not strength.
      4. Genuine breakout — close at i clears the actual high/low of the
         preceding `lookback` bars (the pre-squeeze range), not just "big
         relative to ATR". Without this, a big-range bar in the middle of
         a range would qualify; this is what makes it a breakout of
         something specific.

    Returns (breakout_long, breakout_short) — boolean Series, same index as
    df. NaN-safe: any bar where an input is still NaN (warm-up) reads False,
    never raises.
    """
    n = len(df)
    high = df["high"]
    low = df["low"]
    close = df["close"]
    volume = df["volume"]

    rel_vol = calculate_relative_volume(df, period=20)

    bar_range = high - low
    range_ok = bar_range > (range_atr_mult * atr14)
    vol_ok = rel_vol > vol_spike_mult

    close_loc = np.where(bar_range > 1e-12, (close - low) / bar_range.replace(0, np.nan), 0.5)
    close_loc = pd.Series(close_loc, index=df.index).fillna(0.5)
    close_loc_long_ok = close_loc >= close_loc_min
    close_loc_short_ok = close_loc <= (1.0 - close_loc_min)

    # Squeeze: atr_pct as of the PRIOR bar vs. its own trailing history —
    # shift(1) so bar i's own (already-expanded) atr_pct doesn't leak into
    # its own squeeze check.
    #
    # 🆕 FIX (external review): the threshold used to be computed as
    # `atr_pct_prev.rolling(window=squeeze_window).quantile(...)` — but that
    # rolling window, by construction, includes atr_pct_prev's OWN value at
    # row i (bar i-1's ATR%) as part of the very distribution it's being
    # measured against. A second shift(1) here excludes the point being
    # tested from its own comparison baseline: bar i's squeeze check now
    # reads "is ATR%(i-1) below the Nth percentile of ATR%(i-2 ... i-1-window),
    # a window that does NOT include ATR%(i-1) itself" — a cleaner,
    # self-consistent statistical comparison.
    #
    # 🆕 FIX (external review): min_periods was squeeze_window // 2 (and
    # lookback // 2 below) — meaning a "genuine squeeze vs. its own recent
    # history" claim could fire on half a window's worth of actual history.
    # README/docstring both describe this as "bottom quartile of its own
    # RECENT HISTORY" — a half-populated window undersells that claim, so
    # this now requires the FULL window before it's willing to call
    # anything a squeeze. b_warmed_up/b_warmed_up_bt in signals.py already
    # gate on the full squeeze_window + lookback sum, so this doesn't
    # introduce any additional warm-up delay beyond what was already there.
    atr_pct_prev = atr_pct.shift(1)
    squeeze_threshold = atr_pct_prev.shift(1).rolling(window=squeeze_window, min_periods=squeeze_window).quantile(squeeze_percentile)
    squeeze_ok = atr_pct_prev < squeeze_threshold

    # Darvas-box breakout level: the high/low of the `lookback` bars BEFORE
    # i (shift(1) so the window is [i-lookback, i-1], excluding i itself).
    box_high = high.shift(1).rolling(window=lookback, min_periods=lookback).max()
    box_low = low.shift(1).rolling(window=lookback, min_periods=lookback).min()
    breakout_level_long_ok = close > box_high
    breakout_level_short_ok = close < box_low

    breakout_long = (
        squeeze_ok
        & vol_ok
        & range_ok
        & close_loc_long_ok
        & breakout_level_long_ok
    ).fillna(False)

    breakout_short = (
        squeeze_ok
        & vol_ok
        & range_ok
        & close_loc_short_ok
        & breakout_level_short_ok
    ).fillna(False)

    return breakout_long, breakout_short
