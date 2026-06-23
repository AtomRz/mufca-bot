import asyncio
import time
import numpy as np
import pandas as pd
from typing import Tuple, List, Dict, Optional, Any
from datetime import datetime, timezone
import logging

import ccxt

import config as _cfg
from config import (
    TICKERS,
    TIMEFRAMES,
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
    HTF_CACHE_TTL_SECONDS,
    MARKET_MODE,
    SIGNAL_HISTORY_LIMIT,
    ENABLE_FRAMA_FILTER,
    ENABLE_CHOP_FILTER,
    ENABLE_ATR_FILTER,
    ENABLE_MTF_BIAS,
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
    volume_flow_signal_v3,
    volume_leverage_adjustment_v3,
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
from config import ONCHAIN_ENABLED

logger = logging.getLogger(__name__)

# =====================================================================
# 🔄  CROSSOVER HELPERS (module level for reuse)
# =====================================================================

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

def bars_since_crossover2(s1, s2, cur, lookback):
    for k in range(cur, max(cur - lookback - 1, 1), -1):
        if float(s1.iloc[k]) > float(s2.iloc[k]) and float(s1.iloc[k-1]) <= float(s2.iloc[k-1]):
            return cur - k
    return 999

def bars_since_crossunder2(s1, s2, cur, lookback):
    for k in range(cur, max(cur - lookback - 1, 1), -1):
        if float(s1.iloc[k]) < float(s2.iloc[k]) and float(s1.iloc[k-1]) >= float(s2.iloc[k-1]):
            return cur - k
    return 999

def bars_since_crossover(s, lvl, cur, lookback):
    for k in range(cur, max(cur - lookback - 1, 1), -1):
        if float(s.iloc[k]) > lvl and float(s.iloc[k-1]) <= lvl:
            return cur - k
    return 999

def bars_since_crossunder(s, lvl, cur, lookback):
    for k in range(cur, max(cur - lookback - 1, 1), -1):
        if float(s.iloc[k]) < lvl and float(s.iloc[k-1]) >= lvl:
            return cur - k
    return 999

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

    htf = _cfg.HTF_BIAS
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
    """Рассчитывает Stop Loss с валидацией (SL не пересекает цену входа)."""
    atr_v = max(float(atr14.iloc[idx]), 1e-8)
    if side == "long":
        sl_frama = float(fl.iloc[idx])
        sl_atr = entry_price - 1.5 * atr_v
        sl = max(sl_frama, sl_atr)
        return min(sl, entry_price * 0.995)
    else:
        sl_frama = float(fu.iloc[idx])
        sl_atr = entry_price + 1.5 * atr_v
        sl = min(sl_frama, sl_atr)
        return max(sl, entry_price * 1.005)

# =====================================================================
# 🆕 ON-CHAIN TP/SL SAFETY WRAPPER
# =====================================================================

def apply_onchain_with_safety(
    entry: float,
    sl: float,
    tp: float,
    side: str,
    onchain_bias: Optional[Dict],
    min_rr: float = 1.0,
    max_sl_widen_pct: float = 0.15,  # Максимальное расширение SL на 15%
) -> Tuple[float, float, str, bool]:
    """
    Применяет on-chain множители к TP/SL с проверкой безопасности.

    Returns: (new_tp, new_sl, desc_suffix, applied_ok)
    applied_ok = False если on-chain ухудшил R:R ниже min_rr и был отклонён
    """
    if not onchain_bias or not ONCHAIN_ENABLED:
        return tp, sl, "", True

    if side == "long":
        tp_mult = onchain_bias.get("tp_mult_long", 1.0)
        sl_mult = onchain_bias.get("sl_mult_long", 1.0)
    else:
        tp_mult = onchain_bias.get("tp_mult_short", 1.0)
        sl_mult = onchain_bias.get("sl_mult_short", 1.0)

    if tp_mult == 1.0 and sl_mult == 1.0:
        return tp, sl, "", True

    risk_before = abs(entry - sl)
    reward_before = abs(tp - entry)
    rr_before = reward_before / max(risk_before, 1e-8)

    # Применяем множители
    if side == "long":
        new_tp = entry + reward_before * tp_mult
        # SL mult > 1 = SL дальше (шире), < 1 = SL ближе
        new_sl = entry - risk_before * sl_mult
    else:
        new_tp = entry - reward_before * tp_mult
        new_sl = entry + risk_before * sl_mult

    risk_after = abs(entry - new_sl)
    reward_after = abs(new_tp - entry)
    rr_after = reward_after / max(risk_after, 1e-8)

    # 🆕 SAFETY CHECKS
    desc_suffix = ""

    # 1. Если RR упал ниже минимума — откатываем TP к минимально допустимому
    if rr_after < min_rr:
        logger.warning(f"[ONCHAIN-SAFETY] RR degraded {rr_before:.2f} -> {rr_after:.2f} (below {min_rr}), using conservative")
        # Восстанавливаем RR = min_rr, сохраняя направление on-chain если возможно
        if side == "long":
            # Минимальный TP = entry + risk_after * min_rr
            safe_tp = entry + risk_after * min_rr
            # Но не хуже оригинального TP
            new_tp = max(safe_tp, tp)
        else:
            safe_tp = entry - risk_after * min_rr
            new_tp = min(safe_tp, tp)

        reward_after = abs(new_tp - entry)
        rr_after = reward_after / max(risk_after, 1e-8)
        desc_suffix = f" | OC×TP{tp_mult}/SL{sl_mult} [SAFETY: RR capped @ {rr_after:.2f}]"
        return new_tp, new_sl, desc_suffix, False

    # 2. Если SL расширен слишком сильно — ограничиваем
    sl_widen = (risk_after - risk_before) / risk_before
    if sl_widen > max_sl_widen_pct:
        logger.warning(f"[ONCHAIN-SAFETY] SL widened {sl_widen:.1%} > max {max_sl_widen_pct:.1%}, capping")
        if side == "long":
            new_sl = entry - risk_before * (1 + max_sl_widen_pct)
        else:
            new_sl = entry + risk_before * (1 + max_sl_widen_pct)
        risk_after = abs(entry - new_sl)
        reward_after = abs(new_tp - entry)
        rr_after = reward_after / max(risk_after, 1e-8)
        desc_suffix = f" | OC×TP{tp_mult}/SL{sl_mult} [SL capped +{max_sl_widen_pct:.0%}]"
        return new_tp, new_sl, desc_suffix, True

    desc_suffix = f" | OC×TP{tp_mult}/SL{sl_mult} [RR {rr_before:.2f}→{rr_after:.2f}]"
    return new_tp, new_sl, desc_suffix, True

# =====================================================================
# 📊  TP/SL CHECK
# =====================================================================

def check_tp_sl_hit(state: Dict, high: float, low: float, track: str = "a") -> Optional[str]:
    """Проверяет, был ли пробит TP или SL для указанного трека."""
    trade = state.get(f"{track}_active_trade")
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

def close_trade(state: Dict, exit_price: float, result: str, ticker: str, tf: str, track: str = "a") -> Optional[Dict]:
    """Закрывает активную позицию указанного трека (a или u)."""
    trade_key = f"{track}_active_trade"
    trade = state.get(trade_key)
    if not trade:
        return None

    entry = trade["entry"]
    side = trade["side"]
    bars_key = f"{track}_bars_in_trade"
    bars_held = state.get(bars_key, 0)
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
        "track": track,
    }

    history_key = f"{track}_trade_history"
    state[history_key].append(closed_trade)
    state[history_key] = state[history_key][-50:]

    state["trade_history"].append(closed_trade)
    state["trade_history"] = state["trade_history"][-50:]

    update_signal_record(ticker, tf, side, exit_price, result, bars_held)

    state[trade_key] = None
    state[bars_key] = 0
    notified_key = f"{track}_last_closure_notified"
    state[notified_key] = False

    if track == "a":
        state["a_in_long"] = False
        state["a_in_short"] = False
    else:
        state["u_in_long"] = False
        state["u_in_short"] = False

    state["active_trade"] = state.get("a_active_trade") or state.get("u_active_trade")
    state["bars_in_trade"] = max(state.get("a_bars_in_trade", 0), state.get("u_bars_in_trade", 0))
    state["last_closure_notified"] = state.get("a_last_closure_notified", False) and state.get("u_last_closure_notified", False)

    logger.info(f"[TRADE] Closed {track.upper()}-track {side.upper()} | PnL: {pnl_pct:.2f}% | Result: {result.upper()}")
    return closed_trade

# =====================================================================
# 🧠  CHECK SIGNALS (ОСНОВНАЯ ЛОГИКА)
# =====================================================================

async def check_signals(
    exchange: ccxt.Exchange,
    ticker: str,
    timeframe: str,
    state: Dict[str, Any],
    dry_run: bool = False,
    onchain_bias: Optional[Dict] = None,
) -> Tuple[List[Tuple], Optional[int], str, int]:
    """
    Проверяет сигналы для пары/таймфрейма.
    Возвращает: (signals, bar_time, regime, leverage)
    dry_run=True       — не записывает сигналы в историю (используется в !scan).
    onchain_bias=dict  — данные из onchain.get_onchain_bias(), влияют на TP/SL/lev/confidence.
    """
    try:
        htf_bias = await get_htf_bias(exchange, ticker, timeframe)

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

        for track in ("a", "u"):
            trade = state.get(f"{track}_active_trade")
            if trade:
                update_signal_mae_mfe(ticker, timeframe, trade["side"], last_close)
                hit = check_tp_sl_hit(state, last_high, last_low, track)
                if hit:
                    exit_price = trade["sl"] if hit == "sl" else trade["tp"]
                    close_trade(state, exit_price, hit, ticker, timeframe, track)
                elif is_new_bar:
                    bars_key = f"{track}_bars_in_trade"
                    state[bars_key] = state.get(bars_key, 0) + 1
                    if state[bars_key] >= MAX_HOLD_BARS:
                        close_trade(state, last_close, "cancelled", ticker, timeframe, track)
                        logger.info(f"[TRADE] Force-closed {track.upper()}-track {trade['side'].upper()} after {MAX_HOLD_BARS} bars")

        atr14 = calculate_atr(df, ATR_PERIOD)
        atr_pct = (atr14 / df["close"]) * 100
        chop = calculate_chop(df, CHOP_LENGTH)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        mfi = calculate_mfi(df, MFI_LEN)
        level_os, level_ob = run_kmeans_mfi(mfi, MFI_TRAINING)
        and_osc, and_sig = calculate_andean(df, AND_LEN, AND_SIG_LEN)
        ut_buy, ut_sell = calculate_ut_bot(df, UT_SENSITIVITY, UT_PERIOD, use_ha=_cfg.UT_HEIKIN_ASHI)

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

        hh10_prev = float(df["high"].iloc[max(0, idx-10):idx].max())
        ll10_prev = float(df["low"].iloc[max(0, idx-10):idx].min())
        fake_break_long = float(df["high"].iloc[idx]) > hh10_prev and close_v < hh10_prev
        fake_break_short = float(df["low"].iloc[idx]) < ll10_prev and close_v > ll10_prev

        ll5_prev = float(df["low"].iloc[max(0, idx-5):idx].min())
        hh5_prev = float(df["high"].iloc[max(0, idx-5):idx].max())
        liq_sweep_long = float(df["low"].iloc[idx]) < ll5_prev and close_v > ll5_prev and close_v > open_v
        liq_sweep_short = float(df["high"].iloc[idx]) > hh5_prev and close_v < hh5_prev and close_v < open_v

        regime = "CHAOS" if atr_pct_v > ATR_MAX else "TREND" if atr_pct_v > ATR_MIN * 1.5 else "NORMAL"

        vol_info = volume_flow_signal_v3(df)
        vol_lev_reason = "no signal"

        warmed_up = len(df) >= MFI_TRAINING

        filter_long = (
            (not ENABLE_FRAMA_FILTER or frama_bull)
            and (not ENABLE_CHOP_FILTER  or chop_ok)
            and (not ENABLE_ATR_FILTER   or atr_ok)
            and slope_long
            and (not ENABLE_MTF_BIAS     or htf_bull)
            and not fake_break_long
            and not liq_sweep_short
        )
        filter_short = (
            (not ENABLE_FRAMA_FILTER or frama_bear)
            and (not ENABLE_CHOP_FILTER  or chop_ok)
            and (not ENABLE_ATR_FILTER   or atr_ok)
            and slope_short
            and (not ENABLE_MTF_BIAS     or htf_bear)
            and not fake_break_short
            and not liq_sweep_long
        )

        mfi_bull_sig = crossover(mfi, level_os, idx)
        mfi_bear_sig = crossunder(mfi, level_ob, idx)
        and_bull_sig = crossover2(and_osc, and_sig, idx)
        and_bear_sig = crossunder2(and_osc, and_sig, idx)

        bs_and_bull = bars_since_crossover2(and_osc, and_sig, idx, LOOKBACK)
        bs_mfi_bull = bars_since_crossover(mfi, level_os, idx, LOOKBACK)
        bs_and_bear = bars_since_crossunder2(and_osc, and_sig, idx, LOOKBACK)
        bs_mfi_bear = bars_since_crossunder(mfi, level_ob, idx, LOOKBACK)

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

        sig_a_long  = confirm_long_a  and filter_long  and not a_in_pos and a_long_cd_ok  and warmed_up
        sig_a_short = confirm_short_a and filter_short and not a_in_pos and a_short_cd_ok and warmed_up
        sig_u_long  = bool(ut_buy.iloc[idx])  and filter_long  and not u_in_pos and u_long_cd_ok  and is_new_bar
        sig_u_short = bool(ut_sell.iloc[idx]) and filter_short and not u_in_pos and u_short_cd_ok and is_new_bar

        # Флаги треков ставятся ТОЛЬКО при реальном открытии (не dry_run)
        # и ТОЛЬКО вместе с active_trade, чтобы не было рассинхрона
        if not dry_run:
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

        frama_sl_long = max(1.0, min(3.5, abs(close_v - float(fl.iloc[idx])) / atr_v))
        frama_sl_short = max(1.0, min(3.5, abs(float(fu.iloc[idx]) - close_v) / atr_v))
        sugg_sl = frama_sl_long if (sig_a_long or sig_u_long) else frama_sl_short
        sugg_lev = max(1, min(MAX_ALLOWED_LEV, int(TARGET_RISK_DEP / max(sugg_sl, 0.1))))

        if regime == "CHAOS":
            sugg_lev = max(1, int(sugg_lev * 0.5))
        if regime == "TREND":
            sugg_lev = min(MAX_ALLOWED_LEV, int(sugg_lev * 1.2))

        if (sig_a_long or sig_u_long):
            sugg_lev, vol_lev_reason = volume_leverage_adjustment_v3(
                vol_info, "long", sugg_lev
            )
        elif (sig_a_short or sig_u_short):
            sugg_lev, vol_lev_reason = volume_leverage_adjustment_v3(
                vol_info, "short", sugg_lev
            )

        oc_bias_long  = 0
        oc_bias_short = 0
        oc_lev_delta  = 0

        if onchain_bias and ONCHAIN_ENABLED:
            oc_bias_long      = onchain_bias.get("bias_long",      0)
            oc_bias_short     = onchain_bias.get("bias_short",     0)
            oc_lev_delta      = onchain_bias.get("lev_delta",      0)

        if (sig_a_long or sig_u_long) or (sig_a_short or sig_u_short):
            sugg_lev = max(1, min(MAX_ALLOWED_LEV, sugg_lev + oc_lev_delta))

        def calc_confidence(is_long: bool) -> int:
            score = 20 if chop_ok else 0
            score += 20 if atr_ok else 0
            score += 15 if (frama_bull if is_long else frama_bear) else 0
            a_sig = sig_a_long if is_long else sig_a_short
            u_sig = sig_u_long if is_long else sig_u_short
            score += 25 if (a_sig and u_sig) else 10 if (a_sig or u_sig) else 0
            score += 20 if (htf_bull if is_long else htf_bear) else 0
            score += oc_bias_long if is_long else oc_bias_short
            return max(0, min(100, score))

        signals = []
        MIN_RR = 1.0

        # --- A-track LONG ---
        if sig_a_long and not state.get("a_active_trade"):
            sl = calculate_sl(close_v, "long", fs, fu, fl, atr14, idx)
            tp, tp_desc = calculate_combined_tp(ticker, timeframe, "long", close_v, sl, df, idx, atr14, regime)
            risk = abs(close_v - sl)
            reward = abs(tp - close_v)
            rr = reward / max(risk, 1e-8)

            # 🆕 FIX: Применяем on-chain с safety check
            tp, sl, oc_desc, oc_ok = apply_onchain_with_safety(
                close_v, sl, tp, "long", onchain_bias, min_rr=MIN_RR
            )
            tp_desc += oc_desc
            risk = abs(close_v - sl)
            reward = abs(tp - close_v)
            rr = reward / max(risk, 1e-8)

            bar_low = float(df["low"].iloc[idx])
            if rr < MIN_RR:
                logger.info(f"[FILTER] {ticker} {timeframe} A-LONG skipped — R:R={rr:.2f} < {MIN_RR}")
                sig_a_long = False
            elif bar_low <= sl:
                logger.info(f"[FILTER] {ticker} {timeframe} A-LONG skipped — bar low below SL")
                sig_a_long = False
            else:
                state["a_active_trade"] = {
                    "side": "long",
                    "entry": close_v,
                    "sl": sl,
                    "tp": tp,
                    "lev": sugg_lev,
                    "bar_opened": bar_idx
                }
                state["a_bars_in_trade"] = 0
                if not dry_run:
                    add_signal_record(ticker, timeframe, "long", close_v, datetime.now(timezone.utc).isoformat(), regime)
                stats = get_signal_stats(ticker, timeframe, "long")
                signals.append(("A BUY  (Andean+MFI)", close_v, regime, sugg_lev, bar_time, calc_confidence(True), sl, tp, risk, stats, tp_desc))

        # --- U-track LONG ---
        if sig_u_long and not state.get("u_active_trade"):
            sl = calculate_sl(close_v, "long", fs, fu, fl, atr14, idx)
            tp, tp_desc = calculate_combined_tp(ticker, timeframe, "long", close_v, sl, df, idx, atr14, regime)
            risk = abs(close_v - sl)
            reward = abs(tp - close_v)
            rr = reward / max(risk, 1e-8)

            tp, sl, oc_desc, oc_ok = apply_onchain_with_safety(
                close_v, sl, tp, "long", onchain_bias, min_rr=MIN_RR
            )
            tp_desc += oc_desc
            risk = abs(close_v - sl)
            reward = abs(tp - close_v)
            rr = reward / max(risk, 1e-8)

            bar_low = float(df["low"].iloc[idx])
            if rr < MIN_RR:
                logger.info(f"[FILTER] {ticker} {timeframe} U-LONG skipped — R:R={rr:.2f} < {MIN_RR}")
                sig_u_long = False
            elif bar_low <= sl:
                logger.info(f"[FILTER] {ticker} {timeframe} U-LONG skipped — bar low below SL")
                sig_u_long = False
            else:
                state["u_active_trade"] = {
                    "side": "long",
                    "entry": close_v,
                    "sl": sl,
                    "tp": tp,
                    "lev": sugg_lev,
                    "bar_opened": bar_idx
                }
                state["u_bars_in_trade"] = 0
                if not dry_run:
                    add_signal_record(ticker, timeframe, "long", close_v, datetime.now(timezone.utc).isoformat(), regime)
                stats = get_signal_stats(ticker, timeframe, "long")
                signals.append(("U BUY  (UT Bot)", close_v, regime, sugg_lev, bar_time, calc_confidence(True), sl, tp, risk, stats, tp_desc))

        # --- A-track SHORT ---
        if sig_a_short and not state.get("a_active_trade"):
            sl = calculate_sl(close_v, "short", fs, fu, fl, atr14, idx)
            tp, tp_desc = calculate_combined_tp(ticker, timeframe, "short", close_v, sl, df, idx, atr14, regime)
            risk = abs(sl - close_v)
            reward = abs(close_v - tp)
            rr = reward / max(risk, 1e-8)

            tp, sl, oc_desc, oc_ok = apply_onchain_with_safety(
                close_v, sl, tp, "short", onchain_bias, min_rr=MIN_RR
            )
            tp_desc += oc_desc
            risk = abs(sl - close_v)
            reward = abs(close_v - tp)
            rr = reward / max(risk, 1e-8)

            bar_high = float(df["high"].iloc[idx])
            if rr < MIN_RR:
                logger.info(f"[FILTER] {ticker} {timeframe} A-SHORT skipped — R:R={rr:.2f} < {MIN_RR}")
                sig_a_short = False
            elif bar_high >= sl:
                logger.info(f"[FILTER] {ticker} {timeframe} A-SHORT skipped — bar high above SL")
                sig_a_short = False
            else:
                state["a_active_trade"] = {
                    "side": "short",
                    "entry": close_v,
                    "sl": sl,
                    "tp": tp,
                    "lev": sugg_lev,
                    "bar_opened": bar_idx
                }
                state["a_bars_in_trade"] = 0
                if not dry_run:
                    add_signal_record(ticker, timeframe, "short", close_v, datetime.now(timezone.utc).isoformat(), regime)
                stats = get_signal_stats(ticker, timeframe, "short")
                signals.append(("A SELL (Andean+MFI)", close_v, regime, sugg_lev, bar_time, calc_confidence(False), sl, tp, risk, stats, tp_desc))

        # --- U-track SHORT ---
        if sig_u_short and not state.get("u_active_trade"):
            sl = calculate_sl(close_v, "short", fs, fu, fl, atr14, idx)
            tp, tp_desc = calculate_combined_tp(ticker, timeframe, "short", close_v, sl, df, idx, atr14, regime)
            risk = abs(sl - close_v)
            reward = abs(close_v - tp)
            rr = reward / max(risk, 1e-8)

            tp, sl, oc_desc, oc_ok = apply_onchain_with_safety(
                close_v, sl, tp, "short", onchain_bias, min_rr=MIN_RR
            )
            tp_desc += oc_desc
            risk = abs(sl - close_v)
            reward = abs(close_v - tp)
            rr = reward / max(risk, 1e-8)

            bar_high = float(df["high"].iloc[idx])
            if rr < MIN_RR:
                logger.info(f"[FILTER] {ticker} {timeframe} U-SHORT skipped — R:R={rr:.2f} < {MIN_RR}")
                sig_u_short = False
            elif bar_high >= sl:
                logger.info(f"[FILTER] {ticker} {timeframe} U-SHORT skipped — bar high above SL")
                sig_u_short = False
            else:
                state["u_active_trade"] = {
                    "side": "short",
                    "entry": close_v,
                    "sl": sl,
                    "tp": tp,
                    "lev": sugg_lev,
                    "bar_opened": bar_idx
                }
                state["u_bars_in_trade"] = 0
                if not dry_run:
                    add_signal_record(ticker, timeframe, "short", close_v, datetime.now(timezone.utc).isoformat(), regime)
                stats = get_signal_stats(ticker, timeframe, "short")
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
        level_os, level_ob = 20.0, 80.0
        and_osc, and_sig = calculate_andean(df, AND_LEN, AND_SIG_LEN)
        ut_buy, ut_sell = calculate_ut_bot(df, UT_SENSITIVITY, UT_PERIOD, use_ha=_cfg.UT_HEIKIN_ASHI)

        htf = _cfg.HTF_BIAS
        htf_bias_arr = np.zeros(len(df))

        try:
            htf_bars = exchange.fetch_ohlcv(ticker, htf, limit=num_bars)
            if htf_bars and len(htf_bars) >= 100:
                df_htf = parse_ohlcv(htf_bars)
                if validate_dataframe(df_htf, 50):
                    fs_htf, fu_htf, fl_htf, fdir_htf = calculate_frama(df_htf, FRAMA_LEN, FRAMA_MULT)
                    ltf_times = df["timestamp"].values
                    htf_times = df_htf["timestamp"].values
                    htf_idx = 0
                    for i in range(len(df)):
                        if htf_times[0] > ltf_times[i]:
                            htf_bias_arr[i] = 0
                            continue
                        while htf_idx + 1 < len(htf_times) and htf_times[htf_idx + 1] <= ltf_times[i]:
                            htf_idx += 1
                        if htf_idx >= FRAMA_LEN * 2:
                            htf_close = float(df_htf["close"].iloc[htf_idx])
                            htf_frama_val = float(fs_htf.iloc[htf_idx])
                            htf_bias_arr[i] = 1 if htf_close > htf_frama_val else -1
        except Exception as e:
            logger.warning(f"[BACKTEST] HTF bias fetch failed for {ticker} {tf}: {e}")

        signals_found = 0
        history = load_signals_history()

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

            hh10_prev = float(df["high"].iloc[max(0, idx-10):idx].max())
            ll10_prev = float(df["low"].iloc[max(0, idx-10):idx].min())
            fake_break_long = float(df["high"].iloc[idx]) > hh10_prev and close_v < hh10_prev
            fake_break_short = float(df["low"].iloc[idx]) < ll10_prev and close_v > ll10_prev

            ll5_prev = float(df["low"].iloc[max(0, idx-5):idx].min())
            hh5_prev = float(df["high"].iloc[max(0, idx-5):idx].max())
            liq_sweep_long = float(df["low"].iloc[idx]) < ll5_prev and close_v > ll5_prev and close_v > open_v
            liq_sweep_short = float(df["high"].iloc[idx]) > hh5_prev and close_v < hh5_prev and close_v < open_v

            bt_regime = "CHAOS" if atr_pct_v > ATR_MAX else "TREND" if atr_pct_v > ATR_MIN * 1.5 else "NORMAL"
            warmed_up_bt = idx >= MFI_TRAINING

            filter_long = (
                (not ENABLE_FRAMA_FILTER or frama_bull)
                and (not ENABLE_CHOP_FILTER  or chop_ok)
                and (not ENABLE_ATR_FILTER   or atr_ok)
                and slope_long
                and not fake_break_long
                and not liq_sweep_short
            )
            filter_short = (
                (not ENABLE_FRAMA_FILTER or frama_bear)
                and (not ENABLE_CHOP_FILTER  or chop_ok)
                and (not ENABLE_ATR_FILTER   or atr_ok)
                and slope_short
                and not fake_break_short
                and not liq_sweep_long
            )

            mfi_bull_sig = crossover(mfi, level_os, idx)
            mfi_bear_sig = crossunder(mfi, level_ob, idx)
            and_bull_sig = crossover2(and_osc, and_sig, idx)
            and_bear_sig = crossunder2(and_osc, and_sig, idx)

            bs_and_bull = bars_since_crossover2(and_osc, and_sig, idx, LOOKBACK)
            bs_mfi_bull = bars_since_crossover(mfi, level_os, idx, LOOKBACK)
            bs_and_bear = bars_since_crossunder2(and_osc, and_sig, idx, LOOKBACK)
            bs_mfi_bear = bars_since_crossunder(mfi, level_ob, idx, LOOKBACK)

            confirm_long_a = (mfi_bull_sig and bs_and_bull <= LOOKBACK) or (and_bull_sig and bs_mfi_bull <= LOOKBACK)
            confirm_short_a = (mfi_bear_sig and bs_and_bear <= LOOKBACK) or (and_bear_sig and bs_mfi_bear <= LOOKBACK)

            htf_bull_bt = htf_bias_arr[idx] == 1
            htf_bear_bt = htf_bias_arr[idx] == -1

            sig_a_long  = confirm_long_a  and filter_long  and warmed_up_bt and (not ENABLE_MTF_BIAS or htf_bull_bt)
            sig_a_short = confirm_short_a and filter_short and warmed_up_bt and (not ENABLE_MTF_BIAS or htf_bear_bt)
            sig_u_long  = bool(ut_buy.iloc[idx])  and filter_long
            sig_u_short = bool(ut_sell.iloc[idx]) and filter_short

            for track, side, sig_ok in [
                ("a", "long", sig_a_long), ("a", "short", sig_a_short),
                ("u", "long", sig_u_long), ("u", "short", sig_u_short)
            ]:
                if not sig_ok:
                    continue

                sl = calculate_sl(close_v, side, fs, fu, fl, atr14, idx)
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

                if not tp_hit and not sl_hit and bars_held > 0:
                    exit_price = float(df["close"].iloc[min(idx + bars_held, len(df) - 1)])

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
                    "track": track,
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
    """Создает начальное состояние с независимыми A и U треками."""
    return {
        "a_in_long": False,
        "a_in_short": False,
        "a_long_bar": None,
        "a_short_bar": None,
        "last_a_long_bar": None,
        "last_a_short_bar": None,
        "u_in_long": False,
        "u_in_short": False,
        "u_long_bar": None,
        "u_short_bar": None,
        "last_u_long_bar": None,
        "last_u_short_bar": None,
        "last_bar_time": None,
        "last_processed_bar_time": None,
        "a_active_trade": None,
        "u_active_trade": None,
        "a_trade_history": [],
        "u_trade_history": [],
        "a_bars_in_trade": 0,
        "u_bars_in_trade": 0,
        "a_last_closure_notified": False,
        "u_last_closure_notified": False,
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
