import asyncio
import time
import numpy as np
import pandas as pd
from typing import Tuple, List, Dict, Optional, Any
from datetime import datetime, timezone
import logging

import ccxt

from config import (
    TICKERS,
    TIMEFRAMES,
    UT_HEIKIN_ASHI,
    ATR_PERIOD,
    ATR_MIN,
    ATR_MAX,
    CHOP_LENGTH,
    CHOP_THRESHOLD,
    FRAMA_LEN,
    FRAMA_MULT,
    MFI_LEN,
    MFI_TRAINING,
    AND_LEN,
    AND_SIG_LEN,
    LOOKBACK,
    COOLDOWN_BARS,
    UT_SENSITIVITY,
    UT_PERIOD,
    MAX_ALLOWED_LEV,
    TARGET_RISK_DEP,
    MAX_HOLD_BARS,
    HTF_BIAS,
    HTF_CACHE_TTL_SECONDS,
    MARKET_MODE,
    SIGNAL_HISTORY_LIMIT,
)
from indicators import (
    calculate_atr,
    calculate_chop,
    calculate_frama,
    calculate_mfi,
    calculate_andean,
    calculate_ut_bot,
    run_kmeans_mfi,
    heikin_ashi,
)
from volume_indicators import (
    volume_filter,
    volume_flow_signal,
    volume_leverage_adjustment,
)
from state import (
    load_signals_history,
    save_signals_history,
    add_signal_record,
    update_signal_record,
    update_signal_mae_mfe,
    get_signal_stats,
    calculate_combined_tp,
)
from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe, Timer

logger = logging.getLogger(__name__)

# =====================================================================
# 🧬  HTF BIAS (С КЭШИРОВАНИЕМ)
# =====================================================================

_htf_cache = Timer(HTF_CACHE_TTL_SECONDS)

def clear_htf_cache():
    """Сбрасывает кэш HTF bias — вызывать при изменении !htf."""
    _htf_cache.clear()

async def get_htf_bias(exchange: ccxt.Exchange, ticker: str, timeframe: str) -> int:
    """Возвращает HTF bias с кэшированием."""
    cache_key = f"{ticker}_{timeframe}"
    cached = _htf_cache.get(cache_key)
    if cached is not None:
        return cached

    htf = HTF_BIAS
    try:
        bars = await safe_fetch_ohlcv(exchange, ticker, htf, limit=150)
        if not bars:
            return 0

        df_htf = parse_ohlcv(bars)
        if not validate_dataframe(df_htf, 50):
            return 0

        fs, fu, fl, fdir = calculate_frama(df_htf, FRAMA_LEN, FRAMA_MULT)
        htf_close = float(df_htf["close"].iloc[-2])
        htf_frama = float(fs.iloc[-2])
        bias = 1 if htf_close > htf_frama else -1

        _htf_cache.set(cache_key, bias)
        logger.debug(f"[HTF] {ticker} -> {htf} | bias={'BULL' if bias==1 else 'BEAR'}")
        return bias
    except Exception as e:
        logger.warning(f"HTF Bias ({ticker} {htf}): {e}")
        return 0

# =====================================================================
# 🎯  SL CALCULATION
# =====================================================================

def calculate_sl(
    entry_price: float,
    side: str,
    fs: pd.Series,
    fu: pd.Series,
    fl: pd.Series,
    atr14: pd.Series,
    idx: int
) -> float:
    """Рассчитывает Stop Loss."""
    atr_v = max(float(atr14.iloc[idx]), 1e-8)
    if side == "long":
        sl_frama = float(fl.iloc[idx])
        sl_atr = entry_price - 1.5 * atr_v
        return max(sl_frama, sl_atr)
    else:
        sl_frama = float(fu.iloc[idx])
        sl_atr = entry_price + 1.5 * atr_v
        return min(sl_frama, sl_atr)

# =====================================================================
# 📊  TP/SL CHECK
# =====================================================================

def check_tp_sl_hit(state: Dict, high: float, low: float) -> Optional[str]:
    """Проверяет, был ли пробит TP или SL."""
    trade = state.get("active_trade")
    if not trade:
        return None

    side = trade["side"]
    sl = trade["sl"]
    tp = trade["tp"]

    if side == "long":
        if low <= sl:
            return "sl"
        if high >= tp:
            return "tp"
    else:
        if high >= sl:
            return "sl"
        if low <= tp:
            return "tp"
    return None

def close_trade(state: Dict, exit_price: float, result: str, ticker: str, tf: str) -> Optional[Dict]:
    """Закрывает активную позицию."""
    trade = state.get("active_trade")
    if not trade:
        return None

    entry = trade["entry"]
    side = trade["side"]
    bars_held = state.get("bars_in_trade", 0)
    pnl_pct = (exit_price - entry) / entry * 100 if side == "long" else (entry - exit_price) / entry * 100

    closed_trade = {
        "side": side,
        "entry": entry,
        "sl": trade["sl"],
        "tp": trade["tp"],
        "exit": exit_price,
        "exit_time": datetime.now(timezone.utc).isoformat(),
        "result": result,
        "pnl_pct": round(pnl_pct, 4),
        "bars_held": bars_held,
        "lev": trade.get("lev", 1),
    }

    state["trade_history"].append(closed_trade)
    state["trade_history"] = state["trade_history"][-50:]
    update_signal_record(ticker, tf, side, exit_price, result, bars_held)
    state["active_trade"] = None
    state["bars_in_trade"] = 0
    state["last_closure_notified"] = False

    logger.info(f"[TRADE] Closed {side.upper()} | PnL: {pnl_pct:.2f}% | Result: {result.upper()}")
    return closed_trade

# =====================================================================
# 🧠  CHECK SIGNALS (ОСНОВНАЯ ЛОГИКА)
# =====================================================================

async def check_signals(
    exchange: ccxt.Exchange,
    ticker: str,
    timeframe: str,
    state: Dict[str, Any]
) -> Tuple[List[Tuple], Optional[int], str, int]:
    """
    Проверяет сигналы для пары/таймфрейма.
    Возвращает: (signals, bar_time, regime, leverage)
    """
    try:
        # HTF bias
        htf_bias = await get_htf_bias(exchange, ticker, timeframe)

        # Fetch OHLCV
        bars = await safe_fetch_ohlcv(exchange, ticker, timeframe, limit=900)
        if not bars:
            return [], None, "NO_DATA", 1

        df = parse_ohlcv(bars)
        if not validate_dataframe(df, 100):
            return [], None, "NO_DATA", 1

        current_bar_time = int(df["timestamp"].iloc[-2])
        is_new_bar = (
            state["last_processed_bar_time"] is None or
            current_bar_time > state["last_processed_bar_time"]
        )
        state["last_processed_bar_time"] = current_bar_time

        last_high = float(df["high"].iloc[-2])
        last_low = float(df["low"].iloc[-2])
        last_close = float(df["close"].iloc[-2])

        # Проверка активной позиции
        trade = state.get("active_trade")
        if trade:
            update_signal_mae_mfe(ticker, timeframe, trade["side"], last_close)
            hit = check_tp_sl_hit(state, last_high, last_low)
            if hit:
                exit_price = trade["sl"] if hit == "sl" else trade["tp"]
                close_trade(state, exit_price, hit, ticker, timeframe)
            elif is_new_bar:
                state["bars_in_trade"] = state.get("bars_in_trade", 0) + 1
                if state["bars_in_trade"] >= MAX_HOLD_BARS:
                    close_trade(state, last_close, "cancelled", ticker, timeframe)
                    logger.info(f"[TRADE] Force-closed {trade['side'].upper()} after {MAX_HOLD_BARS} bars")

        # Индикаторы
        atr14 = calculate_atr(df, ATR_PERIOD)
        atr_pct = (atr14 / df["close"]) * 100
        chop = calculate_chop(df, CHOP_LENGTH)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        mfi = calculate_mfi(df, MFI_LEN)
        level_os, level_ob = run_kmeans_mfi(mfi, MFI_TRAINING)
        and_osc, and_sig = calculate_andean(df, AND_LEN, AND_SIG_LEN)
        ut_buy, ut_sell = calculate_ut_bot(df, UT_SENSITIVITY, UT_PERIOD, use_ha=UT_HEIKIN_ASHI)

        idx = len(df) - 2
        bar_idx = idx
        bar_time = int(df["timestamp"].iloc[idx])

        close_v = float(df["close"].iloc[idx])
        open_v = float(df["open"].iloc[idx])
        atr_v = max(float(atr14.iloc[idx]), 1e-8)
        atr_pct_v = float(atr_pct.iloc[idx])
        chop_v = float(chop.iloc[idx])

        atr_ok = ATR_MIN <= atr_pct_v <= ATR_MAX
        chop_ok = chop_v < CHOP_THRESHOLD.get(timeframe, 61.8)

        frama_slope = float(fs.iloc[idx]) - float(fs.iloc[idx - 1])
        slope_long = frama_slope > 0
        slope_short = frama_slope < 0

        frama_dir_v = int(fdir.iloc[idx])
        frama_bull = frama_dir_v == 1
        frama_bear = frama_dir_v == -1

        htf_bull = htf_bias == 1
        htf_bear = htf_bias == -1

        # Фильтры
        hh10_prev = float(df["high"].iloc[max(0, idx-10):idx].max())
        ll10_prev = float(df["low"].iloc[max(0, idx-10):idx].min())  # ✅ ИСПРАВЛЕНО: .min()
        fake_break_long = float(df["high"].iloc[idx]) > hh10_prev and close_v < hh10_prev
        fake_break_short = float(df["low"].iloc[idx]) < ll10_prev and close_v > ll10_prev

        ll5_prev = float(df["low"].iloc[max(0, idx-5):idx].min())
        hh5_prev = float(df["high"].iloc[max(0, idx-5):idx].max())
        liq_sweep_long = float(df["low"].iloc[idx]) < ll5_prev and close_v > ll5_prev and close_v > open_v
        liq_sweep_short = float(df["high"].iloc[idx]) > hh5_prev and close_v < hh5_prev and close_v < open_v

        # Режим
        regime = "CHAOS" if atr_pct_v > ATR_MAX else "TREND" if atr_pct_v > ATR_MIN * 1.5 else "NORMAL"

        # 🆕 Volume filter (regime-aware)
        vol_passed_long, vol_reason_long = volume_filter(df, "long", regime)
        vol_passed_short, vol_reason_short = volume_filter(df, "short", regime)

        filter_long = (
            frama_bull and chop_ok and atr_ok and slope_long
            and htf_bull
            and not fake_break_long
            and not liq_sweep_short
            and vol_passed_long
        )
        filter_short = (
            frama_bear and chop_ok and atr_ok and slope_short
            and htf_bear
            and not fake_break_short
            and not liq_sweep_long
            and vol_passed_short
        )

        # 🆕 Log volume filter rejections for debugging
        if not vol_passed_long and (mfi_bull_sig or and_bull_sig or bool(ut_buy.iloc[idx])):
            logger.info(f"[VOLUME] {ticker} {timeframe} LONG rejected: {vol_reason_long}")
        if not vol_passed_short and (mfi_bear_sig or and_bear_sig or bool(ut_sell.iloc[idx])):
            logger.info(f"[VOLUME] {ticker} {timeframe} SHORT rejected: {vol_reason_short}")

        # Сигналы
        def crossover(s, lvl, i):
            if i < 1:
                return False
            return float(s.iloc[i]) > lvl and float(s.iloc[i-1]) <= lvl

        def crossunder(s, lvl, i):
            if i < 1:
                return False
            return float(s.iloc[i]) < lvl and float(s.iloc[i-1]) >= lvl

        def crossover2(s1, s2, i):
            if i < 1:
                return False
            return float(s1.iloc[i]) > float(s2.iloc[i]) and float(s1.iloc[i-1]) <= float(s2.iloc[i-1])

        def crossunder2(s1, s2, i):
            if i < 1:
                return False
            return float(s1.iloc[i]) < float(s2.iloc[i]) and float(s1.iloc[i-1]) >= float(s2.iloc[i-1])

        mfi_bull_sig = crossover(mfi, level_os, idx)
        mfi_bear_sig = crossunder(mfi, level_ob, idx)
        and_bull_sig = crossover2(and_osc, and_sig, idx)
        and_bear_sig = crossunder2(and_osc, and_sig, idx)

        def bars_since_crossover(s, lvl, cur):
            for k in range(cur, max(cur - LOOKBACK - 1, 1), -1):  # ✅ ИСПРАВЛЕНО: min k=1
                if float(s.iloc[k]) > lvl and float(s.iloc[k-1]) <= lvl:
                    return cur - k
            return 999

        def bars_since_crossunder(s, lvl, cur):
            for k in range(cur, max(cur - LOOKBACK - 1, 1), -1):  # ✅ ИСПРАВЛЕНО: min k=1
                if float(s.iloc[k]) < lvl and float(s.iloc[k-1]) >= lvl:
                    return cur - k
            return 999

        def bars_since_crossover2(s1, s2, cur):
            for k in range(cur, max(cur - LOOKBACK - 1, 1), -1):  # ✅ ИСПРАВЛЕНО: min k=1
                if float(s1.iloc[k]) > float(s2.iloc[k]) and float(s1.iloc[k-1]) <= float(s2.iloc[k-1]):
                    return cur - k
            return 999

        def bars_since_crossunder2(s1, s2, cur):
            for k in range(cur, max(cur - LOOKBACK - 1, 1), -1):  # ✅ ИСПРАВЛЕНО: min k=1
                if float(s1.iloc[k]) < float(s2.iloc[k]) and float(s1.iloc[k-1]) >= float(s2.iloc[k-1]):
                    return cur - k
            return 999

        bs_and_bull = bars_since_crossover2(and_osc, and_sig, idx)
        bs_mfi_bull = bars_since_crossover(mfi, level_os, idx)
        bs_and_bear = bars_since_crossunder2(and_osc, and_sig, idx)
        bs_mfi_bear = bars_since_crossunder(mfi, level_ob, idx)

        confirm_long_a = (mfi_bull_sig and bs_and_bull <= LOOKBACK) or (and_bull_sig and bs_mfi_bull <= LOOKBACK)
        confirm_short_a = (mfi_bear_sig and bs_and_bear <= LOOKBACK) or (and_bear_sig and bs_mfi_bear <= LOOKBACK)

        def cooldown_ok(last_bar):
            return last_bar is None or (bar_idx - last_bar) > COOLDOWN_BARS

        a_long_cd_ok = cooldown_ok(state["last_a_long_bar"])
        a_short_cd_ok = cooldown_ok(state["last_a_short_bar"])
        u_long_cd_ok = cooldown_ok(state["last_u_long_bar"])
        u_short_cd_ok = cooldown_ok(state["last_u_short_bar"])

        a_in_pos = state["a_in_long"] or state["a_in_short"]
        u_in_pos = state["u_in_long"] or state["u_in_short"]

        sig_a_long = confirm_long_a and filter_long and not a_in_pos and a_long_cd_ok
        sig_a_short = confirm_short_a and filter_short and not a_in_pos and a_short_cd_ok
        sig_u_long = bool(ut_buy.iloc[idx]) and filter_long and not u_in_pos and u_long_cd_ok and is_new_bar
        sig_u_short = bool(ut_sell.iloc[idx]) and filter_short and not u_in_pos and u_short_cd_ok and is_new_bar

        # Обновляем состояния
        if sig_a_long:
            state["a_in_long"] = True
            state["a_in_short"] = False
            state["a_long_bar"] = bar_idx
            state["last_a_long_bar"] = bar_idx
        if sig_a_short:
            state["a_in_short"] = True
            state["a_in_long"] = False
            state["a_short_bar"] = bar_idx
            state["last_a_short_bar"] = bar_idx
        if sig_u_long:
            state["u_in_long"] = True
            state["u_in_short"] = False
            state["u_long_bar"] = bar_idx
            state["last_u_long_bar"] = bar_idx
        if sig_u_short:
            state["u_in_short"] = True
            state["u_in_long"] = False
            state["u_short_bar"] = bar_idx
            state["last_u_short_bar"] = bar_idx

        # Леверидж
        frama_sl_long = max(1.0, min(3.5, abs(close_v - float(fl.iloc[idx])) / atr_v))
        frama_sl_short = max(1.0, min(3.5, abs(float(fu.iloc[idx]) - close_v) / atr_v))
        sugg_sl = frama_sl_long if (sig_a_long or sig_u_long) else frama_sl_short
        sugg_lev = max(1, min(MAX_ALLOWED_LEV, int(TARGET_RISK_DEP / max(sugg_sl, 0.1))))

        if regime == "CHAOS":
            sugg_lev = max(1, int(sugg_lev * 0.5))
        if regime == "TREND":
            sugg_lev = min(MAX_ALLOWED_LEV, int(sugg_lev * 1.2))

        # 🆕 Volume-based leverage adjustment
        if (sig_a_long or sig_u_long):
            sugg_lev, vol_lev_reason = volume_leverage_adjustment(
                df, regime, sugg_lev, "long"
            )
        elif (sig_a_short or sig_u_short):
            sugg_lev, vol_lev_reason = volume_leverage_adjustment(
                df, regime, sugg_lev, "short"
            )

        def calc_confidence(is_long: bool) -> int:
            score = 20 if chop_ok else 0
            score += 20 if atr_ok else 0
            score += 15 if (frama_bull if is_long else frama_bear) else 0
            a_sig = sig_a_long if is_long else sig_a_short
            u_sig = sig_u_long if is_long else sig_u_short
            score += 25 if (a_sig and u_sig) else 10 if (a_sig or u_sig) else 0
            score += 20 if (htf_bull if is_long else htf_bear) else 0
            return min(score, 100)

        signals = []
        MIN_RR = 1.0

        # --- LONG ---
        if (sig_a_long or sig_u_long) and not state.get("active_trade"):
            sl = calculate_sl(close_v, "long", fs, fu, fl, atr14, idx)
            tp, tp_desc = calculate_combined_tp(ticker, timeframe, "long", close_v, sl, df, idx, atr14, regime)
            risk = abs(close_v - sl)
            reward = abs(tp - close_v)
            rr = reward / max(risk, 1e-8)

            bar_low = float(df["low"].iloc[idx])
            if rr < MIN_RR:
                logger.info(f"[FILTER] {ticker} {timeframe} LONG skipped — R:R={rr:.2f} < {MIN_RR}")
                sig_a_long = sig_u_long = False
            elif bar_low <= sl:
                logger.info(f"[FILTER] {ticker} {timeframe} LONG skipped — bar low below SL")
                sig_a_long = sig_u_long = False
            else:
                state["active_trade"] = {
                    "side": "long",
                    "entry": close_v,
                    "sl": sl,
                    "tp": tp,
                    "lev": sugg_lev,
                    "bar_opened": bar_idx
                }
                state["bars_in_trade"] = 0
                add_signal_record(ticker, timeframe, "long", close_v, datetime.now(timezone.utc).isoformat(), regime)
                stats = get_signal_stats(ticker, timeframe, "long")

                if sig_a_long:
                    signals.append(("A BUY  (Andean+MFI)", close_v, regime, sugg_lev, bar_time, calc_confidence(True), sl, tp, risk, stats, tp_desc))
                if sig_u_long:
                    signals.append(("U BUY  (UT Bot)", close_v, regime, sugg_lev, bar_time, calc_confidence(True), sl, tp, risk, stats, tp_desc))

        # --- SHORT ---
        if (sig_a_short or sig_u_short) and not state.get("active_trade"):
            sl = calculate_sl(close_v, "short", fs, fu, fl, atr14, idx)
            tp, tp_desc = calculate_combined_tp(ticker, timeframe, "short", close_v, sl, df, idx, atr14, regime)
            risk = abs(sl - close_v)
            reward = abs(close_v - tp)
            rr = reward / max(risk, 1e-8)

            bar_high = float(df["high"].iloc[idx])
            if rr < MIN_RR:
                logger.info(f"[FILTER] {ticker} {timeframe} SHORT skipped — R:R={rr:.2f} < {MIN_RR}")
                sig_a_short = sig_u_short = False
            elif bar_high >= sl:
                logger.info(f"[FILTER] {ticker} {timeframe} SHORT skipped — bar high above SL")
                sig_a_short = sig_u_short = False
            else:
                state["active_trade"] = {
                    "side": "short",
                    "entry": close_v,
                    "sl": sl,
                    "tp": tp,
                    "lev": sugg_lev,
                    "bar_opened": bar_idx
                }
                state["bars_in_trade"] = 0
                add_signal_record(ticker, timeframe, "short", close_v, datetime.now(timezone.utc).isoformat(), regime)
                stats = get_signal_stats(ticker, timeframe, "short")

                if sig_a_short:
                    signals.append(("A SELL (Andean+MFI)", close_v, regime, sugg_lev, bar_time, calc_confidence(False), sl, tp, risk, stats, tp_desc))
                if sig_u_short:
                    signals.append(("U SELL (UT Bot)", close_v, regime, sugg_lev, bar_time, calc_confidence(False), sl, tp, risk, stats, tp_desc))

        return signals, bar_time, regime, sugg_lev

    except Exception as e:
        logger.error(f"check_signals({ticker}, {timeframe}): {e}", exc_info=True)
        return [], None, "ERROR", 1

# =====================================================================
# 🔙  BACKTEST
# =====================================================================

def backtest_history(
    exchange: ccxt.Exchange,
    ticker: str,
    tf: str,
    num_bars: int = 3000
) -> int:
    """Бэктест для накопления истории сигналов."""
    logger.info(f"[BACKTEST] Starting {ticker} {tf} ({num_bars} bars)...")

    try:
        bars = exchange.fetch_ohlcv(ticker, tf, limit=num_bars)
        if not bars or len(bars) < 100:
            logger.warning(f"[BACKTEST] Not enough bars for {ticker} {tf}")
            return 0

        df = parse_ohlcv(bars)
        if not validate_dataframe(df, 100):
            return 0

        atr14 = calculate_atr(df, ATR_PERIOD)
        atr_pct = (atr14 / df["close"]) * 100
        chop = calculate_chop(df, CHOP_LENGTH)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        mfi = calculate_mfi(df, MFI_LEN)
        level_os, level_ob = run_kmeans_mfi(mfi, MFI_TRAINING)
        and_osc, and_sig = calculate_andean(df, AND_LEN, AND_SIG_LEN)
        ut_buy, ut_sell = calculate_ut_bot(df, UT_SENSITIVITY, UT_PERIOD, use_ha=UT_HEIKIN_ASHI)

        signals_found = 0
        history = load_signals_history()

        def crossover(s, lvl, i):
            if i < 1:
                return False
            return float(s.iloc[i]) > lvl and float(s.iloc[i-1]) <= lvl

        def crossunder(s, lvl, i):
            if i < 1:
                return False
            return float(s.iloc[i]) < lvl and float(s.iloc[i-1]) >= lvl

        def crossover2(s1, s2, i):
            if i < 1:
                return False
            return float(s1.iloc[i]) > float(s2.iloc[i]) and float(s1.iloc[i-1]) <= float(s2.iloc[i-1])

        def crossunder2(s1, s2, i):
            if i < 1:
                return False
            return float(s1.iloc[i]) < float(s2.iloc[i]) and float(s1.iloc[i-1]) >= float(s2.iloc[i-1])

        for idx in range(50, len(df) - 100):
            close_v = float(df["close"].iloc[idx])
            open_v = float(df["open"].iloc[idx])
            atr_v = max(float(atr14.iloc[idx]), 1e-8)
            atr_pct_v = float(atr_pct.iloc[idx])
            chop_v = float(chop.iloc[idx])

            atr_ok = ATR_MIN <= atr_pct_v <= ATR_MAX
            chop_ok = chop_v < CHOP_THRESHOLD.get(tf, 61.8)

            frama_slope = float(fs.iloc[idx]) - float(fs.iloc[idx - 1])
            slope_long = frama_slope > 0
            slope_short = frama_slope < 0

            frama_dir_v = int(fdir.iloc[idx])
            frama_bull = frama_dir_v == 1
            frama_bear = frama_dir_v == -1

            # Фильтры
            hh10_prev = float(df["high"].iloc[max(0, idx-10):idx].max())
            ll10_prev = float(df["low"].iloc[max(0, idx-10):idx].min())  # ✅ ИСПРАВЛЕНО
            fake_break_long = float(df["high"].iloc[idx]) > hh10_prev and close_v < hh10_prev
            fake_break_short = float(df["low"].iloc[idx]) < ll10_prev and close_v > ll10_prev

            ll5_prev = float(df["low"].iloc[max(0, idx-5):idx].min())
            hh5_prev = float(df["high"].iloc[max(0, idx-5):idx].max())
            liq_sweep_long = float(df["low"].iloc[idx]) < ll5_prev and close_v > ll5_prev and close_v > open_v
            liq_sweep_short = float(df["high"].iloc[idx]) > hh5_prev and close_v < hh5_prev and close_v < open_v

            # Режим для бэктеста
            bt_regime = "CHAOS" if atr_pct_v > ATR_MAX else "TREND" if atr_pct_v > ATR_MIN * 1.5 else "NORMAL"

            # 🆕 Volume filter для бэктеста (regime-aware)
            vol_passed_long, _ = volume_filter(df.iloc[:idx+1], "long", bt_regime)
            vol_passed_short, _ = volume_filter(df.iloc[:idx+1], "short", bt_regime)

            filter_long = (
                frama_bull and chop_ok and atr_ok and slope_long
                and not fake_break_long
                and not liq_sweep_short
                and vol_passed_long
            )
            filter_short = (
                frama_bear and chop_ok and atr_ok and slope_short
                and not fake_break_short
                and not liq_sweep_long
                and vol_passed_short
            )

            # Сигналы (упрощенные для бэктеста — без HTF bias)
            mfi_bull_sig = crossover(mfi, level_os, idx)
            mfi_bear_sig = crossunder(mfi, level_ob, idx)
            and_bull_sig = crossover2(and_osc, and_sig, idx)
            and_bear_sig = crossunder2(and_osc, and_sig, idx)

            bs_and_bull = 0
            bs_mfi_bull = 0
            bs_and_bear = 0
            bs_mfi_bear = 0

            for k in range(idx, max(idx - LOOKBACK - 1, 1), -1):  # ✅ ИСПРАВЛЕНО
                if crossover2(and_osc, and_sig, k):
                    bs_and_bull = idx - k
                    break
            for k in range(idx, max(idx - LOOKBACK - 1, 1), -1):  # ✅ ИСПРАВЛЕНО
                if crossover(mfi, level_os, k):
                    bs_mfi_bull = idx - k
                    break
            for k in range(idx, max(idx - LOOKBACK - 1, 1), -1):  # ✅ ИСПРАВЛЕНО
                if crossunder2(and_osc, and_sig, k):
                    bs_and_bear = idx - k
                    break
            for k in range(idx, max(idx - LOOKBACK - 1, 1), -1):  # ✅ ИСПРАВЛЕНО
                if crossunder(mfi, level_ob, k):
                    bs_mfi_bear = idx - k
                    break

            confirm_long_a = (mfi_bull_sig and bs_and_bull <= LOOKBACK) or (and_bull_sig and bs_mfi_bull <= LOOKBACK)
            confirm_short_a = (mfi_bear_sig and bs_and_bear <= LOOKBACK) or (and_bear_sig and bs_mfi_bear <= LOOKBACK)

            sig_a_long = confirm_long_a and filter_long
            sig_a_short = confirm_short_a and filter_short
            sig_u_long = bool(ut_buy.iloc[idx]) and filter_long
            sig_u_short = bool(ut_sell.iloc[idx]) and filter_short

            for side, sig_ok in [("long", sig_a_long or sig_u_long), ("short", sig_a_short or sig_u_short)]:
                if not sig_ok:
                    continue

                sl = calculate_sl(close_v, side, fs, fu, fl, atr14, idx)

                # FIXED R:R 2.0 для бэктеста
                risk_fixed = abs(close_v - sl)
                tp = close_v + (2.0 * risk_fixed) if side == "long" else close_v - (2.0 * risk_fixed)

                tp_hit = sl_hit = False
                max_favorable = max_adverse = 0.0
                exit_price = close_v
                bars_held = 0

                for future_idx in range(idx + 1, min(idx + 101, len(df))):
                    fh = float(df["high"].iloc[future_idx])
                    fl_ = float(df["low"].iloc[future_idx])
                    fc = float(df["close"].iloc[future_idx])

                    if side == "long":
                        favorable = (fh - close_v) / close_v * 100
                        adverse = (close_v - fl_) / close_v * 100
                        max_favorable = max(max_favorable, favorable)
                        max_adverse = max(max_adverse, adverse)
                        if fl_ <= sl:
                            sl_hit = True
                            exit_price = sl
                            bars_held = future_idx - idx
                            break
                        if fh >= tp:
                            tp_hit = True
                            exit_price = tp
                            bars_held = future_idx - idx
                            break
                    else:
                        favorable = (close_v - fl_) / close_v * 100
                        adverse = (fh - close_v) / close_v * 100
                        max_favorable = max(max_favorable, favorable)
                        max_adverse = max(max_adverse, adverse)
                        if fh >= sl:
                            sl_hit = True
                            exit_price = sl
                            bars_held = future_idx - idx
                            break
                        if fl_ <= tp:
                            tp_hit = True
                            exit_price = tp
                            bars_held = future_idx - idx
                            break
                    bars_held = future_idx - idx

                # Если не сработал TP/SL — используем последнюю цену
                if not tp_hit and not sl_hit and bars_held > 0:
                    exit_price = float(df["close"].iloc[min(idx + bars_held, len(df) - 1)])

                # Сохраняем сигнал
                _ensure_history_slot(history, ticker, tf)
                exit_type = "tp" if tp_hit else "sl" if sl_hit else "cancelled"
                moved_pct = (exit_price - close_v) / close_v * 100 if side == "long" else (close_v - exit_price) / close_v * 100

                history[ticker][tf][side].append({
                    "entry": round(close_v, 4),
                    "exit": round(exit_price, 4),
                    "exit_type": exit_type,
                    "bars_held": bars_held,
                    "moved_pct": round(moved_pct, 4),
                    "timestamp": str(int(df["timestamp"].iloc[idx])),
                    "max_favorable_pct": round(max_favorable, 4),
                    "max_adverse_pct": round(max_adverse, 4),
                    "regime": bt_regime,
                })
                history[ticker][tf][side] = history[ticker][tf][side][-(SIGNAL_HISTORY_LIMIT * 3):]
                signals_found += 1

        save_signals_history(history)
        logger.info(f"[BACKTEST] {ticker} {tf}: found {signals_found} historical signals")
        return signals_found

    except Exception as e:
        logger.error(f"[BACKTEST] Failed for {ticker} {tf}: {e}", exc_info=True)
        return 0

# =====================================================================
# 🔄  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================

def make_state() -> Dict:
    """Создает начальное состояние."""
    return {
        "a_in_long": False,
        "a_in_short": False,
        "a_long_bar": None,
        "a_short_bar": None,
        "u_in_long": False,
        "u_in_short": False,
        "u_long_bar": None,
        "u_short_bar": None,
        "last_a_long_bar": None,
        "last_a_short_bar": None,
        "last_u_long_bar": None,
        "last_u_short_bar": None,
        "last_bar_time": None,
        "last_processed_bar_time": None,
        "active_trade": None,
        "trade_history": [],
        "bars_in_trade": 0,
        "last_closure_notified": False,
    }

def _ensure_history_slot(history: Dict, ticker: str, tf: str):
    """Создает слот для истории если его нет."""
    if ticker not in history:
        history[ticker] = {}
    if tf not in history[ticker]:
        history[ticker][tf] = {"long": [], "short": []}
