import pandas as pd
import numpy as np
from typing import Tuple

# =====================================================================
# 📊  VOLUME FLOW INDICATORS v2 — Relative Volume + OBV Momentum
# =====================================================================

def calculate_obv(df: pd.DataFrame) -> pd.Series:
    """
    On-Balance Volume (OBV).
    """
    price_change = df["close"].diff()
    sign = np.sign(price_change)
    signed_volume = sign * df["volume"]
    obv = signed_volume.fillna(0).cumsum()
    return obv


def calculate_obv_ema(obv: pd.Series, period: int = 20) -> pd.Series:
    """EMA of OBV for smoothing."""
    return obv.ewm(span=period, adjust=False).mean()


def calculate_relative_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """
    Relative Volume = current volume / SMA(volume, period).
    > 1.5 = high volume, < 0.7 = low volume.
    """
    vol_sma = df["volume"].rolling(window=period).mean()
    return df["volume"] / (vol_sma + 1e-12)


def calculate_volume_delta(df: pd.DataFrame) -> pd.Series:
    """
    Volume Delta — оценка buying/selling pressure через позицию close внутри бара.

    close near high = buying pressure (+)
    close near low = selling pressure (-)
    """
    range_ = df["high"] - df["low"]
    # Where in the range did close finish? 0 = at low, 1 = at high
    close_loc = (df["close"] - df["low"]) / (range_ + 1e-12)
    # Map to [-1, 1]: -1 = sold at lows, +1 = bought at highs
    pressure = (close_loc - 0.5) * 2.0
    return pressure * df["volume"]


def calculate_volume_delta_ema(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """EMA of Volume Delta."""
    delta = calculate_volume_delta(df)
    return delta.ewm(span=period, adjust=False).mean()


def volume_flow_signal_v2(df: pd.DataFrame, obv_period: int = 20) -> dict:
    """
    Комплексный анализ объёма v2.

    Returns dict:
        - flow: "inflow" | "outflow" | "neutral"
        - strength: 0.0-1.0 (сила сигнала)
        - rel_vol: относительный объём
        - obv_mom: моментум OBV (изменение за 5 баров)
        - delta_trend: направление volume delta
    """
    if len(df) < obv_period + 10:
        return {
            "flow": "neutral",
            "strength": 0.0,
            "rel_vol": 1.0,
            "obv_mom": 0.0,
            "delta_trend": "neutral",
            "reason": "insufficient data"
        }

    # 1. OBV Flow (classic)
    obv = calculate_obv(df)
    obv_ema = calculate_obv_ema(obv, obv_period)
    current_obv = obv.iloc[-1]
    current_ema = obv_ema.iloc[-1]

    if pd.isna(current_obv) or pd.isna(current_ema) or current_ema == 0:
        obv_flow = "neutral"
        obv_strength = 0.0
    elif current_obv > current_ema * 1.02:
        obv_flow = "inflow"
        obv_strength = min(1.0, (current_obv / current_ema - 1.02) / 0.1)
    elif current_obv < current_ema * 0.98:
        obv_flow = "outflow"
        obv_strength = min(1.0, (0.98 - current_obv / current_ema) / 0.1)
    else:
        obv_flow = "neutral"
        obv_strength = 0.0

    # 2. OBV Momentum (изменение за 5 баров — более актуально чем уровень)
    obv_mom = (obv.iloc[-1] - obv.iloc[-6]) / (abs(obv.iloc[-6]) + 1e-12) * 100

    # 3. Relative Volume
    rel_vol = calculate_relative_volume(df, obv_period).iloc[-1]

    # 4. Volume Delta Trend
    delta = calculate_volume_delta(df)
    delta_ema = delta.ewm(span=obv_period, adjust=False).mean()
    current_delta = delta.iloc[-1]
    current_delta_ema = delta_ema.iloc[-1]

    if pd.isna(current_delta) or pd.isna(current_delta_ema):
        delta_trend = "neutral"
    elif current_delta > current_delta_ema * 1.05:
        delta_trend = "inflow"
    elif current_delta < current_delta_ema * 0.95:
        delta_trend = "outflow"
    else:
        delta_trend = "neutral"

    # 5. Combined flow (взвешенное)
    # OBV flow: 40%, OBV mom: 30%, Delta trend: 30%
    if obv_flow == "inflow":
        score = 0.4 * obv_strength
    elif obv_flow == "outflow":
        score = -0.4 * obv_strength
    else:
        score = 0.0

    # OBV momentum contribution
    if obv_mom > 2:
        score += 0.3 * min(1.0, obv_mom / 10)
    elif obv_mom < -2:
        score -= 0.3 * min(1.0, abs(obv_mom) / 10)

    # Delta trend contribution
    if delta_trend == "inflow":
        score += 0.3
    elif delta_trend == "outflow":
        score -= 0.3

    # Final classification
    if score > 0.25:
        final_flow = "inflow"
    elif score < -0.25:
        final_flow = "outflow"
    else:
        final_flow = "neutral"

    strength = min(1.0, abs(score))

    return {
        "flow": final_flow,
        "strength": round(strength, 2),
        "rel_vol": round(float(rel_vol), 2),
        "obv_mom": round(float(obv_mom), 2),
        "delta_trend": delta_trend,
        "reason": f"OBV:{obv_flow}({obv_strength:.2f})|mom:{obv_mom:.1f}|delta:{delta_trend}|relvol:{rel_vol:.2f}"
    }


def volume_confirm_v2(df: pd.DataFrame, side: str, obv_period: int = 20) -> Tuple[bool, dict]:
    """
    Проверяет, подтверждает ли объём направление сигнала v2.

    Returns: (confirmed: bool, info: dict)
    """
    info = volume_flow_signal_v2(df, obv_period)
    flow = info["flow"]

    if side == "long":
        confirmed = flow == "inflow"
    else:
        confirmed = flow == "outflow"

    return confirmed, info


def volume_filter_v2(df: pd.DataFrame, side: str, regime: str,
                     obv_period: int = 20) -> Tuple[bool, str, dict]:
    """
    Volume Filter v2 — regime-aware с градациями.

    Returns: (passed: bool, reason: str, info: dict)
    """
    info = volume_flow_signal_v2(df, obv_period)
    flow = info["flow"]
    strength = info["strength"]
    rel_vol = info["rel_vol"]

    # Режим CHAOS — игнорируем объём
    if regime == "CHAOS":
        return True, "CHAOS: volume ignored", info

    # Низкий объём — всегда подозрительно
    if rel_vol < 0.5:
        return False, f"LOW_VOLUME: rel_vol={rel_vol:.2f} (too thin)", info

    confirmed, _ = volume_confirm_v2(df, side, obv_period)

    if regime == "TREND":
        # В тренде — мягче. Даже divergence на сильном объёме может быть OK
        if confirmed:
            return True, f"TREND: volume confirmed ({flow}, strength={strength})", info
        elif strength < 0.3 and rel_vol > 1.0:
            # Слабая divergence на высоком объёме — допустимо
            return True, f"TREND: weak divergence accepted (strength={strength}, rel_vol={rel_vol})", info
        else:
            return False, f"TREND: strong divergence rejected ({flow} vs {side}, strength={strength})", info

    else:  # NORMAL
        if flow == "neutral":
            return True, f"NORMAL: neutral volume (rel_vol={rel_vol})", info
        elif confirmed:
            return True, f"NORMAL: confirmed ({flow}, strength={strength})", info
        elif strength < 0.4 and rel_vol > 0.8:
            # Умеренная divergence на нормальном объёме
            return True, f"NORMAL: moderate divergence accepted", info
        else:
            return False, f"NORMAL: divergence rejected ({flow} vs {side}, strength={strength})", info


def volume_leverage_adjustment_v2(df: pd.DataFrame, regime: str,
                                  base_lev: int, side: str,
                                  obv_period: int = 20) -> Tuple[int, str]:
    """
    Корректирует leverage на основе объёма v2.
    """
    info = volume_flow_signal_v2(df, obv_period)
    flow = info["flow"]
    strength = info["strength"]
    rel_vol = info["rel_vol"]
    confirmed, _ = volume_confirm_v2(df, side, obv_period)

    if regime == "CHAOS":
        return max(1, int(base_lev * 0.6)), f"CHAOS: lev reduced to {max(1, int(base_lev * 0.6))}x"

    if confirmed and rel_vol > 1.2 and strength > 0.5:
        # Сильное подтверждение на высоком объёме
        new_lev = min(base_lev + 2, 10)
        return new_lev, f"STRONG_CONFIRM: lev +2 -> {new_lev}x"
    elif confirmed and rel_vol > 0.8:
        new_lev = min(base_lev + 1, 10)
        return new_lev, f"CONFIRM: lev +1 -> {new_lev}x"
    elif not confirmed and strength > 0.5:
        # Сильная divergence
        new_lev = max(1, int(base_lev * 0.6))
        return new_lev, f"STRONG_DIV: lev -40% -> {new_lev}x"
    elif not confirmed:
        new_lev = max(1, int(base_lev * 0.8))
        return new_lev, f"DIV: lev -20% -> {new_lev}x"
    else:
        return base_lev, f"NEUTRAL: lev unchanged"


# =====================================================================
# 🔄  BACKWARD COMPATIBILITY (v1 API)
# =====================================================================

def volume_flow_signal(df: pd.DataFrame, obv_period: int = 20) -> str:
    """Backward compatible — returns just the flow string."""
    return volume_flow_signal_v2(df, obv_period)["flow"]


def volume_confirm(df: pd.DataFrame, side: str, obv_period: int = 20) -> bool:
    """Backward compatible."""
    confirmed, _ = volume_confirm_v2(df, side, obv_period)
    return confirmed


def volume_filter(df: pd.DataFrame, side: str, regime: str,
                  obv_period: int = 20) -> Tuple[bool, str]:
    """Backward compatible — returns (passed, reason)."""
    passed, reason, _ = volume_filter_v2(df, side, regime, obv_period)
    return passed, reason


def volume_leverage_adjustment(df: pd.DataFrame, regime: str,
                               base_lev: int, side: str,
                               obv_period: int = 20) -> Tuple[int, str]:
    """Backward compatible."""
    return volume_leverage_adjustment_v2(df, regime, base_lev, side, obv_period)
