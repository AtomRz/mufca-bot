"""
MUFCA v4.0 — Chart Data (JSON)
The same thing chart.py computes for the PNG (Discord's !chart), but without
matplotlib — returns JSON for the web dashboard. Uses the SAME indicator
functions as chart.py/indicators.py, so the numbers on the site match the
Discord image 1-to-1.
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
from chart import calc_bollinger_bands, calc_support_resistance, calc_volume_profile
# 🆕 Signal/filter lamps in the top bar: reuses the exact same crossover/
# bars_since helpers and get_htf_bias that signals.check_signals uses, so the
# UI lamps match EXACTLY the logic that actually decides whether a trade
# opens or not (no separate "own" copy of the conditions).
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
    calculate_adaptive_sl,
    apply_onchain_with_safety,
)
from state import calculate_combined_tp
import config

logger = logging.getLogger(__name__)


def _series_to_list(s: pd.Series, limit: int) -> List[Optional[float]]:
    """pandas Series -> list of floats, NaN -> None (JSON has no NaN)."""
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
    Equivalent to chart.generate_chart(), but returns JSON instead of a PNG.

    Returns candles + all overlays (FRAMA channel, BB, S/R, MFI + kmeans
    thresholds) from exactly one OHLCV fetch — no duplicate recomputation.
    """
    # 🆕 FIX: fetch_limit used to be max(limit+250, 300) — the S/R pivot
    # search window scaled together with the UI's selected barsLimit (100/
    # 200/300/500 bars). calc_support_resistance() looks for local extremes
    # across the WHOLE fetched df, not just the displayed tail — meaning at
    # a small barsLimit, a level formed deeper in history was physically
    # unreachable in the candidate sample, not just "not shown". Now the S/R
    # search window doesn't depend on how many bars the user chose to
    # display — FRAMA/MFI/BB don't change with a deeper fetch (causal
    # indicators, the tail already converges even from an older starting
    # point), only the stability of the found levels changes.
    fetch_limit = max(limit + 250, 300, config.SR_MIN_LOOKBACK)
    bars = await safe_fetch_ohlcv(exchange, symbol, timeframe, limit=fetch_limit)
    df = parse_ohlcv(bars)

    if not validate_dataframe(df, min_rows=50):
        raise ValueError(f"Not enough data for {symbol} {timeframe}")

    # ── Indicators — the same calls as in chart.generate_chart() ────────
    frama_s, frama_u, frama_l, _ = calculate_frama(
        df, length=config.FRAMA_LEN, mult=config.FRAMA_MULT
    )
    mfi_s = calculate_mfi(df, length=config.MFI_LEN)
    mfi_os, mfi_ob = run_kmeans_mfi(mfi_s, training_size=config.MFI_TRAINING)

    bb_u, bb_m, bb_l = calc_bollinger_bands(df["close"], period=config.BB_PERIOD, std_mult=config.BB_STDDEV)
    sr = calc_support_resistance(df, pivot_window=config.SR_PIVOT_WINDOW, max_levels=config.SR_MAX_LEVELS)

    # 🆕 Volume Profile (POC / Value Area) — see chart.calc_volume_profile()
    # for the TPO-style approximation used since only OHLCV is available.
    vp = None
    if config.VP_ENABLED:
        vp_window = df.tail(min(config.VP_LOOKBACK, len(df)))
        vp = calc_volume_profile(vp_window, bins=config.VP_BINS, value_area_pct=config.VP_VALUE_AREA_PCT)
        if not config.VP_SHOW_HISTOGRAM:
            vp["bins"] = []
        elif vp["bins"]:
            # Same 6%-of-max threshold as the Discord PNG chart (chart.py) —
            # below that, bars are visually indistinguishable from noise and
            # just clutter a densely-packed histogram strip with a "staircase"
            # of thin low-volume bars near the edges of the price range.
            max_vol = max(b["volume"] for b in vp["bins"])
            if max_vol > 0:
                vp["bins"] = [b for b in vp["bins"] if b["volume"] / max_vol >= 0.06]

    df_tail = df.tail(limit).reset_index(drop=True)

    candles = [
        {
            "time": int(row.timestamp // 1000),  # unix seconds — for lightweight-charts
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
        "volume_profile": vp,
    }

    # ── Active trade (if state_snapshot was passed from !status/state) ──
    if state_snapshot:
        # 🆕 FIX: the key in the trade dict is called bar_opened_time (see
        # signals.py), not entry_time_ms — because of this, signal_bar_time
        # was always None and the signal marker on the chart could never render.
        bar_opened_time_ms = state_snapshot.get("bar_opened_time")
        signal_bar_time = None
        if bar_opened_time_ms is not None:
            try:
                ts_arr = df["timestamp"].values
                closest_i = int(np.argmin(np.abs(ts_arr - float(bar_opened_time_ms))))
                # 🆕 FIX: if the trade opened earlier than the chart's
                # visible range (e.g. limit=150 bars, but the entry was 300
                # bars ago), argmin will still find the "closest" bar —
                # usually the chart's very first one — drawing the marker in
                # a definitely wrong spot. Check the real time proximity
                # (within 1.5 bar intervals); if the trade is genuinely
                # outside the visible window, don't draw a marker at all,
                # instead of lying about its location.
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


async def get_market_pulse(exchange, ticker: str, tf: str, onchain_bias: Optional[Dict] = None) -> Dict:
    """
    Lightweight summary for the dashboard's top bar: current CHOP, FRAMA
    direction (trend), and a rough informational leverage estimate — the
    same formula as in signals.py's check_signals (frama_sl_long/short +
    TARGET_RISK_DEP), but this is ONLY an indicative number for the UI, it
    doesn't determine actual sizing (see the comment in signals.py next to
    sugg_lev).
    """
    # 🆕 limit=900 (not 300) — same as signals.check_signals: run_kmeans_mfi
    # trains on MFI_TRAINING (800 by default) bars, and the overbought/
    # oversold levels on a shorter history would come out different — the
    # lamps need to match the real bot, not just "look similar".
    bars = await safe_fetch_ohlcv(exchange, ticker, tf, limit=900)
    df = parse_ohlcv(bars)

    if not validate_dataframe(df, min_rows=50):
        raise ValueError(f"Not enough data for {ticker} {tf}")

    # idx as a positive integer index (not -2!) — the bars_since_* helpers
    # from signals.py do range(idx, idx-lookback-1, -1); with a negative idx
    # that would be an empty/broken sequence.
    idx = len(df) - 2  # the last confirmed bar — same as everywhere else in signals
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
    # 🆕 SIGNAL AND FILTER LAMPS (for the top bar)
    # Logic 1-to-1 from signals.check_signals — just without reading/writing
    # trading state (positions/cooldowns aren't factored in here, this is
    # only an indication of "what the indicators and filters are currently
    # saying", not "will a trade actually open").
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

    # MFI/Andean lamps "hold" while bars_since <= LOOKBACK — same as
    # confirm_long_a/confirm_short_a itself, not just on the crossover bar.
    mfi_lamp = _lamp_state(bs_mfi_bull <= config.LOOKBACK, bs_mfi_bear <= config.LOOKBACK)
    and_lamp = _lamp_state(bs_and_bull <= config.LOOKBACK, bs_and_bear <= config.LOOKBACK)

    # UT Bot genuinely only triggers on its own bar (no "window" in the
    # actual trading logic) — the lamp honestly goes out on the very next bar.
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

    # 🆕 R:R LAMP — often the most invisible "silent" rejection: MFI/Andean
    # give confluence, every filter is green, but open_position() still
    # silently rejects the signal because reward/risk < MIN_RR (see [FILTER]
    # skipped in the logs). We compute R:R with the SAME functions and in the
    # same order as open_position() (calculate_adaptive_sl →
    # calculate_combined_tp → apply_onchain_with_safety), separately for long
    # and short — regardless of whether there's a real confluence signal
    # right now, so it's visible in advance, not only at the moment of rejection.
    atr_regime_pct = atr_pct_v
    regime = "CHAOS" if atr_regime_pct > config.ATR_MAX else "TREND" if atr_regime_pct > config.ATR_MIN * 1.5 else "NORMAL"

    def _calc_rr(side: str) -> float:
        sl, _ = calculate_adaptive_sl(close_v, side, ticker, tf, frama_s, frama_u, frama_l, atr_s, idx)
        tp1_raw, tp2_raw, _ = calculate_combined_tp(ticker, tf, side, close_v, sl, df, idx, atr_s, regime)
        tp_final, sl_final, _, _ = apply_onchain_with_safety(
            close_v, sl, tp2_raw, side, onchain_bias, min_rr=config.MIN_RR
        )
        if side == "long":
            risk = abs(close_v - sl_final)
            reward = abs(tp_final - close_v)
        else:
            risk = abs(sl_final - close_v)
            reward = abs(close_v - tp_final)
        return reward / max(risk, 1e-8)

    try:
        rr_long = round(_calc_rr("long"), 2)
        rr_short = round(_calc_rr("short"), 2)
    except Exception as e:
        logger.warning(f"[PULSE] {ticker} {tf} R:R calc failed: {e}")
        rr_long = rr_short = None

    # pass_long/pass_short mirror EXACTLY the filter_long/filter_short
    # formulas from signals.py — including the fact that liq_sweep_short
    # blocks LONG there, and liq_sweep_long blocks SHORT (that's how the
    # real trading logic works, not a QA typo — the lamp has to show it as-is).
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
                "enabled": config.ENABLE_FAKE_BREAK_FILTER,
                "pass_long": not fake_break_long,
                "pass_short": not fake_break_short,
            },
            "liq_sweep": {
                "enabled": config.ENABLE_LIQ_SWEEP_FILTER,
                "pass_long": not liq_sweep_short,
                "pass_short": not liq_sweep_long,
            },
            "rr": {
                "enabled": True,  # this is a hard gate in open_position(), not toggleable by a flag
                "pass_long": rr_long is not None and rr_long >= config.MIN_RR,
                "pass_short": rr_short is not None and rr_short >= config.MIN_RR,
                "value_long": rr_long,
                "value_short": rr_short,
                "threshold": config.MIN_RR,
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
