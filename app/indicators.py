import logging
import math
import os
from functools import lru_cache
import numpy as np
import pandas as pd
from typing import Tuple, Optional

try:
    from numba import njit
    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False

logger = logging.getLogger(__name__)

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

# =====================================================================
# 📈  HURST EXPONENT — rescaled-range (R/S), Anis–Lloyd bias-corrected
# =====================================================================
# 🆕 FIX (2026-10 audit, small-sample bias): the raw R/S slope is NOT centered
# on 0.5 for a random walk on short windows. For i.i.d. returns the expected
# R/S grows faster than k**0.5 at small k (Anis & Lloyd, 1976), so the raw
# estimate came out around 0.58-0.61 on pure noise — i.e. "H - 0.5 >=
# HURST_MIN_DEVIATION" was already ~0.1 of the way to passing before the
# market said anything, and the bias differs by window length, so one
# threshold meant different things on different timeframes/windows.
# The correction subtracts the slope that i.i.d. noise would have produced
# over the SAME chunk sizes (E[R/S] from the Anis-Lloyd formula) and
# re-centers on 0.5: H_AL = 0.5 + slope(log R/S) - slope(log E[R/S]).
# It removes the bias, NOT the variance: a short window is still a noisy
# estimate (sd of H is ~0.35 at window 21-23, ~0.14 at 50, ~0.09 at 100),
# so prefer windows >= 100 where the threshold can actually separate regimes.
#
# Web Settings -> Hurst Filter -> "Anis-Lloyd bias correction" toggle (or env
# HURST_BIAS_CORRECTION=0 as the first-boot default) gives the legacy
# (uncorrected) estimate back without a code change — the per-timeframe
# windows and HURST_MIN_DEVIATION already tuned live were calibrated on that
# estimate. The flag lives in config.HURST_BIAS_CORRECTION (persisted, live
# switchable) and is read at call time, NOT imported by value here.
def _bias_correction_enabled() -> bool:
    import config as _cfg
    return bool(_cfg.HURST_BIAS_CORRECTION)

_HURST_MIN_CHUNK = 8


def _hurst_sizes(n: int) -> np.ndarray:
    """Chunk sizes for a window of n returns: 6 log-spaced values from 8 to
    n // 2, de-duplicated. Shared by the numba and the pandas path — they used
    to pick sizes through two slightly different float routes (np.geomspace vs.
    a manual exp/log loop) and disagreed on a chunk size by +-1 at a few
    window lengths; one function means identical sizes everywhere. Fewer than
    two distinct sizes (windows < 18) means no slope can be fitted."""
    max_k = n // 2
    if max_k < _HURST_MIN_CHUNK:
        return np.empty(0, dtype=np.int64)
    log_start = math.log(_HURST_MIN_CHUNK)
    step = (math.log(max_k) - log_start) / 5.0
    out = []
    last = -1
    for i in range(6):
        if i == 0:
            s = _HURST_MIN_CHUNK  # exact endpoints: no exp(log(8)) = 7.999.. -> 7
        elif i == 5:
            s = max_k
        else:
            s = int(math.exp(log_start + step * i))
        if s >= _HURST_MIN_CHUNK and s != last:
            out.append(s)
            last = s
    return np.asarray(out, dtype=np.int64)


@lru_cache(maxsize=None)
def _anis_lloyd_expected_rs(k: int) -> float:
    """E[R/S] of k i.i.d. observations — the original Anis & Lloyd (1976)
    expression, for R/S built with the POPULATION std (ddof=0), which is what
    this file uses everywhere. Do NOT multiply by Peters' (k - 0.5) / k: that
    factor is for the ddof=1 std and would under-state E[R/S] here by ~6% at
    k=8 (checked against a Monte Carlo of this exact R/S definition)."""
    s = 0.0
    for i in range(1, k):
        s += math.sqrt((k - i) / i)
    if k <= 340:
        return math.exp(math.lgamma((k - 1) / 2.0) - math.lgamma(k / 2.0)) / math.sqrt(math.pi) * s
    return s / math.sqrt(k * math.pi / 2.0)  # large-k form of the same expression


def _expected_log_rs(sizes: np.ndarray) -> np.ndarray:
    return np.array([math.log(_anis_lloyd_expected_rs(int(k))) for k in sizes], dtype=np.float64)


def _hurst_rs(returns: np.ndarray, bias_correct: Optional[bool] = None) -> float:
    """Rescaled-range (R/S) Hurst estimate for one window of log-returns.

    For several chunk sizes k, split the window into non-overlapping chunks
    of that size, compute each chunk's rescaled range (the range of its
    mean-adjusted cumulative sum, divided by its standard deviation), and
    average across chunks. The slope of log(R/S) vs log(k) is the Hurst
    exponent: ~0.5 = random walk, >0.5 = trending/persistent (a move tends
    to keep going), <0.5 = mean-reverting/anti-persistent (a move tends to
    reverse).

    bias_correct (default: config.HURST_BIAS_CORRECTION) — apply the Anis–Lloyd
    correction described above, so pure noise reads ~0.5 at any window."""
    if bias_correct is None:
        bias_correct = _bias_correction_enabled()
    n = len(returns)
    if n < 16:
        return 0.5  # not enough data — neutral, not "trending" or "reverting"
    std = returns.std()
    if std == 0 or np.isnan(std):
        return 0.5  # flat/no variance — neutral

    sizes = _hurst_sizes(n)
    if len(sizes) < 2:
        return 0.5
    log_exp = _expected_log_rs(sizes) if bias_correct else None

    log_k, log_rs, log_e = [], [], []
    for idx in range(len(sizes)):
        k = int(sizes[idx])
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
            log_k.append(math.log(k))
            log_rs.append(math.log(float(np.mean(rs_per_chunk))))
            if bias_correct:
                log_e.append(float(log_exp[idx]))

    if len(log_k) < 2:
        return 0.5

    slope = float(np.polyfit(log_k, log_rs, 1)[0])
    if bias_correct:
        slope = slope - float(np.polyfit(log_k, log_e, 1)[0]) + 0.5
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
# in one compiled pass (no per-window Python overhead), ~500x faster.
# Chunk sizes and the Anis–Lloyd expected values are computed once in Python
# (they depend only on the window length, not on the data) and passed in, so
# both paths use exactly the same sizes; the two then differ only by
# floating-point summation order.
#
# If numba isn't installed, calculate_hurst() transparently falls back to
# the pandas/`_hurst_rs` path so the bot still runs, just slower.
# =====================================================================
if _HAS_NUMBA:

    @njit(cache=True)
    def _ols_slope_numba(x: np.ndarray, y: np.ndarray, m: int) -> float:
        sx = 0.0
        sy = 0.0
        for i in range(m):
            sx += x[i]
            sy += y[i]
        mx = sx / m
        my = sy / m
        num = 0.0
        den = 0.0
        for i in range(m):
            dx = x[i] - mx
            num += dx * (y[i] - my)
            den += dx * dx
        if den == 0.0:
            return np.nan
        return num / den

    @njit(cache=True)
    def _hurst_window_numba(returns: np.ndarray, sizes: np.ndarray, log_exp: np.ndarray, correct: bool) -> float:
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

        count = sizes.shape[0]
        if count < 2:
            return 0.5

        log_k = np.empty(count, dtype=np.float64)
        log_rs = np.empty(count, dtype=np.float64)
        log_e = np.empty(count, dtype=np.float64)
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
                log_e[valid_count] = log_exp[idx]
                valid_count += 1

        if valid_count < 2:
            return 0.5

        slope = _ols_slope_numba(log_k, log_rs, valid_count)
        if np.isnan(slope):
            return 0.5
        if correct:
            slope_e = _ols_slope_numba(log_k, log_e, valid_count)
            if np.isnan(slope_e):
                return 0.5
            slope = slope - slope_e + 0.5

        if slope < 0.0:
            slope = 0.0
        elif slope > 1.0:
            slope = 1.0
        return slope

    @njit(cache=True)
    def _hurst_rolling_numba(log_ret: np.ndarray, window: int, sizes: np.ndarray, log_exp: np.ndarray, correct: bool) -> np.ndarray:
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
            out[end] = _hurst_window_numba(log_ret[start:end + 1], sizes, log_exp, correct)
        return out


def calculate_hurst(df: pd.DataFrame, window: int = 100, bias_correct: Optional[bool] = None) -> pd.Series:
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

    bias_correct — Anis–Lloyd small-sample correction (see the section header
    above); None uses config.HURST_BIAS_CORRECTION (default on). Pass False for the
    legacy estimate.

    Uses a numba-JIT rolling implementation when available (~500x faster
    than the pandas/apply path); falls back to pure Python otherwise."""
    if bias_correct is None:
        bias_correct = _bias_correction_enabled()
    log_ret = np.log(df["close"] / df["close"].shift(1))
    if _HAS_NUMBA:
        sizes = _hurst_sizes(window)
        values = _hurst_rolling_numba(
            log_ret.to_numpy(dtype=np.float64), window, sizes, _expected_log_rs(sizes), bool(bias_correct)
        )
        return pd.Series(values, index=df.index)
    return log_ret.rolling(window=window).apply(lambda a: _hurst_rs(a, bias_correct), raw=True)


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
    # 🆕 (external review, decision kept as-is — see chat): flat→0 reads a
    # dead/no-movement market as "maximally trending", which is only safe
    # because the separate ATR_MIN filter almost always catches genuinely
    # dead markets independently (verified: atr_sum→0 here implies
    # calculate_atr's own ATR%→0 too, which fails ATR_MIN and blocks the
    # trade regardless of what CHOP says — both filters are AND'ed
    # together). Logged here (not acted on) so if this combination is ever
    # seen without ATR also catching it — e.g. ATR_MIN gets lowered or
    # disabled later — there's a trace instead of a silent pass-through.
    flat_bars = int((~np.isfinite(pd.Series(np.where(atr_sum > 0, 100 * np.log10(atr_sum / (range_ + 1e-12)) / np.log10(length), np.inf), index=df.index)))
                     .tail(1).sum()) if len(df) else 0
    if flat_bars and atr_sum.iloc[-1] == 0:
        logger.debug(f"[CHOP] Flat/no-movement bar detected (atr_sum=0) — CHOP defaulted to 0 (trending); relying on ATR_MIN to gate this if it's a genuinely dead market.")
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

    # 🆕 FIX (external review): a near-constant MFI window (e.g. a long dead
    # stretch sitting at ~50) makes min≈max, so both K-Means centers collapse
    # to the same value — degenerate clustering, not neutral-but-valid
    # levels. A signal system then reads that collapsed value as BOTH the
    # oversold AND overbought threshold, meaning any subsequent tiny MFI
    # wiggle registers as a crossover in either direction — a genuinely flat
    # market turning into a spurious signal generator. Fall back to the same
    # generic 20/80 used for the NaN case above; there isn't a real
    # oversold/overbought distribution to learn from a flat window either.
    if (c2 - c1) < 1e-6:
        return 20.0, 80.0
    
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

    # 🆕 FIX: the pre-loop guard above only catches a collapsed INPUT
    # (min≈max before any iteration). The Lloyd's-algorithm loop itself can
    # also converge the two centers onto the same value — e.g. if every
    # point keeps landing in one cluster on `d1 <= d2` ties — leaving the
    # same degenerate "one threshold pretending to be both OS and OB"
    # result this function exists to prevent, just reached after the loop
    # instead of before it. Apply the identical fallback here.
    if abs(c2 - c1) < 1e-6:
        return 20.0, 80.0

    return min(c1, c2), max(c1, c2)


def run_kmeans_mfi_rolling(mfi: pd.Series, training_size: int = 800, start_idx: int = 0, end_idx: Optional[int] = None) -> Tuple[pd.Series, pd.Series]:
    """Point-in-time version of run_kmeans_mfi: OS/OB levels at each index i
    are trained only on mfi.iloc[max(0, i-training_size+1) : i+1] — data
    available up to and including bar i — not the whole series.

    🆕 FIX (external review, P0): backtest_history() used to call
    run_kmeans_mfi(mfi, training_size) ONCE on the full historical series
    before the simulation loop. `.dropna().tail(training_size)` took the
    LAST training_size values of the WHOLE series — near the end of the
    backtest — and applied those same OS/OB thresholds uniformly to every
    bar, including ones from far earlier. A signal at bar 500 was being
    judged against a distribution of MFI values that, chronologically,
    wouldn't exist until bar ~2200+ — classic look-ahead bias. Live doesn't
    have this problem: a fresh live fetch's own .tail() is genuinely "the
    most recent training_size values as of right now" already.

    start_idx/end_idx let the caller skip computing this for bars outside
    the range it actually simulates (backtest_history never evaluates
    signals before its own start_idx) — benchmarked at well under 1ms/bar,
    so even the full range costs about a second for a 3000-bar backtest, a
    one-off admin-triggered cost, not a per-scan-tick one."""
    n = len(mfi)
    end_idx = n if end_idx is None else min(end_idx, n)
    os_arr = np.full(n, 20.0)
    ob_arr = np.full(n, 80.0)
    for i in range(start_idx, end_idx):
        window = mfi.iloc[max(0, i - training_size + 1): i + 1]
        os_arr[i], ob_arr[i] = run_kmeans_mfi(window, training_size)
    return pd.Series(os_arr, index=mfi.index), pd.Series(ob_arr, index=mfi.index)
