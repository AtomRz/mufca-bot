import pandas as pd
import numpy as np
from typing import Tuple

# =====================================================================
# 📊  VOLUME FLOW INDICATORS (OBV + FRAMA)
# =====================================================================

def calculate_obv(df: pd.DataFrame) -> pd.Series:
    """
    On-Balance Volume (OBV).
    Cumulative volume based on price direction.

    Formula:
    - Close > Prev Close: OBV += Volume
    - Close < Prev Close: OBV -= Volume
    - Close = Prev Close: OBV unchanged
    """
    # Vectorized implementation (much faster than loop)
    price_change = df["close"].diff()

    # Sign of price change: +1 for up, -1 for down, 0 for flat
    sign = np.sign(price_change)

    # Signed volume
    signed_volume = sign * df["volume"]

    # Cumulative sum, fill first NaN with 0
    obv = signed_volume.fillna(0).cumsum()

    return obv


def calculate_obv_ema(obv: pd.Series, period: int = 20) -> pd.Series:
    """EMA of OBV for smoothing."""
    return obv.ewm(span=period, adjust=False).mean()


def volume_flow_signal(df: pd.DataFrame, obv_period: int = 20) -> str:
    """
    Определяет направление объёмного потока.

    Returns:
        "inflow"  - OBV above EMA (buying pressure)
        "outflow" - OBV below EMA (selling pressure)
        "neutral" - OBV near EMA (no clear direction)
    """
    if len(df) < obv_period + 5:
        return "neutral"  # Not enough data

    obv = calculate_obv(df)
    obv_ema = calculate_obv_ema(obv, obv_period)

    current_obv = obv.iloc[-1]
    current_ema = obv_ema.iloc[-1]

    # Guard against NaN
    if pd.isna(current_obv) or pd.isna(current_ema) or current_ema == 0:
        return "neutral"

    # 2% buffer to avoid noise
    if current_obv > current_ema * 1.02:
        return "inflow"      # Приток (buying pressure)
    elif current_obv < current_ema * 0.98:
        return "outflow"     # Отток (selling pressure)
    else:
        return "neutral"


def volume_confirm(df: pd.DataFrame, side: str, obv_period: int = 20) -> bool:
    """
    Проверяет, подтверждает ли OBV направление сигнала.

    LONG:  нужен inflow (приток = покупки)
    SHORT: нужен outflow (отток = продажи)
    """
    flow = volume_flow_signal(df, obv_period)

    if side == "long":
        return flow == "inflow"
    else:  # short
        return flow == "outflow"


def volume_filter(df: pd.DataFrame, side: str, regime: str, 
                  obv_period: int = 20) -> Tuple[bool, str]:
    """
    Гибридный volume filter с regime-aware логикой.

    Args:
        df: DataFrame with OHLCV data
        side: "long" or "short"
        regime: "CHAOS", "TREND", or "NORMAL"
        obv_period: EMA period for OBV smoothing

    Returns:
        (passed: bool, reason: str)
    """
    # Режим CHAOS — игнорируем объём (он врёт в хаосе)
    if regime == "CHAOS":
        return True, "CHAOS: volume ignored"

    # Проверяем OBV подтверждение
    confirmed = volume_confirm(df, side, obv_period)
    flow = volume_flow_signal(df, obv_period)

    if regime == "TREND":
        # В тренде — строгое подтверждение
        if confirmed:
            return True, f"TREND: OBV {flow} confirmed"
        else:
            return False, f"TREND: OBV divergence ({flow} vs {side})"

    else:  # NORMAL
        # В норме — достаточно нейтрального или подтверждающего объёма
        if flow == "neutral":
            return True, "NORMAL: OBV neutral (acceptable)"
        elif confirmed:
            return True, f"NORMAL: OBV {flow} confirmed"
        else:
            return False, f"NORMAL: OBV divergence ({flow} vs {side})"


def volume_leverage_adjustment(df: pd.DataFrame, regime: str, 
                               base_lev: int, side: str,
                               obv_period: int = 20) -> Tuple[int, str]:
    """
    Корректирует leverage на основе объёма.

    Returns:
        (adjusted_lev: int, reason: str)
    """
    flow = volume_flow_signal(df, obv_period)
    confirmed = volume_confirm(df, side, obv_period)

    if regime == "CHAOS":
        # В хаосе — снижаем плечо в любом случае
        return max(1, int(base_lev * 0.7)), "CHAOS: lev reduced"

    if confirmed:
        # Объём подтверждает — можно чуть увеличить
        if regime == "TREND":
            return min(base_lev + 1, 10), "TREND + OBV confirm: lev +1"
        else:
            return base_lev, "NORMAL + OBV confirm: lev unchanged"
    else:
        # Объём не подтверждает — снижаем
        if regime == "TREND":
            return max(1, int(base_lev * 0.8)), "TREND + OBV divergence: lev -20%"
        else:
            return max(1, int(base_lev * 0.9)), "NORMAL + OBV divergence: lev -10%"
