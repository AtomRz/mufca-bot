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
    COOLDOWN_BARS,
    MAX_ALLOWED_LEV,
    TARGET_RISK_DEP,
    MAX_HOLD_BARS,
    HTF_CACHE_TTL_SECONDS,
    MARKET_MODE,
)
# SIGNAL_HISTORY_LIMIT deliberately NOT bare-imported: it's runtime-mutable via
# !tpconfig limit / web_api.py (_cfg.SIGNAL_HISTORY_LIMIT = ...). A bare import
# would bind a stale int copy here that never sees those updates. Same reasoning
# as the indicator params below — always read via _cfg.SIGNAL_HISTORY_LIMIT.
# 🆕 Indicator parameters (FRAMA/MFI/Andean/UT Bot) are now editable live from
# the web UI (Settings), so we access them as _cfg.FRAMA_LEN etc. at the call
# site instead of a bare import — otherwise this name's copy would go stale
# after `_cfg.FRAMA_LEN = ...` in web_api.py.
from indicators import (
    calculate_atr,
    calculate_chop,
    calculate_frama,
    calculate_mfi,
    calculate_andean,
    calculate_ut_bot,
    run_kmeans_mfi,
    run_kmeans_mfi_rolling,
    heikin_ashi,
    calculate_hurst,
    calculate_breakout_signal,
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
    normalize_timestamp,
)
from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe, Timer, round_price
from config import ONCHAIN_ENABLED
import spread

logger = logging.getLogger(__name__)

# =====================================================================
# 🔄  CROSSOVER HELPERS (module level for reuse)
# =====================================================================

def _lvl_at(lvl, k):
    """Reads a threshold value at index k — works whether `lvl` is a plain
    scalar (the live path's single current-bar level) or a pandas Series
    (the backtest path's point-in-time-correct rolling level, one value per
    bar — see run_kmeans_mfi_rolling)."""
    return float(lvl.iloc[k]) if hasattr(lvl, "iloc") else lvl


def crossover(s, lvl, i):
    if i < 1:
        return False
    return float(s.iloc[i]) > _lvl_at(lvl, i) and float(s.iloc[i-1]) <= _lvl_at(lvl, i-1)

def crossunder(s, lvl, i):
    if i < 1:
        return False
    return float(s.iloc[i]) < _lvl_at(lvl, i) and float(s.iloc[i-1]) >= _lvl_at(lvl, i-1)

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
        if float(s.iloc[k]) > _lvl_at(lvl, k) and float(s.iloc[k-1]) <= _lvl_at(lvl, k-1):
            return cur - k
    return 999

def bars_since_crossunder(s, lvl, cur, lookback):
    for k in range(cur, max(cur - lookback - 1, 1), -1):
        if float(s.iloc[k]) < _lvl_at(lvl, k) and float(s.iloc[k-1]) >= _lvl_at(lvl, k-1):
            return cur - k
    return 999

# =====================================================================
# 🧬  HTF BIAS (WITH CACHING)
# =====================================================================

_htf_cache = Timer(HTF_CACHE_TTL_SECONDS)

def clear_htf_cache():
    """Resets the HTF bias cache — call when !htf changes."""
    _htf_cache.clear()

async def get_htf_bias(exchange: ccxt.Exchange, ticker: str, timeframe: str) -> int:
    """Returns the HTF bias, with caching.

    🆕 FIX (Kimi review #9): the cache key used to be f"{ticker}_{timeframe}",
    where timeframe is the trading TF (1h/4h/...), even though HTF bias only
    depends on ticker and _cfg.HTF_BIAS (usually 1d) — the timeframe
    parameter never actually factored into the calculation. With multiple
    trading TFs on the same ticker (the normal TICKERS × TIMEFRAMES
    configuration), this produced N identical cache entries and N redundant
    fetches of the same HTF timeframe instead of one.
    """
    cache_key = f"{ticker}_{_cfg.HTF_BIAS}"
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

        fs, fu, fl, fdir = calculate_frama(df_htf, _cfg.FRAMA_LEN, _cfg.FRAMA_MULT)
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
    """Calculates Stop Loss with validation (SL never crosses the entry price).

    BUGFIX BUG-CR002: FRAMA returns NaN for the first ~22 bars (rolling
    windows not yet filled). max(NaN, x) = NaN in Python (IEEE 754), so SL
    became NaN, the rr < MIN_RR and bar_low <= sl checks both evaluated to
    False, and the position opened with no working stop and never closed on SL.
    Fix: if sl_frama = NaN, use sl_atr alone.
    """
    atr_v = max(float(atr14.iloc[idx]), 1e-8)
    if side == "long":
        sl_frama = float(fl.iloc[idx])
        sl_atr = entry_price - 1.5 * atr_v
        # NaN guard: if FRAMA hasn't warmed up — fall back to the ATR stop
        if np.isnan(sl_frama):
            sl = sl_atr
        else:
            sl = max(sl_frama, sl_atr)
        return min(sl, entry_price * 0.995)
    else:
        sl_frama = float(fu.iloc[idx])
        sl_atr = entry_price + 1.5 * atr_v
        # NaN guard
        if np.isnan(sl_frama):
            sl = sl_atr
        else:
            sl = min(sl_frama, sl_atr)
        return max(sl, entry_price * 1.005)

# =====================================================================
# 🛑  ADAPTIVE SL (based on historical MAE)
# =====================================================================

def calculate_adaptive_sl(
    entry_price: float,
    side: str,
    ticker: str,
    timeframe: str,
    fs: pd.Series,
    fu: pd.Series,
    fl: pd.Series,
    atr14: pd.Series,
    idx: int,
    as_of: Optional[str] = None,
) -> tuple[float, str]:
    """
    Adaptive SL based on historical MAE of winning trades.

    Logic:
    - Only use trades where price came back (exit_type == "tp" or "cancelled") —
      true MAE: price moved against us but came back without touching the stop.
    - Take the SL_MAE_PERCENTILE percentile of these MAE values + an SL_MAE_BUFFER buffer.
    - If there are fewer winning trades than SL_MIN_HISTORY — fall back to a fixed % or ATR-based SL.

    as_of — ISO timestamp string; same look-ahead-prevention purpose as
    calculate_adaptive_tp's as_of (see state.get_signal_stats' docstring) —
    this pulls its own records independently rather than through
    get_signal_stats/calculate_adaptive_tp, so it needed the same fix
    applied separately. None (the default) for live callers.

    Returns:
        (sl_price, description)
    """
    atr_sl = calculate_sl(entry_price, side, fs, fu, fl, atr14, idx)

    if not _cfg.SL_ADAPTIVE_ENABLED:
        return atr_sl, "frama/atr"

    try:
        history = load_signals_history()
        records = history.get(ticker, {}).get(timeframe, {}).get(side, [])

        # Winning trades only (price came back without touching the stop)
        # 🆕 FIX: exclude synthetic records (!sim) — they don't reflect real MAE.
        winning = [
            r for r in records
            if r.get("exit_type") in ("tp", "cancelled")
            and r.get("max_adverse_pct", 0) > 0
            and not r.get("synthetic", False)
        ]
        if as_of is not None:
            winning = [r for r in winning if r.get("timestamp", "") < as_of]

        if len(winning) < _cfg.SL_MIN_HISTORY:
            # 🆕 FIX CRITICAL BUG: this used to take the "opposite" FRAMA line —
            # fu (upper) for LONG and fl (lower) for SHORT — trying to get a
            # "wide" stop. In practice this put the SL on the WRONG side of
            # entry: for a long, fu is almost always ABOVE the entry price,
            # meaning SL ended up above entry and didn't protect the position
            # (it would trigger as an "SL" while price was rising, with a
            # positive PnL — exactly what was showing up in !signals when
            # history was thin, < SL_MIN_HISTORY=10 winning trades).
            # atr_sl is already computed above via calculate_sl() — guaranteed
            # to be on the correct side (fl for long, fu for short), plus NaN
            # protection and a clamp so SL never crosses the entry price.
            # Just reuse it.
            sl = atr_sl
            logger.debug(
                f"[ADAPTIVE_SL] {ticker} {timeframe} {side}: "
                f"only {len(winning)} winning trades < {_cfg.SL_MIN_HISTORY} min, "
                f"fallback ATR/FRAMA-SL -> SL={sl:.4f}"
            )
            return sl, f"frama/atr-fallback ({len(winning)}/{_cfg.SL_MIN_HISTORY} wins)"

        mae_values = [r["max_adverse_pct"] / 100 for r in winning]  # convert % → fraction
        mae_percentile = float(np.percentile(mae_values, _cfg.SL_MAE_PERCENTILE * 100))
        mae_with_buffer = mae_percentile + _cfg.SL_MAE_BUFFER

        # 🆕 FIX: TP has an ATR cap (calculate_adaptive_tp), SL didn't have one —
        # an unbounded MAE percentile could give an unreasonably wide stop on
        # outliers in the history. Cap it at SL_MAX_ATR_MULT × ATR.
        atr_v = max(float(atr14.iloc[idx]), 1e-8)
        max_risk_pct = (_cfg.SL_MAX_ATR_MULT * atr_v) / entry_price
        capped = mae_with_buffer > max_risk_pct
        if capped:
            mae_with_buffer = max_risk_pct

        if side == "long":
            sl_adaptive = entry_price * (1 - mae_with_buffer)
            # Never put SL above the entry price
            sl = min(sl_adaptive, entry_price * (1 - _cfg.SL_MIN_DISTANCE_PCT))
        else:
            sl_adaptive = entry_price * (1 + mae_with_buffer)
            # Never put SL below the entry price
            sl = max(sl_adaptive, entry_price * (1 + _cfg.SL_MIN_DISTANCE_PCT))

        desc = (
            f"adaptive MAE p{_cfg.SL_MAE_PERCENTILE*100:.0f} "
            f"{mae_percentile*100:.2f}%+{_cfg.SL_MAE_BUFFER*100:.1f}%"
            f"{' [ATR-capped]' if capped else ''} "
            f"({len(winning)} wins)"
        )
        logger.debug(f"[ADAPTIVE_SL] {ticker} {timeframe} {side}: {desc} → SL={sl:.4f}")
        return sl, desc

    except Exception as e:
        logger.warning(f"[ADAPTIVE_SL] Error, falling back to ATR-SL: {e}")
        return atr_sl, "frama/atr (error)"

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
    max_sl_widen_pct: float = 0.15,  # Maximum SL widening of 15%
) -> Tuple[float, float, str, bool]:
    """
    Applies on-chain multipliers to TP/SL with a safety check.

    Returns: (new_tp, new_sl, desc_suffix, applied_ok)
    applied_ok = False if on-chain degraded R:R below min_rr and was rejected
    """
    if not onchain_bias or not ONCHAIN_ENABLED:
        return tp, sl, "", True

    if side == "long":
        tp_mult = onchain_bias.get("tp_mult_long", 1.0)
        sl_mult = onchain_bias.get("sl_mult_long", 1.0)
    else:
        tp_mult = onchain_bias.get("tp_mult_short", 1.0)
        sl_mult = onchain_bias.get("sl_mult_short", 1.0)

    if abs(tp_mult - 1.0) < 1e-9 and abs(sl_mult - 1.0) < 1e-9:
        return tp, sl, "", True

    risk_before = abs(entry - sl)
    reward_before = abs(tp - entry)
    rr_before = reward_before / max(risk_before, 1e-8)

    # Apply the multipliers
    if side == "long":
        new_tp = entry + reward_before * tp_mult
        # SL mult > 1 = SL farther (wider), < 1 = SL closer
        new_sl = entry - risk_before * sl_mult
    else:
        new_tp = entry - reward_before * tp_mult
        new_sl = entry + risk_before * sl_mult

    desc_suffix = ""

    # 🆕 FIX: SL-widen cap moved BEFORE the RR check (was after, with an early
    # `return` in the RR branch — if on-chain widened SL enough to also drop RR
    # below min_rr, the function returned from the RR branch first and the SL
    # cap below was never reached, silently letting SL through wider than
    # max_sl_widen_pct). Capping first means the RR check afterwards always
    # operates on the already-capped risk, so neither guarantee can undercut
    # the other regardless of which condition triggers.
    risk_after = abs(entry - new_sl)
    sl_widen = (risk_after - risk_before) / risk_before
    if sl_widen > max_sl_widen_pct:
        logger.warning(f"[ONCHAIN-SAFETY] SL widened {sl_widen:.1%} > max {max_sl_widen_pct:.1%}, capping")
        if side == "long":
            new_sl = entry - risk_before * (1 + max_sl_widen_pct)
        else:
            new_sl = entry + risk_before * (1 + max_sl_widen_pct)
        risk_after = abs(entry - new_sl)
        desc_suffix = f" | OC×TP{tp_mult}/SL{sl_mult} [SL capped +{max_sl_widen_pct:.0%}]"

    reward_after = abs(new_tp - entry)
    rr_after = reward_after / max(risk_after, 1e-8)

    # If RR is still below the minimum (due to the on-chain TP multiplier or
    # the SL cap above) — roll TP back to the minimum allowed, preserving the
    # on-chain direction where possible.
    if rr_after < min_rr:
        logger.warning(f"[ONCHAIN-SAFETY] RR degraded {rr_before:.2f} -> {rr_after:.2f} (below {min_rr}), using conservative")
        if side == "long":
            # Minimum TP = entry + risk_after * min_rr, but never worse than the original TP
            safe_tp = entry + risk_after * min_rr
            new_tp = max(safe_tp, tp)
        else:
            safe_tp = entry - risk_after * min_rr
            new_tp = min(safe_tp, tp)

        reward_after = abs(new_tp - entry)
        rr_after = reward_after / max(risk_after, 1e-8)
        desc_suffix += f" | OC×TP{tp_mult}/SL{sl_mult} [SAFETY: RR capped @ {rr_after:.2f}]"
        return new_tp, new_sl, desc_suffix, False

    if not desc_suffix:
        desc_suffix = f" | OC×TP{tp_mult}/SL{sl_mult} [RR {rr_before:.2f}→{rr_after:.2f}]"
    return new_tp, new_sl, desc_suffix, True

# =====================================================================
# 📊  TP/SL CHECK
# =====================================================================

def check_tp_sl_hit(state: Dict, high: float, low: float, track: str = "a",
                     bar_time: Optional[int] = None) -> Optional[str]:
    """Checks whether TP or SL was hit for the given track.

    🆕 A single bar's high/low can't tell you which was actually touched
    first intrabar if both SL and TP fall within its range — see
    config.SAME_BAR_EXIT_POLICY ("sl_first", the only option implemented):
    SL is checked before TP below, consistently with backtest_history()'s
    equivalent block, so live and backtest agree on the same conservative
    resolution of that ambiguity.

    🆕 FIX BUG-LO009 (found by Kimi audit): the SL moved after TP1 used to be
    checked against ANY bar, including ones that closed BEFORE the move
    happened — their low/high were printed before price ever even touched
    TP1, and are almost guaranteed to be below (for long) the new
    halfway/breakeven level. This produced a false "sl" on the very first
    scan after a TP1 hit, at a price the market never actually touched after
    the move. bar_time + trade["sl_moved_after_bar"] (set in bot.py at the
    moment of the move) excludes such bars."""
    trade = state.get(f"{track}_active_trade")
    if not trade:
        return None

    side = trade["side"]
    sl = trade["sl"]
    tp = trade["tp"]

    sl_valid_from = trade.get("sl_moved_after_bar")
    sl_applicable = sl_valid_from is None or (bar_time is not None and bar_time > sl_valid_from)

    if side == "long":
        if sl_applicable and low <= sl:
            return "sl"
        if high >= tp:
            return "tp"
    else:
        if sl_applicable and high >= sl:
            return "sl"
        if low <= tp:
            return "tp"
    return None

def close_trade(state: Dict, exit_price: float, result: str, ticker: str, tf: str, track: str = "a") -> Optional[Dict]:
    """Closes the active position for the given track (a or u)."""
    trade_key = f"{track}_active_trade"
    trade = state.get(trade_key)
    if not trade:
        return None

    entry = trade["entry"]
    side = trade["side"]
    bars_key = f"{track}_bars_in_trade"
    bars_held = state.get(bars_key, 0)

    tp1_hit = bool(trade.get("tp1_hit"))
    tp1_price = trade.get("tp1")

    # 🆕 FIX: if TP1 was already hit (50% closed in practice manually on the
    # exchange, SL on the remaining 50% moved to breakeven/half_tp1), the
    # real PnL is the average between the half locked in at TP1 and the
    # result of the second half, not a naive entry→exit_price over the whole
    # position (see the detailed comment in state.update_signal_record).
    if tp1_hit and tp1_price is not None:
        tp1_leg_pct = (tp1_price - entry) / entry * 100 if side == "long" else (entry - tp1_price) / entry * 100
        remainder_leg_pct = (exit_price - entry) / entry * 100 if side == "long" else (entry - exit_price) / entry * 100
        pnl_pct = (tp1_leg_pct + remainder_leg_pct) / 2
    else:
        pnl_pct = (exit_price - entry) / entry * 100 if side == "long" else (entry - exit_price) / entry * 100

    # 🆕 FIX BUG-LO008: a trade where TP1 already gave profit and the
    # remainder closed at the moved SL (breakeven/half_tp1) used to be
    # labeled with result "sl" — the same label as a full loss at the
    # original SL with no partial close at all. pnl_pct was already computed
    # correctly (see above), but the binary result/exit_type label distorted
    # the hit rate (_calculate_hit_rate in state.py) and the TP percentile
    # auto-calibration — a trade with a net positive outcome was counted as
    # a plain loss on par with a genuine full loss. A separate
    # "sl_after_tp1" label lets it count as a partial success instead of a loss.
    result_label = "sl_after_tp1" if (result == "sl" and tp1_hit) else result

    closed_trade = {
        "side": side,
        "entry": entry,
        "sl": trade["sl"],
        "tp": trade["tp"],
        "exit": exit_price,
        "exit_time": datetime.now(timezone.utc).isoformat(),
        "result": result_label,
        "pnl_pct": round(pnl_pct, 4),
        # 🆕 (external review, P3): this is a signal bot, not an execution
        # bot (see the TP1 comment above) — it never places a real order on
        # the exchange for the 50% partial close. pnl_pct after a TP1 hit is
        # therefore a MODELLED figure that assumes the person actually
        # closed half the position manually at TP1, not a verified fill.
        # Flagged explicitly here so any downstream consumer (Discord
        # embeds, the web History panel, !history) can label it as such
        # instead of presenting it as equivalent to a single-exit PnL.
        "pnl_is_modelled": tp1_hit,
        "bars_held": bars_held,
        "lev": trade.get("lev", 1),
        "track": track,
        "tp1_hit": tp1_hit,
    }

    history_key = f"{track}_trade_history"
    state[history_key].append(closed_trade)
    state[history_key] = state[history_key][-50:]

    state["trade_history"].append(closed_trade)
    state["trade_history"] = state["trade_history"][-50:]

    # 🆕 FIX: pass track through so we don't accidentally close the other
    # track's record (A and U can simultaneously hold a position on the same
    # side for the same ticker/tf)
    update_signal_record(ticker, tf, side, exit_price, result_label, bars_held, track=track,
                          tp1_hit=tp1_hit, tp1_price=tp1_price)

    state[trade_key] = None
    state[bars_key] = 0
    notified_key = f"{track}_last_closure_notified"
    state[notified_key] = False

    # 🆕 FIX: generalized from `if track == "a": ... else: (assumed "u")` —
    # that else branch would have reset u_in_long/u_in_short for a closing
    # B-track trade instead of b_in_long/b_in_short, permanently stranding
    # b_in_long/b_in_short at True and blocking every future B-track signal
    # for the life of the process.
    state[f"{track}_in_long"] = False
    state[f"{track}_in_short"] = False

    state["active_trade"] = state.get("a_active_trade") or state.get("u_active_trade") or state.get("b_active_trade")
    state["bars_in_trade"] = max(state.get("a_bars_in_trade", 0), state.get("u_bars_in_trade", 0), state.get("b_bars_in_trade", 0))
    state["last_closure_notified"] = state.get("a_last_closure_notified", False) and state.get("u_last_closure_notified", False)

    logger.info(f"[TRADE] Closed {track.upper()}-track {side.upper()} | PnL: {pnl_pct:.2f}% | Result: {result.upper()}")
    return closed_trade


# =====================================================================
# 🆕 UNIFIED POSITION OPENING (replaces 4 duplicated blocks)
# =====================================================================

async def open_position(
    state: Dict[str, Any],
    track: str,
    side: str,
    close_v: float,
    fs: pd.Series,
    fu: pd.Series,
    fl: pd.Series,
    atr14: pd.Series,
    df: pd.DataFrame,
    idx: int,
    ticker: str,
    timeframe: str,
    regime: str,
    vol_info: Dict,
    oc_lev_delta: int,
    onchain_bias: Optional[Dict],
    dry_run: bool,
    calc_confidence,
    MIN_RR: float,
    spread_info: Optional[Dict] = None,
) -> Optional[Tuple]:
    """
    Unified position opening logic for any track (a/u) and side (long/short).
    Returns signal tuple if position opened, None if filtered out.
    """
    trade_key = f"{track}_active_trade"
    if state.get(trade_key):
        return None

    sl, sl_desc = calculate_adaptive_sl(close_v, side, ticker, timeframe, fs, fu, fl, atr14, idx)

    # 🆕 FIX: leverage used to be computed BEFORE calling calculate_adaptive_sl,
    # from a rough estimate of the FRAMA channel width (frama_sl_long/short in
    # check_signals) — i.e. leverage didn't match the real risk that the
    # adaptive SL (historical MAE percentile) actually determined. Now it's
    # computed from the actual sl.
    atr_v = max(float(atr14.iloc[idx]), 1e-8)
    sl_atr_mult = max(1.0, min(3.5, abs(close_v - sl) / atr_v))
    lev = max(1, min(MAX_ALLOWED_LEV, int(TARGET_RISK_DEP / max(sl_atr_mult, 0.1))))
    if regime == "CHAOS":
        lev = max(1, int(lev * 0.5))
    if regime == "TREND":
        lev = min(MAX_ALLOWED_LEV, int(lev * 1.2))
    lev, vol_lev_reason = volume_leverage_adjustment_v3(vol_info, side, lev)
    lev = max(1, min(MAX_ALLOWED_LEV, lev + oc_lev_delta))

    tp1, tp2, tp_desc = calculate_combined_tp(ticker, timeframe, side, close_v, sl, df, idx, atr14, regime)
    tp = tp2  # primary TP for R:R calculations and filters — we use tp2
    tp_desc = f"SL:{sl_desc} | {tp_desc}"

    if side == "long":
        risk = abs(close_v - sl)
        reward = abs(tp - close_v)
    else:
        risk = abs(sl - close_v)
        reward = abs(close_v - tp)
    rr = reward / max(risk, 1e-8)

    tp, sl, oc_desc, oc_ok = apply_onchain_with_safety(
        close_v, sl, tp, side, onchain_bias, min_rr=MIN_RR
    )
    tp_desc += oc_desc

    # Recompute TP1 proportionally if on-chain changed TP2.
    # Without this, when tp_mult < 1.0, TP1 > TP2 becomes possible (for a
    # short, TP1 < TP2).
    if abs(tp - close_v) > 1e-8 and abs(tp2 - close_v) > 1e-8:
        ratio = abs(tp - close_v) / abs(tp2 - close_v)
        if side == "long":
            tp1 = round_price(close_v + (tp1 - close_v) * ratio)
        else:
            tp1 = round_price(close_v - (close_v - tp1) * ratio)

    # 🆕 GUARD: make sure tp1 stays between entry and tp (no farther than tp,
    # no closer than entry).
    # 🆕 FIX (Kimi review #11): the lower bound used to be exactly close_v —
    # under heavy on-chain TP compression (tp_mult << 1) the proportional
    # recalculation could yield tp1 == close_v, closing 50% of the position
    # at entry with zero PnL. Now the minimum is entry±0.1% (reusing
    # SL_MIN_DISTANCE_PCT, the same "not zero" threshold already used for
    # SL), then we re-clamp against tp so the order of operations can't push
    # tp1 past tp.
    if side == "long":
        tp1 = max(tp1, close_v * (1 + _cfg.SL_MIN_DISTANCE_PCT))
        tp1 = min(tp1, tp)       # tp1 no farther than tp
    else:
        tp1 = min(tp1, close_v * (1 - _cfg.SL_MIN_DISTANCE_PCT))
        tp1 = max(tp1, tp)       # tp1 no farther than tp (for short, tp < entry)

    if side == "long":
        risk = abs(close_v - sl)
        reward = abs(tp - close_v)
        bar_extreme = float(df["low"].iloc[idx])
        extreme_violation = bar_extreme <= sl
    else:
        risk = abs(sl - close_v)
        reward = abs(close_v - tp)
        bar_extreme = float(df["high"].iloc[idx])
        extreme_violation = bar_extreme >= sl

    rr = reward / max(risk, 1e-8)

    track_label = _cfg.TRACK_LABELS[track]
    signal_label = f"{track_label} BUY  ({_cfg.TRACK_NAMES[track]})" if side == "long" else \
                   f"{track_label} SELL ({_cfg.TRACK_NAMES[track]})"

    in_long_key = f"{track}_in_long"
    in_short_key = f"{track}_in_short"
    last_bar_key = f"last_{track}_{side}_bar"
    bars_key = f"{track}_bars_in_trade"

    # 🆕 FIX: this used to reset last_bar_key = None on a filter rejection,
    # which zeroed the cooldown and made the signal retry opening on every
    # scan cycle (every ~20s) until a new bar arrived — wasted work and log
    # spam. Now last_bar_key is left untouched: cooldown (COOLDOWN_BARS)
    # works as intended, in_long/in_short just clears the "in position" flag.
    if rr < MIN_RR:
        logger.info(f"[FILTER] {ticker} {timeframe} {track_label}-{side.upper()} skipped — R:R={rr:.2f} < {MIN_RR}")
        if not dry_run:
            state[in_long_key] = False
            state[in_short_key] = False
        return None

    if extreme_violation:
        logger.info(f"[FILTER] {ticker} {timeframe} {track_label}-{side.upper()} skipped — bar extreme violates SL")
        if not dry_run:
            state[in_long_key] = False
            state[in_short_key] = False
        return None

    # 🆕 Order book spread gate — live-only (see spread.py's module
    # docstring: spread_info is always None during backtest_history(), so
    # this is a structural no-op there, not a bug). Uses this specific
    # signal's own SL distance (risk/close_v), not a flat percent — see the
    # SPREAD section in config.py for why.
    if _cfg.ENABLE_SPREAD_FILTER and spread_info is not None:
        sl_distance_pct = risk / max(close_v, 1e-8)
        gate = spread.evaluate_spread_gate(spread_info, sl_distance_pct)
        if gate["blocked"]:
            logger.info(
                f"[FILTER] {ticker} {timeframe} {track_label}-{side.upper()} skipped — "
                f"spread too wide (reason={gate['reason']}, spread={gate.get('spread_pct', 0)*100:.3f}%, "
                f"eats_sl={gate.get('eats_sl_pct', 0)*100:.0f}%)"
            )
            if not dry_run:
                state[in_long_key] = False
                state[in_short_key] = False
            return None

    state[trade_key] = {
        "side": side,
        "entry": close_v,
        "sl": sl,
        "tp": tp,    # TP2 — target for 100% of the position
        "tp1": tp1,  # TP1 — statistical, target for 50% of the position
        "lev": lev,
        "bar_opened": idx,
        # 🆕 FIX: besides the positional idx (only valid for the df the
        # signal was computed on) we also store the real bar timestamp — so
        # !chart can reliably locate the right bar in its OWN, independently
        # fetched df.
        "bar_opened_time": int(df["timestamp"].iloc[idx]),
        "tp1_hit": False,  # flag: TP1 notification already sent
    }
    state[bars_key] = 0

    # 🆕 FIX BUG-LO006: the cooldown (last_{track}_{side}_time, read by
    # cooldown_ok in check_signals) used to be armed in check_signals BEFORE
    # open_position was even called — meaning even an attempt rejected here
    # (R:R < MIN_RR or extreme_violation just above) already blocked the next
    # COOLDOWN_BARS bars, even though no trade was actually opened. Now the
    # cooldown is only armed here, at the point of actually opening a position.
    if not dry_run:
        bar_idx_val = idx
        bar_time_val = int(df["timestamp"].iloc[idx])
        state[f"{track}_{side}_bar"] = bar_idx_val
        state[last_bar_key] = bar_idx_val               # debug/backward compatibility
        state[f"last_{track}_{side}_time"] = bar_time_val

    if not dry_run:
        # 🆕 FIX: pass track through so A- and U-track records don't get mixed up in history
        add_signal_record(ticker, timeframe, side, close_v, datetime.now(timezone.utc).isoformat(), regime, track=track)

    stats = get_signal_stats(ticker, timeframe, side, regime)
    conf = calc_confidence(side == "long")

    return (signal_label, close_v, regime, lev, int(df["timestamp"].iloc[idx]), conf, sl, tp, tp1, risk, stats, tp_desc)

# =====================================================================
# 🧠  CHECK SIGNALS (MAIN LOGIC)
# =====================================================================

async def check_signals(
    exchange: ccxt.Exchange,
    ticker: str,
    timeframe: str,
    state: Dict[str, Any],
    dry_run: bool = False,
    onchain_bias: Optional[Dict] = None,
    spread_info: Optional[Dict] = None,
) -> Tuple[List[Tuple], Optional[int], str, int]:
    """
    Checks signals for a pair/timeframe.
    Returns: (signals, bar_time, regime, leverage)
    dry_run=True       — doesn't write signals to history (used by !scan).
    onchain_bias=dict  — data from onchain.get_onchain_bias(), affects TP/SL/lev/confidence.
    spread_info=dict   — one already-fetched order book snapshot from
                         spread.get_spread_snapshot(), evaluated against
                         this signal's own SL distance. Live-only (see
                         spread.py's module docstring) — None during
                         backtest_history(), where this gate is a no-op.
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

        for track in _cfg.TRACKS:
            trade = state.get(f"{track}_active_trade")
            if trade:
                update_signal_mae_mfe(ticker, timeframe, trade["side"], last_close, track=track,
                                       high=last_high, low=last_low)
                hit = check_tp_sl_hit(state, last_high, last_low, track, bar_time=current_bar_time)
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
        fs, fu, fl, fdir = calculate_frama(df, _cfg.FRAMA_LEN, _cfg.FRAMA_MULT)
        mfi = calculate_mfi(df, _cfg.MFI_LEN)
        # 🆕 FIX (external review, P1): mfi here is computed on the FULL
        # fetched df, which includes the still-forming last bar — but the
        # signal itself is evaluated at idx = len(df)-2 (the last CLOSED
        # bar, defined a few lines below). Training K-Means on one bar MORE
        # than what the signal is actually based on is a small but real
        # point-in-time mismatch, and it's exactly what made live diverge
        # from backtest_history()'s now-strictly-point-in-time
        # run_kmeans_mfi_rolling() (mfi.iloc[:idx+1] there vs the full
        # series here). Truncate the same way live's own signal already is.
        level_os, level_ob = run_kmeans_mfi(mfi.iloc[:len(df) - 1], _cfg.MFI_TRAINING)
        and_osc, and_sig = calculate_andean(df, _cfg.AND_LEN, _cfg.AND_SIG_LEN)
        ut_buy, ut_sell = calculate_ut_bot(df, _cfg.UT_SENSITIVITY, _cfg.UT_PERIOD, use_ha=_cfg.UT_HEIKIN_ASHI)
        # 🆕 Only computed when the filter is actually on (off by default) —
        # the rolling R/S calculation is meaningfully more expensive than the
        # other indicators here, no point paying for it unused.
        hurst = calculate_hurst(df, window=_cfg.HURST_WINDOW) if _cfg.ENABLE_HURST_FILTER else None
        # 🆕 Track "b" (Breakout) — see config.py's BREAKOUT section for the
        # methodology. Always computed (unlike Hurst, this isn't behind a
        # toggle — it IS the B-track's entry logic, not an optional filter).
        breakout_long_s, breakout_short_s = calculate_breakout_signal(
            df, atr14, atr_pct,
            lookback=_cfg.BREAKOUT_LOOKBACK,
            squeeze_window=_cfg.BREAKOUT_SQUEEZE_WINDOW,
            squeeze_percentile=_cfg.BREAKOUT_SQUEEZE_PERCENTILE,
            vol_spike_mult=_cfg.BREAKOUT_VOL_SPIKE_MULT,
            range_atr_mult=_cfg.BREAKOUT_RANGE_ATR_MULT,
            close_loc_min=_cfg.BREAKOUT_CLOSE_LOC_MIN,
        )

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

        # 🆕 Hurst exponent — direction-agnostic "regime clarity" check, a
        # second opinion alongside CHOP (see indicators.calculate_hurst).
        # Rejects both long and short signals equally when the market sits
        # close to a random walk, where neither trending nor mean-reverting
        # strategies have much statistical edge — not a trend/mean-reversion
        # split between the A/U tracks (that's a possible future refinement,
        # not what this first version does).
        hurst_ok = True
        if _cfg.ENABLE_HURST_FILTER and hurst is not None:
            hurst_v = float(hurst.iloc[idx])
            hurst_ok = not np.isnan(hurst_v) and abs(hurst_v - 0.5) >= _cfg.HURST_MIN_DEVIATION

        regime = "CHAOS" if atr_pct_v > ATR_MAX else "TREND" if atr_pct_v > ATR_MIN * 1.5 else "NORMAL"

        vol_info = volume_flow_signal_v3(df)
        vol_lev_reason = "no signal"

        warmed_up = len(df) >= _cfg.MFI_TRAINING
        # 🆕 FIX: B-track doesn't use MFI/Andean at all, so gating it on
        # MFI_TRAINING (default 800 bars) was borrowed from A-track without
        # actually applying to B — if MFI_TRAINING is ever raised above the
        # fetched 900-bar window, B would silently stop firing for a reason
        # that has nothing to do with what it actually needs. B's own
        # requirement is just enough bars for its squeeze window + box
        # lookback to have real (non-truncated) history.
        b_warmed_up = len(df) >= (_cfg.BREAKOUT_SQUEEZE_WINDOW + _cfg.BREAKOUT_LOOKBACK)

        filter_long = (
            (not _cfg.ENABLE_FRAMA_FILTER or frama_bull)
            and (not _cfg.ENABLE_CHOP_FILTER  or chop_ok)
            and (not _cfg.ENABLE_ATR_FILTER   or atr_ok)
            and slope_long
            and (not _cfg.ENABLE_MTF_BIAS     or htf_bull)
            and (not _cfg.ENABLE_FAKE_BREAK_FILTER or not fake_break_long)
            and (not _cfg.ENABLE_LIQ_SWEEP_FILTER  or not liq_sweep_short)
            and hurst_ok
        )
        filter_short = (
            (not _cfg.ENABLE_FRAMA_FILTER or frama_bear)
            and (not _cfg.ENABLE_CHOP_FILTER  or chop_ok)
            and (not _cfg.ENABLE_ATR_FILTER   or atr_ok)
            and slope_short
            and (not _cfg.ENABLE_MTF_BIAS     or htf_bear)
            and (not _cfg.ENABLE_FAKE_BREAK_FILTER or not fake_break_short)
            and (not _cfg.ENABLE_LIQ_SWEEP_FILTER  or not liq_sweep_long)
            and hurst_ok
        )

        # 🆕 Track "b" (Breakout) — deliberately does NOT gate on CHOP or
        # FRAMA direction/slope (see config.py's BREAKOUT section): CHOP
        # reads the tail of the just-ended squeeze as "still choppy" right
        # as the breakout starts, and FRAMA's slope is exactly what lags in
        # this pattern — gating on either would block the track from ever
        # catching what it exists to catch. ATR bounds, HTF bias,
        # fake-break/liquidity-sweep, Hurst, and spread filters still apply;
        # those catch different failure modes (thin execution, manipulation
        # wicks) a strong squeeze breakout is not automatically immune to.
        filter_long_b = (
            (not _cfg.ENABLE_ATR_FILTER   or atr_ok)
            and (not _cfg.ENABLE_MTF_BIAS     or htf_bull)
            and (not _cfg.ENABLE_FAKE_BREAK_FILTER or not fake_break_long)
            and (not _cfg.ENABLE_LIQ_SWEEP_FILTER  or not liq_sweep_short)
            and hurst_ok
        )
        filter_short_b = (
            (not _cfg.ENABLE_ATR_FILTER   or atr_ok)
            and (not _cfg.ENABLE_MTF_BIAS     or htf_bear)
            and (not _cfg.ENABLE_FAKE_BREAK_FILTER or not fake_break_short)
            and (not _cfg.ENABLE_LIQ_SWEEP_FILTER  or not liq_sweep_long)
            and hurst_ok
        )

        mfi_bull_sig = crossover(mfi, level_os, idx)
        mfi_bear_sig = crossunder(mfi, level_ob, idx)
        and_bull_sig = crossover2(and_osc, and_sig, idx)
        and_bear_sig = crossunder2(and_osc, and_sig, idx)

        bs_and_bull = bars_since_crossover2(and_osc, and_sig, idx, _cfg.LOOKBACK)
        bs_mfi_bull = bars_since_crossover(mfi, level_os, idx, _cfg.LOOKBACK)
        bs_and_bear = bars_since_crossunder2(and_osc, and_sig, idx, _cfg.LOOKBACK)
        bs_mfi_bear = bars_since_crossunder(mfi, level_ob, idx, _cfg.LOOKBACK)

        confirm_long_a = (mfi_bull_sig and bs_and_bull <= _cfg.LOOKBACK) or (and_bull_sig and bs_mfi_bull <= _cfg.LOOKBACK)
        confirm_short_a = (mfi_bear_sig and bs_and_bear <= _cfg.LOOKBACK) or (and_bear_sig and bs_mfi_bear <= _cfg.LOOKBACK)

        # 🆕 FIX BUG-LO005: the cooldown used to be computed via
        # bar_idx = len(df) - 2, which is recomputed on EVERY call to
        # check_signals from a freshly-fetched limit=900 window of bars —
        # meaning it's almost always the same number (~898), NOT a
        # continuously-increasing counter of elapsed bars (unlike
        # backtest_history, where idx genuinely advances through a loop over
        # history). As a result, (bar_idx - last_bar) was comparing two
        # nearly-identical positions and the cooldown fired unpredictably —
        # sometimes blocking a signal far longer than COOLDOWN_BARS,
        # sometimes releasing early, depending on random fluctuations in the
        # length of the candle window the exchange happened to return.
        # Now the cooldown is computed from the REAL bar time (bar_time, ms)
        # — independent of how many bars the exchange returned for a given
        # request, and correctly reflects the number of timeframes elapsed.
        try:
            tf_ms = int(exchange.parse_timeframe(timeframe) * 1000)
        except Exception:
            tf_ms = 3600_000  # fallback: assume a 1h timeframe if parse_timeframe isn't available

        def cooldown_ok(last_time):
            return last_time is None or (bar_time - last_time) >= COOLDOWN_BARS * tf_ms

        a_long_cd_ok = cooldown_ok(state.get("last_a_long_time"))
        a_short_cd_ok = cooldown_ok(state.get("last_a_short_time"))
        u_long_cd_ok = cooldown_ok(state.get("last_u_long_time"))
        u_short_cd_ok = cooldown_ok(state.get("last_u_short_time"))
        b_long_cd_ok = cooldown_ok(state.get("last_b_long_time"))
        b_short_cd_ok = cooldown_ok(state.get("last_b_short_time"))

        a_in_pos = state["a_in_long"] or state["a_in_short"]
        u_in_pos = state["u_in_long"] or state["u_in_short"]
        b_in_pos = state["b_in_long"] or state["b_in_short"]

        # 🆕 FIX (Kimi review #1): U-track was already gated by is_new_bar
        # (the signal is checked once per new bar). A-track had no such
        # guard — confirm_long_a/confirm_short_a stay True for the whole
        # closed bar, and if open_position() rejects the attempt
        # (R:R/extreme_violation), the in_pos flag resets back to False (see
        # open_position), so on every subsequent scan (~15-60s) the bot
        # tried entering again — spamming [FILTER] skipped in the logs and
        # calling open_position redundantly on the same bar.
        # last_a_{side}_attempt_bar guarantees at most one attempt per track
        # per bar, symmetric to how is_new_bar already works for U-track.
        a_long_not_attempted  = state.get("last_a_long_attempt_bar")  != bar_time
        a_short_not_attempted = state.get("last_a_short_attempt_bar") != bar_time

        sig_a_long  = confirm_long_a  and filter_long  and not a_in_pos and a_long_cd_ok  and warmed_up and a_long_not_attempted
        sig_a_short = confirm_short_a and filter_short and not a_in_pos and a_short_cd_ok and warmed_up and a_short_not_attempted
        sig_u_long  = bool(ut_buy.iloc[idx])  and filter_long  and not u_in_pos and u_long_cd_ok  and is_new_bar
        sig_u_short = bool(ut_sell.iloc[idx]) and filter_short and not u_in_pos and u_short_cd_ok and is_new_bar
        # 🆕 Track "b" — like UT Bot, this is a one-shot "this bar IS the
        # breakout bar" condition (not a lingering confluence state like
        # Andean/MFI), so it's gated by is_new_bar the same way U-track is.
        sig_b_long  = bool(breakout_long_s.iloc[idx])  and filter_long_b  and not b_in_pos and b_long_cd_ok  and is_new_bar and b_warmed_up
        sig_b_short = bool(breakout_short_s.iloc[idx]) and filter_short_b and not b_in_pos and b_short_cd_ok and is_new_bar and b_warmed_up

        # Track flags are only set on an actual open (not dry_run), and ALWAYS
        # together with active_trade, so they never get out of sync.
        # 🆕 FIX BUG-LO006: last_{track}_{side}_bar/_time (cooldown) are no
        # longer written here — they used to be armed here, BEFORE
        # open_position, so even an attempt rejected there
        # (R:R/extreme_violation) already blocked the next COOLDOWN_BARS
        # bars. Now the cooldown is only armed inside open_position, at the
        # point of actually opening the trade.
        if not dry_run:
            if sig_a_long:
                state["a_in_long"] = True
                state["a_in_short"] = False
            if sig_a_short:
                state["a_in_short"] = True
                state["a_in_long"] = False
            if sig_u_long:
                state["u_in_long"] = True
                state["u_in_short"] = False
            if sig_u_short:
                state["u_in_short"] = True
                state["u_in_long"] = False
            if sig_b_long:
                state["b_in_long"] = True
                state["b_in_short"] = False
            if sig_b_short:
                state["b_in_short"] = True
                state["b_in_long"] = False

        # 🆕 NOTE: sugg_lev here is a rough informational estimate (from the
        # FRAMA channel width), used only as a fallback value when there's no
        # signal (the function's return) and for regime logging. The real
        # leverage for each opened position is now computed INSIDE
        # open_position from the actual adaptive SL (see the fix there) —
        # this estimate has no effect on it.
        frama_sl_long = max(1.0, min(3.5, abs(close_v - float(fl.iloc[idx])) / atr_v))
        frama_sl_short = max(1.0, min(3.5, abs(float(fu.iloc[idx]) - close_v) / atr_v))
        sugg_sl = frama_sl_long if (sig_a_long or sig_u_long or sig_b_long) else frama_sl_short
        sugg_lev = max(1, min(MAX_ALLOWED_LEV, int(TARGET_RISK_DEP / max(sugg_sl, 0.1))))

        if regime == "CHAOS":
            sugg_lev = max(1, int(sugg_lev * 0.5))
        if regime == "TREND":
            sugg_lev = min(MAX_ALLOWED_LEV, int(sugg_lev * 1.2))

        oc_bias_long  = 0
        oc_bias_short = 0
        oc_lev_delta  = 0

        if onchain_bias and ONCHAIN_ENABLED:
            oc_bias_long      = onchain_bias.get("bias_long",      0)
            oc_bias_short     = onchain_bias.get("bias_short",     0)
            oc_lev_delta      = onchain_bias.get("lev_delta",      0)

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

        async def _safe_open_position(track: str, side: str):
            """Wraps open_position with exception safety (Kimi review #2).
            state["{track}_in_long/short"] is already set True above, before
            open_position is called — open_position itself resets it back to
            False on an explicit rejection (R:R < MIN_RR, extreme_violation),
            but NOT on an exception (e.g. if calculate_combined_tp or
            apply_onchain_with_safety raise). Without this wrapper, such an
            exception would escape to check_signals' outer try/except, the
            flag would stay True forever, and the track would silently be
            blocked until the container restarted."""
            in_long_key = f"{track}_in_long"
            in_short_key = f"{track}_in_short"
            try:
                return await open_position(state, track, side, close_v, fs, fu, fl, atr14, df, idx,
                                            ticker, timeframe, regime, vol_info, oc_lev_delta, onchain_bias, dry_run,
                                            calc_confidence, MIN_RR, spread_info=spread_info)
            except Exception as e:
                logger.error(f"[OPEN_POSITION] {ticker} {timeframe} {track.upper()}-{side.upper()} failed: {e}", exc_info=True)
                if not dry_run:
                    state[in_long_key] = False
                    state[in_short_key] = False
                return None

        signals = []
        MIN_RR = _cfg.MIN_RR

        # --- A-track LONG ---
        if sig_a_long:
            sig = await _safe_open_position("a", "long")
            state["last_a_long_attempt_bar"] = bar_time
            if sig:
                signals.append(sig)
            else:
                sig_a_long = False

        # --- U-track LONG ---
        if sig_u_long:
            sig = await _safe_open_position("u", "long")
            if sig:
                signals.append(sig)
            else:
                sig_u_long = False

        # --- A-track SHORT ---
        if sig_a_short:
            sig = await _safe_open_position("a", "short")
            state["last_a_short_attempt_bar"] = bar_time
            if sig:
                signals.append(sig)
            else:
                sig_a_short = False

        # --- U-track SHORT ---
        if sig_u_short:
            sig = await _safe_open_position("u", "short")
            if sig:
                signals.append(sig)
            else:
                sig_u_short = False

        # --- B-track (Breakout) LONG ---
        if sig_b_long:
            sig = await _safe_open_position("b", "long")
            if sig:
                signals.append(sig)
            else:
                sig_b_long = False

        # --- B-track (Breakout) SHORT ---
        if sig_b_short:
            sig = await _safe_open_position("b", "short")
            if sig:
                signals.append(sig)
            else:
                sig_b_short = False

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
    num_bars: int = 3000,
    tracks: tuple = _cfg.TRACKS,
) -> int:
    """Backtest for accumulating signal history.

    tracks — restrict which track(s) get simulated (default: all of them).
    Use this to backfill just one track (e.g. tracks=("b",)) on a
    ticker/tf that already has history for the others — running the full,
    unrestricted backtest again on an already-populated ticker/tf would
    duplicate every existing A/U record (see startup_sequence's comment on
    this in bot.py), so this is the safe way to add coverage for a track
    added after the others already had months of accumulated history.
    """
    logger.info(f"[BACKTEST] Starting {ticker} {tf} ({num_bars} bars, tracks={tracks})...")

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
        fs, fu, fl, fdir = calculate_frama(df, _cfg.FRAMA_LEN, _cfg.FRAMA_MULT)
        mfi = calculate_mfi(df, _cfg.MFI_LEN)
        # 🆕 FIX (external review, P0): rolling/point-in-time version, not
        # the plain run_kmeans_mfi() live uses — see
        # run_kmeans_mfi_rolling's docstring. A single upfront call here
        # used to train OS/OB on the LAST training_size bars of the whole
        # backtest and apply that to every historical bar, including ones
        # from long before that training window existed — level_os/level_ob
        # are now per-bar Series instead of fixed scalars; crossover() and
        # friends already handle either.
        level_os, level_ob = run_kmeans_mfi_rolling(mfi, _cfg.MFI_TRAINING)
        and_osc, and_sig = calculate_andean(df, _cfg.AND_LEN, _cfg.AND_SIG_LEN)
        ut_buy, ut_sell = calculate_ut_bot(df, _cfg.UT_SENSITIVITY, _cfg.UT_PERIOD, use_ha=_cfg.UT_HEIKIN_ASHI)
        # 🆕 Always computed here (unlike check_signals, not gated behind the
        # toggle) — backtest_history is a one-off run, not a per-scan cost
        # concern, and needs to match live's filter logic for calibration
        # consistency regardless of whether the toggle happens to be on right now.
        hurst = calculate_hurst(df, window=_cfg.HURST_WINDOW)
        # 🆕 Track "b" (Breakout) — same detector as the live path, computed
        # once as a full series here too (see calculate_breakout_signal's
        # docstring / config.py's BREAKOUT section for the methodology).
        breakout_long_s, breakout_short_s = calculate_breakout_signal(
            df, atr14, atr_pct,
            lookback=_cfg.BREAKOUT_LOOKBACK,
            squeeze_window=_cfg.BREAKOUT_SQUEEZE_WINDOW,
            squeeze_percentile=_cfg.BREAKOUT_SQUEEZE_PERCENTILE,
            vol_spike_mult=_cfg.BREAKOUT_VOL_SPIKE_MULT,
            range_atr_mult=_cfg.BREAKOUT_RANGE_ATR_MULT,
            close_loc_min=_cfg.BREAKOUT_CLOSE_LOC_MIN,
        )

        htf = _cfg.HTF_BIAS
        htf_bias_arr = np.zeros(len(df))

        try:
            htf_bars = exchange.fetch_ohlcv(ticker, htf, limit=num_bars)
            if htf_bars and len(htf_bars) >= 100:
                df_htf = parse_ohlcv(htf_bars)
                if validate_dataframe(df_htf, 50):
                    fs_htf, fu_htf, fl_htf, fdir_htf = calculate_frama(df_htf, _cfg.FRAMA_LEN, _cfg.FRAMA_MULT)
                    ltf_times = df["timestamp"].values
                    htf_times = df_htf["timestamp"].values
                    # 🆕 FIX (external review, P0): OHLCV timestamps mark a
                    # candle's OPEN, not its close. The old condition
                    # (`htf_times[htf_idx+1] <= ltf_times[i]`) only checked
                    # that the HTF candle had STARTED by the ltf bar's time —
                    # not that it had CLOSED. For ltf=13:00 with htf=4h, the
                    # 12:00-16:00 HTF candle's open (12:00) satisfies that
                    # check, but its close price reflects the market at
                    # 16:00 — three hours in this historical bar's future.
                    # Live's get_htf_bias() doesn't have this problem (it
                    # always reads .iloc[-2], the last candle a fresh fetch
                    # confirms is actually closed) — this brings backtest in
                    # line with that by requiring the HTF candle's open +
                    # its own duration (observed directly from the fetched
                    # data, not assumed from the timeframe string) to be
                    # <= the ltf bar's time before using it.
                    htf_duration_ms = (htf_times[1] - htf_times[0]) if len(htf_times) > 1 else 0
                    htf_idx = 0
                    for i in range(len(df)):
                        if htf_times[0] + htf_duration_ms > ltf_times[i]:
                            htf_bias_arr[i] = 0
                            continue
                        while htf_idx + 1 < len(htf_times) and htf_times[htf_idx + 1] + htf_duration_ms <= ltf_times[i]:
                            htf_idx += 1
                        if htf_idx >= _cfg.FRAMA_LEN * 2:
                            htf_close = float(df_htf["close"].iloc[htf_idx])
                            htf_frama_val = float(fs_htf.iloc[htf_idx])
                            # BUGFIX BUG-ME003: FRAMA can return NaN for the first ~22 bars.
                            # htf_close > NaN = False (IEEE 754) → bias = -1 (bear) instead of 0 (neutral).
                            # This creates a false bearish bias and distorts backtest stats.
                            if np.isnan(htf_frama_val):
                                htf_bias_arr[i] = 0
                            else:
                                htf_bias_arr[i] = 1 if htf_close > htf_frama_val else -1
        except Exception as e:
            logger.warning(f"[BACKTEST] HTF bias fetch failed for {ticker} {tf}: {e}")

        signals_found = 0
        history = load_signals_history()

        # Hurst is always computed above (unlike check_signals, not gated
        # behind ENABLE_HURST_FILTER) and is NaN for its first HURST_WINDOW
        # bars while warming up. Starting the loop at a flat idx=50 used to
        # walk straight into that warm-up window whenever the filter was on,
        # so every signal in idx 50..HURST_WINDOW-1 was unconditionally
        # blocked (hurst_ok = False on NaN) and never entered the training
        # history — silently skewing the adaptive TP/SL calibration toward
        # later, possibly less volatile data. Start past warm-up so those
        # bars are eligible on equal footing with the rest of the backtest.
        #
        # 🆕 FIX (external review, P1): only reserve those extra bars when
        # the filter is actually on. It was unconditional before — with
        # ENABLE_HURST_FILTER off (the default), that discarded ~110 bars
        # of otherwise-usable training data for a filter that isn't even
        # active, shrinking the calibration sample for no reason.
        start_idx = max(50, _cfg.HURST_WINDOW + 10) if _cfg.ENABLE_HURST_FILTER else 50

        # 🆕 FIX (external review, P0): the loop below used to have no
        # concept of a track already being "in a position" — every idx
        # where a signal condition was true immediately got its own
        # independent future_idx simulation, with no cooldown either. Since
        # live only ever allows ONE open position per track (a_in_long/
        # a_in_short etc. block re-entry until close_trade() runs, then
        # cooldown_ok() gates the next attempt), the backtest could and did
        # record multiple overlapping "trades" for the same track — e.g. a
        # signal at bar 100 simulated through bar 150, while a second signal
        # at bar 110 (still technically "inside" the first one, if this were
        # live) got its own separate, overlapping simulation. That directly
        # corrupts the MFE/MAE/win-rate distributions used to calibrate
        # adaptive TP/SL, since the training data represents a trading
        # pattern (parallel overlapping entries per track) the live bot
        # never actually executes.
        #
        # next_available_idx[track] mirrors live's in_position + cooldown
        # combined into one bar index: a track's signal conditions are only
        # even evaluated once idx reaches that value, and it's pushed
        # forward to (exit_idx + COOLDOWN_BARS) every time a simulated trade
        # for that track closes — one position at a time, with the same
        # cooldown gap live enforces, matching the real trading model.
        next_available_idx = {t: -1 for t in _cfg.TRACKS}

        # 🆕 FIX (external review, P1): was a flat `len(df) - 100`, decoupled
        # from _cfg.MAX_HOLD_BARS (default 20) — see the future_idx loop
        # below for why that matters. Reserve exactly enough trailing bars
        # for the longest simulation the hold period actually needs, not a
        # number that happened to match an old hardcoded default.
        for idx in range(start_idx, len(df) - _cfg.MAX_HOLD_BARS):
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

            hurst_v = float(hurst.iloc[idx])
            hurst_ok = not np.isnan(hurst_v) and abs(hurst_v - 0.5) >= _cfg.HURST_MIN_DEVIATION

            bt_regime = "CHAOS" if atr_pct_v > ATR_MAX else "TREND" if atr_pct_v > ATR_MIN * 1.5 else "NORMAL"
            warmed_up_bt = idx >= _cfg.MFI_TRAINING
            # 🆕 FIX: same reasoning as live's b_warmed_up — B doesn't use
            # MFI, so it shouldn't be gated on MFI_TRAINING (previously it
            # had NO warmup gate at all in the backtest, the opposite
            # problem: too permissive, could fire on bars where the squeeze
            # window's rolling percentile is only partially populated).
            b_warmed_up_bt = idx >= (_cfg.BREAKOUT_SQUEEZE_WINDOW + _cfg.BREAKOUT_LOOKBACK)

            filter_long = (
                (not _cfg.ENABLE_FRAMA_FILTER or frama_bull)
                and (not _cfg.ENABLE_CHOP_FILTER  or chop_ok)
                and (not _cfg.ENABLE_ATR_FILTER   or atr_ok)
                and slope_long
                and (not _cfg.ENABLE_FAKE_BREAK_FILTER or not fake_break_long)
                and (not _cfg.ENABLE_LIQ_SWEEP_FILTER  or not liq_sweep_short)
                and (not _cfg.ENABLE_HURST_FILTER or hurst_ok)
            )
            filter_short = (
                (not _cfg.ENABLE_FRAMA_FILTER or frama_bear)
                and (not _cfg.ENABLE_CHOP_FILTER  or chop_ok)
                and (not _cfg.ENABLE_ATR_FILTER   or atr_ok)
                and slope_short
                and (not _cfg.ENABLE_FAKE_BREAK_FILTER or not fake_break_short)
                and (not _cfg.ENABLE_LIQ_SWEEP_FILTER  or not liq_sweep_long)
                and (not _cfg.ENABLE_HURST_FILTER or hurst_ok)
            )

            # 🆕 Track "b" filters — mirrors filter_long_b/filter_short_b in
            # check_signals() exactly (no CHOP/FRAMA gating, see that
            # function's comment for why).
            filter_long_b = (
                (not _cfg.ENABLE_ATR_FILTER   or atr_ok)
                and (not _cfg.ENABLE_FAKE_BREAK_FILTER or not fake_break_long)
                and (not _cfg.ENABLE_LIQ_SWEEP_FILTER  or not liq_sweep_short)
                and (not _cfg.ENABLE_HURST_FILTER or hurst_ok)
            )
            filter_short_b = (
                (not _cfg.ENABLE_ATR_FILTER   or atr_ok)
                and (not _cfg.ENABLE_FAKE_BREAK_FILTER or not fake_break_short)
                and (not _cfg.ENABLE_LIQ_SWEEP_FILTER  or not liq_sweep_long)
                and (not _cfg.ENABLE_HURST_FILTER or hurst_ok)
            )

            mfi_bull_sig = crossover(mfi, level_os, idx)
            mfi_bear_sig = crossunder(mfi, level_ob, idx)
            and_bull_sig = crossover2(and_osc, and_sig, idx)
            and_bear_sig = crossunder2(and_osc, and_sig, idx)

            bs_and_bull = bars_since_crossover2(and_osc, and_sig, idx, _cfg.LOOKBACK)
            bs_mfi_bull = bars_since_crossover(mfi, level_os, idx, _cfg.LOOKBACK)
            bs_and_bear = bars_since_crossunder2(and_osc, and_sig, idx, _cfg.LOOKBACK)
            bs_mfi_bear = bars_since_crossunder(mfi, level_ob, idx, _cfg.LOOKBACK)

            confirm_long_a = (mfi_bull_sig and bs_and_bull <= _cfg.LOOKBACK) or (and_bull_sig and bs_mfi_bull <= _cfg.LOOKBACK)
            confirm_short_a = (mfi_bear_sig and bs_and_bear <= _cfg.LOOKBACK) or (and_bear_sig and bs_mfi_bear <= _cfg.LOOKBACK)

            htf_bull_bt = htf_bias_arr[idx] == 1
            htf_bear_bt = htf_bias_arr[idx] == -1

            sig_a_long  = confirm_long_a  and filter_long  and warmed_up_bt and (not _cfg.ENABLE_MTF_BIAS or htf_bull_bt)
            sig_a_short = confirm_short_a and filter_short and warmed_up_bt and (not _cfg.ENABLE_MTF_BIAS or htf_bear_bt)
            sig_u_long  = bool(ut_buy.iloc[idx])  and filter_long
            sig_u_short = bool(ut_sell.iloc[idx]) and filter_short
            # 🆕 Track "b" — HTF applied here (not inside filter_long_b/short_b,
            # which are defined before htf_bull_bt/htf_bear_bt exist), same
            # place A-track's HTF check happens.
            sig_b_long  = bool(breakout_long_s.iloc[idx])  and filter_long_b  and (not _cfg.ENABLE_MTF_BIAS or htf_bull_bt) and b_warmed_up_bt
            sig_b_short = bool(breakout_short_s.iloc[idx]) and filter_short_b and (not _cfg.ENABLE_MTF_BIAS or htf_bear_bt) and b_warmed_up_bt

            for track, side, sig_ok in [
                ("a", "long", sig_a_long), ("a", "short", sig_a_short),
                ("u", "long", sig_u_long), ("u", "short", sig_u_short),
                ("b", "long", sig_b_long), ("b", "short", sig_b_short),
            ]:
                if track not in tracks:
                    continue
                if idx < next_available_idx[track]:
                    continue
                if not sig_ok:
                    continue

                # 🆕 FIX (external review, P0): as_of prevents adaptive
                # TP/SL for this historical bar from seeing records that,
                # chronologically, didn't exist yet at this point — either
                # later records from THIS SAME backtest run (idx increases
                # monotonically, but signals_history.json accumulates as we
                # go) or real live trades saved before this backtest/backfill
                # happened to run. Without this, a backtest signal from
                # e.g. 2025 could get calibrated partly from 2026 live
                # trades — pure look-ahead bias.
                as_of_ts = normalize_timestamp(int(df["timestamp"].iloc[idx]))

                sl, sl_desc = calculate_adaptive_sl(close_v, side, ticker, tf, fs, fu, fl, atr14, idx, as_of=as_of_ts)
                risk_fixed = abs(close_v - sl)
                tp1, tp2, tp_desc = calculate_combined_tp(ticker, tf, side, close_v, sl, df, idx, atr14, bt_regime, as_of=as_of_ts)
                # 🆕 FIX: the backtest used to check tp_hit against tp1
                # (statistical, no RR cap), while live trading actually
                # exits at tp2 (same calculate_combined_tp, but with the RR
                # cap — see open_position(), tp = tp2). Because of this, the
                # stats (win-rate/MFE/MAE) that calibrate
                # calculate_combined_tp for FUTURE signals were trained on a
                # different exit criterion than live trading actually uses.
                # Now both paths match.
                tp = tp2

                tp_hit = sl_hit = False
                max_favorable = max_adverse = 0.0
                exit_price = close_v
                bars_held = 0

                # 🆕 FIX (external review, P0): backtest used to check only
                # the ORIGINAL sl/tp2 on every future bar, with no concept
                # of TP1 at all — live's real mechanics are two-stage (TP1
                # hit -> 50% closed -> SL moves to breakeven/half_tp1 ->
                # remainder rides to TP2 or the moved SL). A backtest trade
                # that would have banked TP1 and then given back the
                # remainder at breakeven was recorded as a plain full "sl"
                # loss — same label and PnL as a trade that never worked at
                # all — systematically pessimistic training data for
                # adaptive TP/SL, hit-rate, and regime stats. tp1_reached
                # tracks whether TP1 was crossed at any point (for PnL/label
                # purposes) even on the branch where price ran straight
                # through TP1 and TP2 in the same continuous move.
                current_sl = sl
                tp1_reached = False

                # 🆕 SL checked before TP on each bar below — see
                # config.SAME_BAR_EXIT_POLICY and check_tp_sl_hit()'s
                # docstring for why (a single bar's high/low can't tell you
                # which was actually touched first if both fall within its
                # range; this keeps backtest and live agreeing on the same
                # conservative resolution).
                # 🆕 FIX (external review, P1): was hardcoded to a flat 100
                # bars, independent of _cfg.MAX_HOLD_BARS (live force-closes
                # at 20 by default) — the backtest was simulating "cancelled"
                # outcomes on a 5x-longer holding period than live actually
                # allows, so its MFE/MAE/win-rate stats for cancelled trades
                # (and how often a trade even reaches "cancelled" vs. TP/SL
                # within the real hold window) didn't represent what live
                # trading actually does.
                for future_idx in range(idx + 1, min(idx + _cfg.MAX_HOLD_BARS + 1, len(df))):
                    fh = float(df["high"].iloc[future_idx])
                    fl_ = float(df["low"].iloc[future_idx])
                    fc = float(df["close"].iloc[future_idx])
                    bars_held = future_idx - idx

                    if side == "long":
                        favorable = (fh - close_v) / close_v * 100
                        adverse = (close_v - fl_) / close_v * 100
                        max_favorable = max(max_favorable, favorable)
                        max_adverse = max(max_adverse, adverse)

                        if not tp1_reached:
                            if fl_ <= current_sl:
                                sl_hit = True
                                exit_price = current_sl
                                break
                            if fh >= tp:
                                # Reached TP2 directly — since tp1 < tp2 on
                                # the same side, this necessarily passed
                                # through TP1 in the same continuous move
                                # (unlike SL vs TP, these aren't ambiguous
                                # relative to each other). Live's own
                                # continuous ticker check would register
                                # both essentially at once — same result
                                # here: a full win, both legs positive.
                                tp1_reached = True
                                tp_hit = True
                                exit_price = tp
                                break
                            if fh >= tp1:
                                # 🆕 Mirrors live's sl_moved_after_bar: don't
                                # also check the moved SL / TP2 on THIS same
                                # bar — only from the next one onward, since
                                # we can't know whether the touch happened
                                # at the start or end of this bar's range.
                                tp1_reached = True
                                if _cfg.TP1_SL_MODE == "half_tp1":
                                    current_sl = close_v + (tp1 - close_v) / 2
                                else:
                                    current_sl = close_v
                                continue
                        else:
                            if fl_ <= current_sl:
                                sl_hit = True
                                exit_price = current_sl
                                break
                            if fh >= tp:
                                tp_hit = True
                                exit_price = tp
                                break
                    else:
                        favorable = (close_v - fl_) / close_v * 100
                        adverse = (fh - close_v) / close_v * 100
                        max_favorable = max(max_favorable, favorable)
                        max_adverse = max(max_adverse, adverse)

                        if not tp1_reached:
                            if fh >= current_sl:
                                sl_hit = True
                                exit_price = current_sl
                                break
                            if fl_ <= tp:
                                tp1_reached = True
                                tp_hit = True
                                exit_price = tp
                                break
                            if fl_ <= tp1:
                                tp1_reached = True
                                if _cfg.TP1_SL_MODE == "half_tp1":
                                    current_sl = close_v - (close_v - tp1) / 2
                                else:
                                    current_sl = close_v
                                continue
                        else:
                            if fh >= current_sl:
                                sl_hit = True
                                exit_price = current_sl
                                break
                            if fl_ <= tp:
                                tp_hit = True
                                exit_price = tp
                                break

                if not tp_hit and not sl_hit and bars_held > 0:
                    exit_price = float(df["close"].iloc[min(idx + bars_held, len(df) - 1)])

                # 🆕 See the next_available_idx comment above the outer loop —
                # this is where the "position" for this track actually
                # closes in the simulation; block re-entry for this track
                # until COOLDOWN_BARS past that point, same as live.
                exit_idx = idx + bars_held
                next_available_idx[track] = exit_idx + _cfg.COOLDOWN_BARS

                _ensure_history_slot(history, ticker, tf)
                # 🆕 FIX BUG-LO008 (backtest side of it): a trade where TP1
                # already banked profit and the remainder gave it back at
                # the moved SL gets its own "sl_after_tp1" label — same
                # convention close_trade() already uses live — instead of
                # being indistinguishable from a full loss at the original
                # SL. tp_hit==True already implies a full win regardless of
                # whether TP1 was crossed on the way (see the "reached TP2
                # directly" branch above), so only the sl_hit branch needs
                # to check tp1_reached.
                exit_type = "tp" if tp_hit else ("sl_after_tp1" if (sl_hit and tp1_reached) else "sl" if sl_hit else "cancelled")

                # 🆕 Real two-leg economics once TP1 was crossed — same
                # formula as state.update_signal_record()/signals.close_trade()
                # for the live path: half the position's outcome is locked
                # in at TP1, the other half at wherever it actually closed
                # (moved SL, TP2, or a force-close price if never resolved).
                if tp1_reached:
                    tp1_leg_pct = (tp1 - close_v) / close_v * 100 if side == "long" else (close_v - tp1) / close_v * 100
                    remainder_leg_pct = (exit_price - close_v) / close_v * 100 if side == "long" else (close_v - exit_price) / close_v * 100
                    moved_pct = (tp1_leg_pct + remainder_leg_pct) / 2
                else:
                    moved_pct = (exit_price - close_v) / close_v * 100 if side == "long" else (close_v - exit_price) / close_v * 100

                history[ticker][tf][side].append({
                    "entry": round_price(close_v),
                    "exit": round_price(exit_price),
                    "exit_type": exit_type,
                    "bars_held": bars_held,
                    "moved_pct": round(moved_pct, 4),
                    "timestamp": normalize_timestamp(int(df["timestamp"].iloc[idx])),
                    "max_favorable_pct": round(max_favorable, 4),
                    "max_adverse_pct": round(max_adverse, 4),
                    "regime": bt_regime,
                    "track": track,
                })
                history[ticker][tf][side] = history[ticker][tf][side][-(_cfg.SIGNAL_HISTORY_LIMIT * 3):]
                signals_found += 1

        save_signals_history(history)
        logger.info(f"[BACKTEST] {ticker} {tf}: found {signals_found} historical signals")
        return signals_found

    except Exception as e:
        logger.error(f"[BACKTEST] Failed for {ticker} {tf}: {e}", exc_info=True)
        return 0

# =====================================================================
# 🔄  HELPER FUNCTIONS
# =====================================================================

def make_state() -> Dict:
    """Creates the initial state with independent A, U, and B tracks."""
    return {
        "a_in_long": False,
        "a_in_short": False,
        "a_long_bar": None,
        "a_short_bar": None,
        "last_a_long_bar": None,
        "last_a_short_bar": None,
        # 🆕 FIX BUG-LO005: time-based cooldown instead of positional bar_idx
        # (see cooldown_ok in check_signals) — last_a_long_bar/last_a_short_bar
        # above are kept only for debugging/backward compatibility, the
        # cooldown no longer reads them.
        "last_a_long_time": None,
        "last_a_short_time": None,
        # 🆕 Kimi review #1: at most one open_position attempt per A-track
        # per closed bar, even if it was rejected (see _safe_open_position).
        "last_a_long_attempt_bar": None,
        "last_a_short_attempt_bar": None,
        "u_in_long": False,
        "u_in_short": False,
        "u_long_bar": None,
        "u_short_bar": None,
        "last_u_long_bar": None,
        "last_u_short_bar": None,
        "last_u_long_time": None,
        "last_u_short_time": None,
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
        # 🆕 Track "b" (Breakout) — same shape as a_*/u_* above.
        "b_in_long": False,
        "b_in_short": False,
        "b_long_bar": None,
        "b_short_bar": None,
        "last_b_long_bar": None,
        "last_b_short_bar": None,
        "last_b_long_time": None,
        "last_b_short_time": None,
        "b_active_trade": None,
        "b_trade_history": [],
        "b_bars_in_trade": 0,
        "b_last_closure_notified": False,
        "active_trade": None,
        "trade_history": [],
        "bars_in_trade": 0,
        "last_closure_notified": False,
    }

def _ensure_history_slot(history: Dict, ticker: str, tf: str):
    """Creates a history slot if it doesn't exist yet."""
    if ticker not in history:
        history[ticker] = {}
    if tf not in history[ticker]:
        history[ticker][tf] = {"long": [], "short": []}
