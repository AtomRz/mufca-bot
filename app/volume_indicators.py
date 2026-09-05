import pandas as pd
import numpy as np
from typing import Tuple, Dict
from config import MAX_ALLOWED_LEV

# =====================================================================
# 📊  VOLUME FLOW INDICATORS v3 — Score-Based (Confidence & Leverage)
# =====================================================================

def calculate_obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    price_change = df["close"].diff()
    sign = np.sign(price_change)
    signed_volume = sign * df["volume"]
    return signed_volume.fillna(0).cumsum()


def calculate_obv_ema(obv: pd.Series, period: int = 20) -> pd.Series:
    return obv.ewm(span=period, adjust=False).mean()


def calculate_relative_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Relative Volume = current / SMA(volume, period) of the PRIOR `period` bars.

    🆕 FIX (external review): `rolling(window=period).mean()` on the same
    (unshifted) series includes the current bar's own volume as the last
    element of its own baseline — a genuine spike bar dilutes the very
    average it's being compared against, systematically understating how
    unusual it actually is (e.g. 20x normal volume on top of 19 normal bars
    reads as ~4.17x instead of the true ~5x vs. the *prior* baseline).
    Shifted by 1 so "how much bigger is this bar than the recent normal
    volume" doesn't have the bar answering that question baked into the
    question itself. Matters most for the B-track breakout detector, which
    thresholds directly on this value (BREAKOUT_VOL_SPIKE_MULT) — the old
    self-inclusive version made genuine spikes read as systematically
    smaller than they were, requiring an even bigger real spike to clear
    the same nominal threshold."""
    vol_sma = df["volume"].shift(1).rolling(window=period).mean()
    return df["volume"] / (vol_sma + 1e-12)


def calculate_volume_pressure(df: pd.DataFrame) -> pd.Series:
    """
    Volume pressure — an ESTIMATED buying/selling pressure proxy via close
    location in the bar (close near high = +pressure, close near low =
    -pressure), multiplied by volume.

    🆕 RENAMED (external review) from calculate_volume_delta: this is not
    real Volume Delta / CVD (buy-volume minus sell-volume from trade-level
    tape data) — Gate.io's OHLCV doesn't carry that, so this proxies it from
    candle shape instead. The old name invited exactly the confusion this
    docstring already warned about — a future reader skimming just the
    function name could easily assume it's the real thing and, if Gate.io
    trade-level data is ever wired in, silently keep using this proxy
    alongside/instead of it. Renamed rather than reimplemented — same
    formula, clearer name for what it actually measures.

    BUGFIX BUG-ME004: at high == low (a doji candle) range_ = 0, close_loc ≈ 0,
    pressure = -1.0 — a doji was being interpreted as maximum sell pressure.
    Now a doji gives close_loc = 0.5 (neutral).
    """
    range_ = df["high"] - df["low"]
    # 🆕 FIX: doji protection — at zero or near-zero range, use a neutral value
    close_loc = np.where(
        range_ < 1e-12,
        0.5,  # Doji = neutral
        (df["close"] - df["low"]) / (range_ + 1e-12)
    )
    pressure = (close_loc - 0.5) * 2.0
    return pressure * df["volume"]


def volume_flow_signal_v3(df: pd.DataFrame, obv_period: int = 20) -> Dict:
    """
    Comprehensive volume analysis v3.

    Returns dict:
        - flow: "inflow" | "outflow" | "neutral"
        - score: -1.0 to +1.0 (composite volume score)
        - rel_vol: relative volume
        - obv_mom: OBV momentum % (5 bars)
        - delta_trend: volume delta trend
        - strength: 0.0-1.0
    """
    if len(df) < obv_period + 10:
        return {
            "flow": "neutral",
            "score": 0.0,
            "rel_vol": 1.0,
            "obv_mom": 0.0,
            "delta_trend": "neutral",
            "strength": 0.0,
            "reason": "insufficient data"
        }

    # 1. OBV Flow
    obv = calculate_obv(df)
    obv_ema = calculate_obv_ema(obv, obv_period)
    current_obv = obv.iloc[-1]
    current_ema = obv_ema.iloc[-1]

    obv_score = 0.0
    if not (pd.isna(current_obv) or pd.isna(current_ema)):
        # 🆕 FIX (external review): only guarded against current_ema == 0
        # exactly — but OBV crossing (or nearly crossing) zero can leave
        # current_ema merely TINY rather than exactly zero, which still
        # blows the ratio up to an extreme value that then maxes out
        # obv_score at +/-1.0 for what's often a practically insignificant
        # OBV move. Floor the "is there a meaningful EMA level to compare
        # against" check using a small fraction of recent total traded
        # volume — a scale reference that doesn't itself depend on OBV's
        # own (potentially degenerate) value. Doesn't change anything for
        # the ordinary case (current_ema is virtually always far larger
        # than 1% of the period's total volume once trading has been
        # ongoing) — only prevents the near-zero-crossing blowup.
        obv_scale_floor = df["volume"].iloc[-obv_period:].sum() * 0.01
        if abs(current_ema) >= obv_scale_floor:
            obv_ratio = current_obv / current_ema
            if obv_ratio > 1.02:
                obv_score = min(1.0, (obv_ratio - 1.02) / 0.08)
            elif obv_ratio < 0.98:
                obv_score = max(-1.0, (obv_ratio - 0.98) / 0.08)

    # 2. OBV Momentum (5 bars)
    # 🆕 FIX (external review): same near-zero-crossing issue as obv_ratio
    # above — abs(obv.iloc[-6]) can be tiny even when not exactly zero,
    # making obv_mom spike to an enormous (if later clipped) percentage for
    # a practically insignificant absolute OBV change. Floor the
    # denominator's magnitude the same way, using recent total volume as a
    # scale reference — unchanged for the ordinary case, only kicks in when
    # obv.iloc[-6] happens to sit near a zero-crossing.
    mom_scale_floor = df["volume"].iloc[-20:].sum() * 0.01
    obv_mom_denom = max(abs(obv.iloc[-6]), mom_scale_floor)
    obv_mom = (obv.iloc[-1] - obv.iloc[-6]) / obv_mom_denom * 100
    mom_score = np.clip(obv_mom / 10, -0.5, 0.5)

    # 3. Relative Volume
    rel_vol = float(calculate_relative_volume(df, obv_period).iloc[-1])

    # 4. Volume Delta Trend
    delta = calculate_volume_pressure(df)
    delta_ema = delta.ewm(span=obv_period, adjust=False).mean()
    current_delta = delta.iloc[-1]
    current_delta_ema = delta_ema.iloc[-1]

    delta_score = 0.0
    if not (pd.isna(current_delta) or pd.isna(current_delta_ema)):
        if current_delta > current_delta_ema * 1.05:
            delta_score = 0.3
        elif current_delta < current_delta_ema * 0.95:
            delta_score = -0.3

    # Combined score: OBV 40%, momentum 30%, delta 30%
    score = 0.4 * obv_score + 0.3 * mom_score + 0.3 * delta_score

    # Rel_vol multiplier: high volume amplifies signal, low volume dampens
    if rel_vol >= 1.5:
        vol_mult = 1.2
    elif rel_vol >= 1.0:
        vol_mult = 1.0
    elif rel_vol >= 0.7:
        vol_mult = 0.8
    else:
        vol_mult = 0.5  # Low volume = weak signal

    score *= vol_mult
    score = float(np.clip(score, -1.0, 1.0))

    if score > 0.2:
        flow = "inflow"
    elif score < -0.2:
        flow = "outflow"
    else:
        flow = "neutral"

    return {
        "flow": flow,
        "score": round(score, 3),
        "rel_vol": round(rel_vol, 2),
        "obv_mom": round(float(obv_mom), 2),
        "delta_trend": "inflow" if delta_score > 0 else "outflow" if delta_score < 0 else "neutral",
        "strength": round(abs(score), 2),
        "reason": f"score={score:.2f}|RV={rel_vol:.1f}|OBV={obv_score:.2f}|mom={mom_score:.2f}|delta={delta_score:.2f}"
    }


def volume_score_for_side(info: Dict, side: str) -> float:
    """
    Converts the volume score into a directional score for the signal's side.

    LONG:  a positive score = good (buyers)
    SHORT: a negative score = good (sellers)

    Returns: -1.0 to +1.0 where +1 = perfect alignment
    """
    score = info["score"]
    if side == "long":
        return score  # Positive = inflow = good for long
    else:
        return -score  # Negative = outflow = good for short


def volume_confidence_adjustment(info: Dict, side: str) -> int:
    """
    Adjusts confidence based on volume.

    Returns: delta to add to confidence score (-15 to +15)
    """
    directional = volume_score_for_side(info, side)

    # Map -1..+1 to -15..+15 confidence points
    return int(round(directional * 15))


def volume_leverage_adjustment_v3(info: Dict, side: str, base_lev: int) -> Tuple[int, str]:
    """
    Adjusts leverage based on volume.

    Returns: (adjusted_lev, reason)
    """
    directional = volume_score_for_side(info, side)
    rel_vol = info["rel_vol"]

    # Critical low volume — always reduce significantly
    if rel_vol < 0.5:
        new_lev = max(1, int(base_lev * 0.5))
        return new_lev, f"LOW_VOL(RV={rel_vol:.1f}): lev {base_lev}->{new_lev}x"

    # Volume score adjustment
    if directional > 0.5 and rel_vol > 1.2:
        delta = +2
    elif directional > 0.2 and rel_vol > 0.8:
        delta = +1
    elif directional < -0.5:
        delta = -2
    elif directional < -0.2:
        delta = -1
    else:
        delta = 0

    new_lev = max(1, min(MAX_ALLOWED_LEV, base_lev + delta))

    if delta > 0:
        return new_lev, f"VOL_CONFIRM({directional:+.2f},RV={rel_vol:.1f}): lev +{delta}->{new_lev}x"
    elif delta < 0:
        return new_lev, f"VOL_WARN({directional:+.2f},RV={rel_vol:.1f}): lev {delta}->{new_lev}x"
    else:
        return new_lev, f"VOL_NEUTRAL: lev unchanged {new_lev}x"


def volume_filter_v3(df: pd.DataFrame, side: str, regime: str,
                     obv_period: int = 20) -> Tuple[bool, str, Dict]:
    """
    Volume Filter v3 — score-based, almost never rejects.

    Returns: (passed, reason, info)
    passed is almost always True except for critical conditions.
    """
    info = volume_flow_signal_v3(df, obv_period)
    rel_vol = info["rel_vol"]
    score = info["score"]

    # Only hard reject: critically low volume (manipulation risk)
    if rel_vol < 0.3:
        return False, f"CRITICAL_LOW_VOL(RV={rel_vol:.2f}): reject", info

    # Regime context
    directional = volume_score_for_side(info, side)

    if regime == "CHAOS":
        return True, f"CHAOS: vol ignored (score={directional:+.2f})", info

    # All other cases: PASS with scoring info
    if directional > 0.3:
        return True, f"VOL_ALIGN({directional:+.2f},RV={rel_vol:.1f}): pass", info
    elif directional < -0.3:
        return True, f"VOL_MISALIGN({directional:+.2f},RV={rel_vol:.1f}): pass", info
    else:
        return True, f"VOL_NEUTRAL({directional:+.2f},RV={rel_vol:.1f}): pass", info


# =====================================================================
# 🔄  BACKWARD COMPATIBILITY
# =====================================================================

def volume_flow_signal(df: pd.DataFrame, obv_period: int = 20) -> str:
    return volume_flow_signal_v3(df, obv_period)["flow"]


def volume_confirm(df: pd.DataFrame, side: str, obv_period: int = 20) -> bool:
    info = volume_flow_signal_v3(df, obv_period)
    directional = volume_score_for_side(info, side)
    return directional > 0.1  # Soft threshold for backward compat


def volume_filter(df: pd.DataFrame, side: str, regime: str,
                  obv_period: int = 20) -> Tuple[bool, str]:
    passed, reason, _ = volume_filter_v3(df, side, regime, obv_period)
    return passed, reason


def volume_leverage_adjustment(df: pd.DataFrame, regime: str,
                               base_lev: int, side: str,
                               obv_period: int = 20) -> Tuple[int, str]:
    info = volume_flow_signal_v3(df, obv_period)
    return volume_leverage_adjustment_v3(info, side, base_lev)
