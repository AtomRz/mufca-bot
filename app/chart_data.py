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
from indicators import (
    calculate_frama,
    calculate_mfi,
    calculate_andean,
    calculate_ut_bot,
    run_kmeans_mfi,
    calculate_chop,
    calculate_atr,
)
from chart import calc_bollinger_bands, calc_support_resistance
# 🆕 Лампочки сигналов/фильтров в топ-баре: те же crossover/bars_since хелперы
# и get_htf_bias, что использует signals.check_signals — переиспользуем 1-в-1,
# чтобы лампочки на UI ТОЧНО совпадали с логикой, которая реально решает,
# откроется сделка или нет (никакой отдельной "своей" копии условий).
from signals import (
    crossover,
    crossunder,
    crossover2,
    crossunder2,
    bars_since_crossover,
    bars_since_crossunder,
    bars_since_crossover2,
    bars_since_crossunder2,
    get_htf_bias,
)
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

    bb_u, bb_m, bb_l = calc_bollinger_bands(df["close"], period=config.BB_PERIOD, std_mult=config.BB_STDDEV)
    sr = calc_support_resistance(df, pivot_window=config.SR_PIVOT_WINDOW, max_levels=config.SR_MAX_LEVELS)

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
        # 🆕 FIX: ключ в trade dict называется bar_opened_time (см. signals.py),
        # а не entry_time_ms — из-за этого signal_bar_time всегда был None и
        # маркер сигнала на графике никогда не мог отрисоваться.
        bar_opened_time_ms = state_snapshot.get("bar_opened_time")
        signal_bar_time = None
        if bar_opened_time_ms is not None:
            try:
                ts_arr = df["timestamp"].values
                closest_i = int(np.argmin(np.abs(ts_arr - float(bar_opened_time_ms))))
                # 🆕 FIX: если сделка открылась раньше видимого на графике диапазона
                # (например limit=150 баров, а вход был 300 баров назад), argmin всё
                # равно найдёт "ближайший" бар — обычно самый первый на графике,
                # что рисует маркер в заведомо неверном месте. Проверяем реальную
                # близость по времени (в пределах 1.5 интервала бара); если сделка
                # реально за пределами видимого окна — не рисуем маркер вообще,
                # вместо того чтобы врать о его местоположении.
                bar_interval_ms = float(np.median(np.diff(ts_arr))) if len(ts_arr) > 1 else 0
                actual_diff = abs(ts_arr[closest_i] - float(bar_opened_time_ms))
                if bar_interval_ms <= 0 or actual_diff <= bar_interval_ms * 1.5:
                    signal_bar_time = int(df["timestamp"].iloc[closest_i] // 1000)
            except Exception as e:
                logger.warning(f"[CHART_DATA] Failed to resolve bar_opened_time: {e}")

        result["active_trade"] = {
            "side": state_snapshot.get("side"),
            "entry": state_snapshot.get("entry"),
            "tp": state_snapshot.get("tp"),
            "tp1": state_snapshot.get("tp1"),
            "sl": state_snapshot.get("sl"),
            "tp1_hit": state_snapshot.get("tp1_hit", False),
            "signal_bar_time": signal_bar_time,
        }

    return result


async def get_market_pulse(exchange, ticker: str, tf: str) -> Dict:
    """
    Лёгкая сводка для верхней плашки дашборда: текущий CHOP, направление FRAMA
    (тренд) и грубая informational-оценка leverage — та же формула, что в
    signals.py check_signals (frama_sl_long/short + TARGET_RISK_DEP), но это
    ТОЛЬКО индикативное число для UI, реальный sizing она не определяет
    (см. комментарий в signals.py рядом с sugg_lev).
    """
    # 🆕 limit=900 (а не 300) — как в signals.check_signals: run_kmeans_mfi
    # тренируется на MFI_TRAINING (по умолчанию 800) баров, и уровни
    # перекупленности/перепроданности на меньшей истории будут другими —
    # лампочки должны совпадать с реальным ботом, а не быть "похожими".
    bars = await safe_fetch_ohlcv(exchange, ticker, tf, limit=900)
    df = parse_ohlcv(bars)
    if not validate_dataframe(df, min_rows=50):
        raise ValueError(f"Недостаточно данных для {ticker} {tf}")

    # idx как позитивный целочисленный индекс (не -2!) — bars_since_* хелперы
    # из signals.py делают range(idx, idx-lookback-1, -1), с отрицательным idx
    # это была бы пустая/сломанная последовательность.
    idx = len(df) - 2  # последний подтверждённый бар — как везде в сигналах
    close_v = float(df["close"].iloc[idx])

    chop_s = calculate_chop(df, length=config.CHOP_LENGTH)
    chop_v = float(chop_s.iloc[idx])
    chop_threshold = config.CHOP_THRESHOLD.get(tf, 61.8)

    frama_s, frama_u, frama_l, frama_dir = calculate_frama(df, length=config.FRAMA_LEN, mult=config.FRAMA_MULT)
    dir_v = int(frama_dir.iloc[idx])
    trend = "bullish" if dir_v == 1 else "bearish" if dir_v == -1 else "neutral"

    atr_s = calculate_atr(df, config.ATR_PERIOD)
    atr_v = float(atr_s.iloc[idx])
    fu = float(frama_u.iloc[idx])
    fl = float(frama_l.iloc[idx])

    if atr_v > 0:
        frama_sl_long = max(1.0, min(3.5, abs(close_v - fl) / atr_v))
        frama_sl_short = max(1.0, min(3.5, abs(fu - close_v) / atr_v))
        sugg_sl = frama_sl_long if dir_v >= 0 else frama_sl_short
        sugg_lev = max(1, min(config.MAX_ALLOWED_LEV, int(config.TARGET_RISK_DEP / max(sugg_sl, 0.1))))

        atr_pct_v = (atr_v / close_v) * 100
        if atr_pct_v > config.ATR_MAX:
            sugg_lev = max(1, int(sugg_lev * 0.5))
        elif atr_pct_v > config.ATR_MIN * 1.5:
            sugg_lev = min(config.MAX_ALLOWED_LEV, int(sugg_lev * 1.2))
    else:
        sugg_lev = 1
        atr_pct_v = 0.0

    # =====================================================================
    # 🆕 ЛАМПОЧКИ СИГНАЛОВ И ФИЛЬТРОВ (для топ-бара)
    # Логика 1-в-1 из signals.check_signals — просто без чтения/записи
    # торгового state (позиции/кулдауны здесь не учитываются, это только
    # индикация "что сейчас говорят индикаторы и фильтры", а не "откроется
    # ли сделка на самом деле").
    # =====================================================================
    htf_bias = await get_htf_bias(exchange, ticker, tf)
    htf_bull = htf_bias == 1
    htf_bear = htf_bias == -1

    mfi = calculate_mfi(df, config.MFI_LEN)
    level_os, level_ob = run_kmeans_mfi(mfi, config.MFI_TRAINING)
    and_osc, and_sig = calculate_andean(df, config.AND_LEN, config.AND_SIG_LEN)
    ut_buy, ut_sell = calculate_ut_bot(df, config.UT_SENSITIVITY, config.UT_PERIOD, use_ha=config.UT_HEIKIN_ASHI)

    mfi_bull_sig = crossover(mfi, level_os, idx)
    mfi_bear_sig = crossunder(mfi, level_ob, idx)
    bs_mfi_bull = bars_since_crossover(mfi, level_os, idx, config.LOOKBACK)
    bs_mfi_bear = bars_since_crossunder(mfi, level_ob, idx, config.LOOKBACK)

    and_bull_sig = crossover2(and_osc, and_sig, idx)
    and_bear_sig = crossunder2(and_osc, and_sig, idx)
    bs_and_bull = bars_since_crossover2(and_osc, and_sig, idx, config.LOOKBACK)
    bs_and_bear = bars_since_crossunder2(and_osc, and_sig, idx, config.LOOKBACK)

    def _lamp_state(bull_active: bool, bear_active: bool) -> str:
        if bull_active and not bear_active:
            return "bull"
        if bear_active and not bull_active:
            return "bear"
        return "neutral"

    # MFI/Andean лампочки "держатся" bars_since <= LOOKBACK — как в самом
    # confirm_long_a/confirm_short_a, а не только на баре самого пересечения.
    mfi_lamp = _lamp_state(bs_mfi_bull <= config.LOOKBACK, bs_mfi_bear <= config.LOOKBACK)
    and_lamp = _lamp_state(bs_and_bull <= config.LOOKBACK, bs_and_bear <= config.LOOKBACK)

    # UT Bot реально триггерится только на своём баре (нет "окна" в самой
    # торговой логике) — лампочка честно гаснет уже на следующем баре.
    ut_lamp = _lamp_state(bool(ut_buy.iloc[idx]), bool(ut_sell.iloc[idx]))

    frama_slope = float(frama_s.iloc[idx]) - float(frama_s.iloc[idx - 1])
    slope_long = frama_slope > 0
    slope_short = frama_slope < 0
    frama_bull = dir_v == 1
    frama_bear = dir_v == -1

    chop_ok = chop_v < chop_threshold
    atr_ok = config.ATR_MIN <= atr_pct_v <= config.ATR_MAX

    hh10_prev = float(df["high"].iloc[max(0, idx - 10):idx].max())
    ll10_prev = float(df["low"].iloc[max(0, idx - 10):idx].min())
    fake_break_long = float(df["high"].iloc[idx]) > hh10_prev and close_v < hh10_prev
    fake_break_short = float(df["low"].iloc[idx]) < ll10_prev and close_v > ll10_prev

    ll5_prev = float(df["low"].iloc[max(0, idx - 5):idx].min())
    hh5_prev = float(df["high"].iloc[max(0, idx - 5):idx].max())
    open_v = float(df["open"].iloc[idx])
    liq_sweep_long = float(df["low"].iloc[idx]) < ll5_prev and close_v > ll5_prev and close_v > open_v
    liq_sweep_short = float(df["high"].iloc[idx]) > hh5_prev and close_v < hh5_prev and close_v < open_v

    # pass_long/pass_short повторяют ИМЕННО формулы filter_long/filter_short
    # из signals.py — включая то, что там liq_sweep_short блокирует LONG, а
    # liq_sweep_long блокирует SHORT (так в реальной торговой логике, это не
    # опечатка контроля качества — лампочка обязана показывать как есть).
    lamps = {
        "mfi":    {"kind": "signal", "state": mfi_lamp},
        "andean": {"kind": "signal", "state": and_lamp},
        "ut_bot": {"kind": "signal", "state": ut_lamp},
        "filters": {
            "frama": {
                "enabled": config.ENABLE_FRAMA_FILTER,
                "pass_long": frama_bull and slope_long,
                "pass_short": frama_bear and slope_short,
            },
            "chop": {
                "enabled": config.ENABLE_CHOP_FILTER,
                "pass_long": chop_ok,
                "pass_short": chop_ok,
                "value": round(chop_v, 1),
                "threshold": chop_threshold,
            },
            "atr": {
                "enabled": config.ENABLE_ATR_FILTER,
                "pass_long": atr_ok,
                "pass_short": atr_ok,
                "value": round(atr_pct_v, 2),
            },
            "htf": {
                "enabled": config.ENABLE_MTF_BIAS,
                "pass_long": htf_bull,
                "pass_short": htf_bear,
            },
            "fake_break": {
                "enabled": True,
                "pass_long": not fake_break_long,
                "pass_short": not fake_break_short,
            },
            "liq_sweep": {
                "enabled": True,
                "pass_long": not liq_sweep_short,
                "pass_short": not liq_sweep_long,
            },
        },
    }

    return {
        "ticker": ticker,
        "tf": tf,
        "chop": round(chop_v, 1),
        "chop_threshold": chop_threshold,
        "chop_trending": chop_v < chop_threshold,
        "trend": trend,
        "suggested_leverage": sugg_lev,
        "lamps": lamps,
    }
