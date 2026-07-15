"""
MUFCA v4.0 — Chart Data (JSON)
То же, что считает chart.py для PNG (!chart в Discord), но без matplotlib —
отдаёт JSON для веб-морды. Использует ТЕ ЖЕ функции индикаторов, что и
chart.py/indicators.py, чтобы цифры на сайте 1-в-1 совпадали с картинкой в Discord.
"""

import logging
from typing import Optional, Dict, List

import numpy as np
import pandas as pd

from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe
from indicators import calculate_frama, calculate_mfi, run_kmeans_mfi
from chart import calc_bollinger_bands, calc_support_resistance
import config

logger = logging.getLogger(__name__)


def _series_to_list(s: pd.Series, limit: int) -> List[Optional[float]]:
    """pandas Series -> список float, NaN -> None (JSON не умеет в NaN)."""
    tail = s.tail(limit)
    out = []
    for v in tail.values:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            out.append(None)
        else:
            out.append(round(float(v), 8))
    return out


async def get_chart_data(
    exchange,
    symbol: str,
    timeframe: str,
    limit: int = 150,
    state_snapshot: Optional[dict] = None,
) -> Dict:
    """
    Аналог chart.generate_chart(), но возвращает JSON вместо PNG.

    Возвращает свечи + все оверлеи (FRAMA channel, BB, S/R, MFI + kmeans-пороги)
    ровно за один fetch OHLCV — никакого дублирующего пересчёта.
    """
    fetch_limit = max(limit + 250, 300)
    bars = await safe_fetch_ohlcv(exchange, symbol, timeframe, limit=fetch_limit)
    df = parse_ohlcv(bars)

    if not validate_dataframe(df, min_rows=50):
        raise ValueError(f"Недостаточно данных для {symbol} {timeframe}")

    # ── Индикаторы — те же вызовы, что в chart.generate_chart() ────────
    frama_s, frama_u, frama_l, _ = calculate_frama(
        df, length=config.FRAMA_LEN, mult=config.FRAMA_MULT
    )
    mfi_s = calculate_mfi(df, length=config.MFI_LEN)
    mfi_os, mfi_ob = run_kmeans_mfi(mfi_s, training_size=config.MFI_TRAINING)

    bb_u, bb_m, bb_l = calc_bollinger_bands(df["close"])
    sr = calc_support_resistance(df)

    df_tail = df.tail(limit).reset_index(drop=True)

    candles = [
        {
            "time": int(row.timestamp // 1000),  # unix seconds — под lightweight-charts
            "open": round(float(row.open), 8),
            "high": round(float(row.high), 8),
            "low": round(float(row.low), 8),
            "close": round(float(row.close), 8),
            "volume": round(float(row.volume), 4),
        }
        for row in df_tail.itertuples()
    ]

    result: Dict = {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "frama": _series_to_list(frama_s, limit),
        "frama_upper": _series_to_list(frama_u, limit),
        "frama_lower": _series_to_list(frama_l, limit),
        "bb_upper": _series_to_list(bb_u, limit),
        "bb_mid": _series_to_list(bb_m, limit),
        "bb_lower": _series_to_list(bb_l, limit),
        "mfi": _series_to_list(mfi_s, limit),
        "mfi_overbought": round(float(mfi_ob), 2),
        "mfi_oversold": round(float(mfi_os), 2),
        "support": sr["support"],
        "resistance": sr["resistance"],
    }

    # ── Активная сделка (если передан state_snapshot из !status/state) ──
    if state_snapshot:
        entry_time_ms = state_snapshot.get("entry_time_ms")
        signal_bar_time = None
        if entry_time_ms is not None:
            try:
                ts_arr = df["timestamp"].values
                closest_i = int(np.argmin(np.abs(ts_arr - float(entry_time_ms))))
                signal_bar_time = int(df["timestamp"].iloc[closest_i] // 1000)
            except Exception as e:
                logger.warning(f"[CHART_DATA] Failed to resolve entry_time_ms: {e}")

        result["active_trade"] = {
            "side": state_snapshot.get("side"),
            "entry": state_snapshot.get("entry"),
            "tp": state_snapshot.get("tp"),
            "tp1": state_snapshot.get("tp1"),
            "sl": state_snapshot.get("sl"),
            "signal_bar_time": signal_bar_time,
        }

    return result
