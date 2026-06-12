import os
import asyncio
import math
import numpy as np
import pandas as pd
import ccxt
import discord
from discord.ext import tasks, commands
from dotenv import load_dotenv
import json
from datetime import datetime, timezone

load_dotenv()

# =====================================================================
# ⚙️  SETTINGS
# =====================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CHANNEL_NAME  = os.getenv("CHANNEL_NAME", "general")

DEFAULT_TICKERS = ["BTC/USDT", "ETH/USDT"]
TIMEFRAMES      = ["1h", "4h"]
PAIRS_FILE      = "pairs.json"

# =====================================================================
# 💾 DYNAMIC PAIRS LIST
# =====================================================================
def load_tickers() -> list[str]:
    try:
        with open(PAIRS_FILE, "r") as f:
            data = json.load(f)
            return data.get("tickers", DEFAULT_TICKERS)
    except Exception:
        return DEFAULT_TICKERS.copy()

def save_tickers(tickers: list[str]):
    try:
        with open(PAIRS_FILE, "w") as f:
            json.dump({"tickers": tickers}, f)
    except Exception as e:
        print(f"[WARN] Failed to save pairs: {e}")

TICKERS: list[str] = load_tickers()

# =====================================================================
# ⚙️  INDICATOR PARAMETERS
# =====================================================================
ATR_PERIOD      = 14
ATR_MIN         = 0.3
ATR_MAX         = 4.5
CHOP_LENGTH     = 14
CHOP_THRESHOLD  = 61.8
FRAMA_LEN       = 22
FRAMA_MULT      = 2.1
MFI_LEN         = 8
MFI_TRAINING    = 800
AND_LEN         = 23
AND_SIG_LEN     = 6
LOOKBACK        = 3
COOLDOWN_BARS   = 2
UT_SENSITIVITY  = 1.0
UT_PERIOD       = 10
MAX_ALLOWED_LEV = 10
TARGET_RISK_DEP = 5.0

# =====================================================================
# 🎓  ADAPTIVE TP SETTINGS
# =====================================================================
SIGNAL_HISTORY_LIMIT = 25
TP_PERCENTILE        = 0.75
SAFE_TP_PERCENTILE   = 0.50  # Conservative mode — uses median (50th %ile) instead of 75th
MIN_TP_PCT           = 0.3
MAX_TP_PCT           = 8.0
MAX_HOLD_BARS        = 20

# =====================================================================
# 🕯️  HEIKIN ASHI
# =====================================================================
UT_HA_FILE = "ut_ha.json"

def load_ut_ha() -> bool:
    try:
        with open(UT_HA_FILE, "r") as f:
            return json.load(f).get("ut_heikin_ashi", False)
    except Exception:
        return False

def save_ut_ha(enabled: bool):
    try:
        with open(UT_HA_FILE, "w") as f:
            json.dump({"ut_heikin_ashi": enabled}, f)
    except Exception as e:
        print(f"[WARN] Failed to save UT HA setting: {e}")

UT_HEIKIN_ASHI: bool = load_ut_ha()

# =====================================================================
# 🧬  HTF BIAS
# =====================================================================
HTF_FILE = "htf_bias.json"

def load_htf() -> str:
    try:
        with open(HTF_FILE, "r") as f:
            return json.load(f).get("htf", "1d")
    except Exception:
        return "1d"

def save_htf(htf: str):
    try:
        with open(HTF_FILE, "w") as f:
            json.dump({"htf": htf}, f)
    except Exception as e:
        print(f"[WARN] Failed to save HTF setting: {e}")

HTF_BIAS: str = load_htf()

# HTF cache: {ticker: (bias, timestamp)}
_htf_cache: dict[str, tuple[int, datetime]] = {}
HTF_CACHE_TTL_SECONDS = 300  # 5 minutes

# =====================================================================
# 📡  MARKET MODE
# =====================================================================
MODE_FILE = "mode.json"

def load_mode() -> str:
    try:
        with open(MODE_FILE, "r") as f:
            return json.load(f).get("mode", "spot")
    except Exception:
        return "spot"

def save_mode(mode: str):
    try:
        with open(MODE_FILE, "w") as f:
            json.dump({"mode": mode}, f)
    except Exception as e:
        print(f"[WARN] Failed to save mode: {e}")

def make_exchange(mode: str) -> ccxt.gate:
    if mode == "futures":
        return ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    return ccxt.gate({"enableRateLimit": True})

MARKET_MODE: str = load_mode()
exchange = make_exchange(MARKET_MODE)

# =====================================================================
# 📚  SIGNALS HISTORY — in-memory cache + JSON persistence
# =====================================================================
SIGNALS_HISTORY_FILE = "signals_history.json"
_signals_history_cache: dict = {}

def load_signals_history() -> dict:
    global _signals_history_cache
    if _signals_history_cache:
        return _signals_history_cache
    try:
        with open(SIGNALS_HISTORY_FILE, "r") as f:
            _signals_history_cache = json.load(f)
            return _signals_history_cache
    except Exception:
        _signals_history_cache = {}
        return _signals_history_cache

def save_signals_history(history: dict):
    global _signals_history_cache
    _signals_history_cache = history
    try:
        with open(SIGNALS_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"[WARN] Failed to save signals history: {e}")

def _ensure_history_slot(history: dict, ticker: str, tf: str):
    if ticker not in history:
        history[ticker] = {}
    if tf not in history[ticker]:
        history[ticker][tf] = {"long": [], "short": []}

def add_signal_record(ticker: str, tf: str, side: str, entry: float, timestamp: str):
    history = load_signals_history()
    _ensure_history_slot(history, ticker, tf)
    record = {
        "entry": round(entry, 4),
        "exit": None,
        "exit_type": "open",
        "bars_held": 0,
        "moved_pct": 0.0,
        "timestamp": timestamp,
        "max_favorable_pct": 0.0,
        "max_adverse_pct": 0.0,
    }
    history[ticker][tf][side].append(record)
    history[ticker][tf][side] = history[ticker][tf][side][-(SIGNAL_HISTORY_LIMIT * 3):]
    save_signals_history(history)
    print(f"[SIGNAL_RECORD] ADDED {side} signal for {ticker} {tf} @ {entry}")

def update_signal_record(ticker: str, tf: str, side: str, exit_price: float, exit_type: str, bars_held: int):
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        print(f"[WARN] Cannot update signal: no history for {ticker} {tf}")
        return
    records = history[ticker][tf][side]
    for rec in reversed(records):
        if rec["exit_type"] == "open":
            rec["exit"] = round(exit_price, 4)
            rec["exit_type"] = exit_type
            rec["bars_held"] = bars_held
            entry = rec["entry"]
            if side == "long":
                rec["moved_pct"] = round((exit_price - entry) / entry * 100, 4)
            else:
                rec["moved_pct"] = round((entry - exit_price) / entry * 100, 4)
            save_signals_history(history)
            print(f"[SIGNAL_RECORD] CLOSED {side} signal for {ticker} {tf} | Entry:{entry} Exit:{exit_price} Result:{exit_type} PnL:{rec['moved_pct']:.2f}%")
            return
    print(f"[WARN] No open signal found to close for {ticker} {tf} {side}")

def update_signal_mae_mfe(ticker: str, tf: str, side: str, current_price: float):
    """Update max favorable/adverse excursion — uses in-memory cache, no disk I/O."""
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        return
    records = history[ticker][tf][side]
    for rec in reversed(records):
        if rec["exit_type"] == "open":
            entry = rec["entry"]
            if side == "long":
                favorable = (current_price - entry) / entry * 100
                adverse   = (entry - current_price) / entry * 100
            else:
                favorable = (entry - current_price) / entry * 100
                adverse   = (current_price - entry) / entry * 100
            rec["max_favorable_pct"] = round(max(float(rec.get("max_favorable_pct", 0)), favorable), 4)
            rec["max_adverse_pct"]   = round(max(float(rec.get("max_adverse_pct",   0)), adverse),   4)
            # Update cache but don't write to disk every tick — write only on close
            return

def calculate_adaptive_tp(ticker: str, tf: str, side: str, entry: float, current_sl: float, use_safe: bool = False) -> float:
    """Calculate adaptive TP based on historical signal MFE percentile.

    Args:
        use_safe: If True, uses SAFE_TP_PERCENTILE (50th / median) for conservative TP.
                 If False, uses TP_PERCENTILE (75th) for aggressive TP.
    """
    history = load_signals_history()
    risk = abs(entry - current_sl)
    fallback_tp = entry + (2.0 * risk) if side == "long" else entry - (2.0 * risk)

    if ticker not in history or tf not in history[ticker]:
        print(f"[ADAPTIVE_TP] {ticker} {tf} {side} | NO HISTORY | fallback TP={fallback_tp:.4f}")
        return round(fallback_tp, 4)

    records = history[ticker][tf][side]
    closed  = [r for r in records if r["exit_type"] in ("tp", "sl", "cancelled")]

    if len(closed) < 3:
        print(f"[ADAPTIVE_TP] {ticker} {tf} {side} | Only {len(closed)} closed signals | fallback TP={fallback_tp:.4f}")
        return round(fallback_tp, 4)

    recent = closed[-SIGNAL_HISTORY_LIMIT:]
    favorable_pcts = []
    for r in recent:
        mfe = r.get("max_favorable_pct", 0)
        if mfe == 0 and r.get("moved_pct", 0) != 0:
            mfe = abs(r["moved_pct"])
        favorable_pcts.append(max(mfe, 0.1))

    pct = SAFE_TP_PERCENTILE if use_safe else TP_PERCENTILE
    tp_pct = float(np.percentile(favorable_pcts, pct * 100))
    tp_pct = max(MIN_TP_PCT, min(MAX_TP_PCT, tp_pct))

    tp = entry * (1 + tp_pct / 100) if side == "long" else entry * (1 - tp_pct / 100)
    mode = "SAFE" if use_safe else "AGGR"
    print(f"[ADAPTIVE_TP] {ticker} {tf} {side} | mode={mode} | entry={entry:.4f} | history={len(recent)} | tp_pct={tp_pct:.2f}% | TP={tp:.4f}")
    return round(tp, 4)

def calculate_combined_tp(ticker: str, tf: str, side: str, entry: float, sl: float,
                          df: pd.DataFrame, idx: int, atr14: pd.Series, use_safe: bool = False) -> tuple[float, str]:
    """
    Combined TP: adaptive history-based (primary) with R:R fallback.
    Returns (tp_price, description_string).
    """
    stats = get_signal_stats(ticker, tf, side)
    tp = calculate_adaptive_tp(ticker, tf, side, entry, sl, use_safe=use_safe)
    risk = abs(entry - sl)
    rr   = round(abs(tp - entry) / max(risk, 1e-8), 2)

    pct = SAFE_TP_PERCENTILE if use_safe else TP_PERCENTILE
    if stats["count"] >= 5:
        desc = f"📚 Adaptive {pct*100:.0f}th %ile | {stats['count']} signals | Avg MFE {stats['avg_mfe']:.2f}% | Win Rate: {stats['win_rate']:.1f}%"
    else:
        desc = f"📐 Fallback R:R 2.0 (only {stats['count']} signals in history)"

    return tp, desc

def get_signal_stats(ticker: str, tf: str, side: str) -> dict:
    history = load_signals_history()
    empty   = {"count": 0, "avg_mfe": 0, "median_mfe": 0, "tp_pct": 0, "best": 0, "worst": 0, "mean_mfe": 0, "std_mfe": 0,
                 "tp_hits": 0, "sl_hits": 0, "cancelled": 0, "win_rate": 0, "avg_pnl": 0}
    if ticker not in history or tf not in history[ticker]:
        return empty
    records = history[ticker][tf][side]
    closed  = [r for r in records if r["exit_type"] in ("tp", "sl", "cancelled")]
    if not closed:
        return empty
    recent = closed[-SIGNAL_HISTORY_LIMIT:]
    favorable_pcts = []
    pnls = []
    tp_hits = sl_hits = cancelled = wins = 0
    for r in recent:
        mfe = r.get("max_favorable_pct", 0)
        if mfe == 0 and r.get("moved_pct", 0) != 0:
            mfe = abs(r["moved_pct"])
        favorable_pcts.append(max(mfe, 0.1))
        pnls.append(r.get("moved_pct", 0))
        et = r.get("exit_type", "")
        if et == "tp": tp_hits += 1
        elif et == "sl": sl_hits += 1
        elif et == "cancelled": cancelled += 1
        if r.get("moved_pct", 0) > 0:
            wins += 1
    count = len(recent)
    return {
        "count":      count,
        "avg_mfe":    round(float(np.mean(favorable_pcts)), 2),
        "median_mfe": round(float(np.median(favorable_pcts)), 2),
        "tp_pct":     round(float(np.mean(favorable_pcts) + 0.5 * np.std(favorable_pcts)), 2),
        "mean_mfe":   round(float(np.mean(favorable_pcts)), 2),
        "std_mfe":    round(float(np.std(favorable_pcts)), 2),
        "best":       round(float(max(favorable_pcts)), 2),
        "worst":      round(float(min(favorable_pcts)), 2),
        "tp_hits":    tp_hits,
        "sl_hits":    sl_hits,
        "cancelled":  cancelled,
        "win_rate":   round(wins / count * 100, 1) if count > 0 else 0,
        "avg_pnl":    round(float(np.mean(pnls)), 2) if pnls else 0,
    }

# =====================================================================
# 🔙  BACKTEST — populate signal history from historical bars
# =====================================================================
def backtest_history(ticker: str, tf: str, num_bars: int = 3000) -> int:
    print(f"[BACKTEST] Starting {ticker} {tf} ({num_bars} bars)...")
    try:
        bars = exchange.fetch_ohlcv(ticker, tf, limit=num_bars)
        df   = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        if len(df) < 100:
            print(f"[BACKTEST] Not enough bars for {ticker} {tf}")
            return 0

        atr14            = calculate_atr(df, ATR_PERIOD)
        atr_pct          = (atr14 / df["close"]) * 100
        chop             = calculate_chop(df, CHOP_LENGTH)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        mfi              = calculate_mfi(df, MFI_LEN)
        level_os, level_ob = run_kmeans_mfi(mfi, MFI_TRAINING)
        and_osc, and_sig = calculate_andean(df, AND_LEN, AND_SIG_LEN)
        ut_buy, ut_sell  = calculate_ut_bot(df, UT_SENSITIVITY, UT_PERIOD, use_ha=UT_HEIKIN_ASHI)

        signals_found = 0
        history = load_signals_history()

        def xover(s, lvl, i):
            return float(s.iloc[i]) > lvl and float(s.iloc[i-1]) <= lvl
        def xunder(s, lvl, i):
            return float(s.iloc[i]) < lvl and float(s.iloc[i-1]) >= lvl
        def xover2(s1, s2, i):
            return float(s1.iloc[i]) > float(s2.iloc[i]) and float(s1.iloc[i-1]) <= float(s2.iloc[i-1])
        def xunder2(s1, s2, i):
            return float(s1.iloc[i]) < float(s2.iloc[i]) and float(s1.iloc[i-1]) >= float(s2.iloc[i-1])

        for idx in range(50, len(df) - 100):
            close_v   = float(df["close"].iloc[idx])
            open_v    = float(df["open"].iloc[idx])
            atr_v     = max(float(atr14.iloc[idx]), 1e-8)
            atr_pct_v = float(atr_pct.iloc[idx])
            chop_v    = float(chop.iloc[idx])

            atr_ok  = ATR_MIN <= atr_pct_v <= ATR_MAX
            chop_ok = chop_v < CHOP_THRESHOLD

            frama_slope = float(fs.iloc[idx]) - float(fs.iloc[idx - 1])
            slope_long  = frama_slope > 0
            slope_short = frama_slope < 0

            frama_dir_v = int(fdir.iloc[idx])
            frama_bull  = frama_dir_v == 1
            frama_bear  = frama_dir_v == -1

            hh10_prev = float(df["high"].iloc[max(0, idx-10):idx].max())
            ll10_prev = float(df["low"].iloc[max(0, idx-10):idx].min())
            fake_break_long  = float(df["high"].iloc[idx]) > hh10_prev and close_v < hh10_prev
            fake_break_short = float(df["low"].iloc[idx])  < ll10_prev and close_v > ll10_prev

            ll5_prev = float(df["low"].iloc[max(0, idx-5):idx].min())
            hh5_prev = float(df["high"].iloc[max(0, idx-5):idx].max())
            liq_sweep_long  = float(df["low"].iloc[idx])  < ll5_prev and close_v > ll5_prev and close_v > open_v
            liq_sweep_short = float(df["high"].iloc[idx]) > hh5_prev and close_v < hh5_prev and close_v < open_v

            # HTF skipped in backtest (assume aligned)
            filter_long  = frama_bull and chop_ok and atr_ok and slope_long  and not fake_break_long  and not liq_sweep_short
            filter_short = frama_bear and chop_ok and atr_ok and slope_short and not fake_break_short and not liq_sweep_long

            sig_a_long  = xover(mfi,  level_os, idx) and filter_long
            sig_a_short = xunder(mfi, level_ob, idx) and filter_short
            sig_u_long  = bool(ut_buy.iloc[idx])  and filter_long
            sig_u_short = bool(ut_sell.iloc[idx]) and filter_short

            for side, sig_ok in [("long", sig_a_long or sig_u_long), ("short", sig_a_short or sig_u_short)]:
                if not sig_ok:
                    continue

                sl = calculate_sl(close_v, side, fs, fu, fl, atr14, idx)

                # FIX #5: Use the same adaptive TP logic as live trading (no lookahead bias)
                tp = calculate_adaptive_tp(ticker, tf, side, close_v, sl)

                tp_hit = sl_hit = False
                max_favorable = max_adverse = 0.0
                exit_price = close_v
                bars_held  = 0

                for future_idx in range(idx + 1, min(idx + 101, len(df))):
                    fh = float(df["high"].iloc[future_idx])
                    fl_ = float(df["low"].iloc[future_idx])

                    if side == "long":
                        favorable = (fh - close_v) / close_v * 100
                        adverse   = (close_v - fl_) / close_v * 100
                        max_favorable = max(max_favorable, favorable)
                        max_adverse   = max(max_adverse,   adverse)
                        if fl_ <= sl:
                            sl_hit = True; exit_price = sl; bars_held = future_idx - idx; break
                        if fh >= tp:
                            tp_hit = True; exit_price = tp; bars_held = future_idx - idx; break
                    else:
                        favorable = (close_v - fl_) / close_v * 100
                        adverse   = (fh - close_v) / close_v * 100
                        max_favorable = max(max_favorable, favorable)
                        max_adverse   = max(max_adverse,   adverse)
                        if fh >= sl:
                            sl_hit = True; exit_price = sl; bars_held = future_idx - idx; break
                        if fl_ <= tp:
                            tp_hit = True; exit_price = tp; bars_held = future_idx - idx; break
                    bars_held = future_idx - idx

                _ensure_history_slot(history, ticker, tf)
                exit_type = "tp" if tp_hit else "sl" if sl_hit else "cancelled"
                moved_pct = (exit_price - close_v) / close_v * 100 if side == "long" else (close_v - exit_price) / close_v * 100

                history[ticker][tf][side].append({
                    "entry":             round(close_v,       4),
                    "exit":              round(exit_price,    4),
                    "exit_type":         exit_type,
                    "bars_held":         bars_held,
                    "moved_pct":         round(moved_pct,     4),
                    "timestamp":         str(int(df["timestamp"].iloc[idx])),
                    "max_favorable_pct": round(max_favorable, 4),
                    "max_adverse_pct":   round(max_adverse,   4),
                })
                history[ticker][tf][side] = history[ticker][tf][side][-(SIGNAL_HISTORY_LIMIT * 3):]
                signals_found += 1

        save_signals_history(history)
        print(f"[BACKTEST] {ticker} {tf}: found {signals_found} historical signals")
        return signals_found

    except Exception as e:
        print(f"[ERROR] Backtest failed for {ticker} {tf}: {e}")
        import traceback; traceback.print_exc()
        return 0

def run_startup_backtest():
    print("=" * 60)
    print("[STARTUP] Running historical backtest to populate signal history...")
    print("=" * 60)
    total = 0
    import time
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            count = backtest_history(ticker, tf, num_bars=3000)
            total += count
            time.sleep(0.5)
    print("=" * 60)
    print(f"[STARTUP] Backtest complete! Total historical signals: {total}")
    print("=" * 60)

# =====================================================================
# 🗂️  STATE
# =====================================================================
def make_state():
    return {
        "a_in_long":               False,
        "a_in_short":              False,
        "a_long_bar":              None,
        "a_short_bar":             None,
        "u_in_long":               False,
        "u_in_short":              False,
        "u_long_bar":              None,
        "u_short_bar":             None,
        "last_a_long_bar":         None,
        "last_a_short_bar":        None,
        "last_u_long_bar":         None,
        "last_u_short_bar":        None,
        "last_bar_time":           None,
        "last_processed_bar_time": None,
        "active_trade":            None,
        "trade_history":           [],
        "bars_in_trade":           0,
        "last_closure_notified":   False,  # FIX #3: prevent duplicate closure notifications
    }

state: dict = {ticker: {tf: make_state() for tf in TIMEFRAMES} for ticker in TICKERS}

scan_stats = {"total_scans": 0, "signals_generated": 0, "last_scan_time": None}

def ensure_state(ticker: str):
    if ticker not in state:
        state[ticker] = {tf: make_state() for tf in TIMEFRAMES}

# =====================================================================
# 📊  INDICATORS
# =====================================================================
def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calculate_chop(df: pd.DataFrame, length: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr_sum = tr.rolling(window=length).sum()
    hh = df["high"].rolling(window=length).max()
    ll = df["low"].rolling(window=length).min()
    # FIX #8: stronger protection against division by zero
    return 100 * np.log10(atr_sum / (hh - ll + 1e-12)) / np.log10(length)

def calculate_frama(df: pd.DataFrame, length: int = 22, mult: float = 2.1):
    n   = int(length / 2)
    hh1 = df["high"].rolling(window=n).max()
    ll1 = df["low"].rolling(window=n).min()
    n1  = (hh1 - ll1) / n
    hh2 = df["high"].shift(n).rolling(window=n).max()
    ll2 = df["low"].shift(n).rolling(window=n).min()
    n2  = (hh2 - ll2) / n
    hh3 = df["high"].rolling(window=length).max()
    ll3 = df["low"].rolling(window=length).min()
    n3  = (hh3 - ll3) / length

    with np.errstate(divide="ignore", invalid="ignore"):
        dimen = np.where(
            (n1 > 0) & (n2 > 0) & (n3 > 0),
            (np.log(n1 + n2 + 1e-8) - np.log(n3 + 1e-8)) / np.log(2.0),
            0.0,
        )
    alpha = np.clip(np.exp(-4.6 * (dimen - 1.0)), 0.01, 1.0)

    close    = df["close"].values
    frama_ma = np.zeros(len(df))
    frama_ma[0] = close[0]
    for i in range(1, len(df)):
        frama_ma[i] = alpha[i] * close[i] + (1.0 - alpha[i]) * frama_ma[i - 1]

    fs   = pd.Series(frama_ma, index=df.index)
    hl   = df["high"] - df["low"]
    hc   = (df["high"] - df["close"].shift()).abs()
    lc   = (df["low"]  - df["close"].shift()).abs()
    tr   = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    fatr = tr.rolling(window=length).mean()
    fu   = fs + fatr * mult
    fl   = fs - fatr * mult

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
    hlc3  = (df["high"] + df["low"] + df["close"]) / 3.0
    mf    = hlc3 * df["volume"]
    pos   = np.where(hlc3 > hlc3.shift(1), mf, 0.0)
    neg   = np.where(hlc3 < hlc3.shift(1), mf, 0.0)
    pos_s = pd.Series(pos, index=df.index).rolling(window=length).sum()
    neg_s = pd.Series(neg, index=df.index).rolling(window=length).sum()
    ratio = pos_s / (neg_s + 1e-8)
    return 100.0 - (100.0 / (1.0 + ratio))

def run_kmeans_mfi(mfi: pd.Series, training_size: int = 800):
    vals = mfi.dropna().tail(training_size).values
    if len(vals) < 2:
        return 20.0, 80.0
    c1, c2 = float(vals.min()), float(vals.max())
    for _ in range(10):
        d1 = np.abs(vals - c1)
        d2 = np.abs(vals - c2)
        # FIX #9: handle empty clusters — assign to nearest if tied
        cl1 = vals[d1 < d2]
        cl2 = vals[d2 <= d1]
        if len(cl1) == 0 and len(cl2) == 0:
            break
        if len(cl1) == 0:
            c1 = float(vals.mean())
        else:
            c1 = float(cl1.mean())
        if len(cl2) == 0:
            c2 = float(vals.mean())
        else:
            c2 = float(cl2.mean())
    return min(c1, c2), max(c1, c2)

def calculate_andean(df: pd.DataFrame, length: int = 23, sig_len: int = 6):
    alpha = 2.0 / (length + 1)
    c = df["close"].values
    o = df["open"].values
    u1 = np.zeros(len(df)); u2 = np.zeros(len(df))
    l1 = np.zeros(len(df)); l2 = np.zeros(len(df))
    u1[0] = c[0]; u2[0] = c[0]**2
    l1[0] = c[0]; l2[0] = c[0]**2
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

def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    ha       = df.copy()
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open  = pd.Series(index=df.index, dtype=float)
    ha_open.iloc[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2.0
    ha["open"]  = ha_open
    ha["high"]  = pd.concat([df["high"], ha_open, ha_close], axis=1).max(axis=1)
    ha["low"]   = pd.concat([df["low"],  ha_open, ha_close], axis=1).min(axis=1)
    ha["close"] = ha_close
    return ha

def calculate_ut_bot(df: pd.DataFrame, sensitivity: float = 1.0, period: int = 10, use_ha: bool = False):
    df_ut  = heikin_ashi(df) if use_ha else df.copy()
    src    = df_ut["close"].values
    n_loss = (sensitivity * calculate_atr(df_ut, period)).values
    # FIX #1: use len(df_ut) instead of len(df)
    ts     = np.zeros(len(df_ut))
    ts[0]  = src[0]
    for i in range(1, len(df_ut)):
        prev = ts[i-1]
        if src[i] > prev and src[i-1] > prev:
            ts[i] = max(prev, src[i] - n_loss[i])
        elif src[i] < prev and src[i-1] < prev:
            ts[i] = min(prev, src[i] + n_loss[i])
        else:
            ts[i] = src[i] - n_loss[i] if src[i] > prev else src[i] + n_loss[i]
    ts_s    = pd.Series(ts, index=df.index)
    src_s   = df_ut["close"]
    ut_buy  = (src_s > ts_s) & (src_s.shift(1) <= ts_s.shift(1))
    ut_sell = (src_s < ts_s) & (src_s.shift(1) >= ts_s.shift(1))
    return ut_buy, ut_sell

def get_htf_bias(ticker: str, timeframe: str) -> int:
    """FIX #12: Cache HTF bias for HTF_CACHE_TTL_SECONDS to reduce API calls."""
    global _htf_cache
    now = datetime.now(timezone.utc)
    if ticker in _htf_cache:
        bias, cached_at = _htf_cache[ticker]
        if (now - cached_at).total_seconds() < HTF_CACHE_TTL_SECONDS:
            return bias

    htf = HTF_BIAS
    try:
        bars   = exchange.fetch_ohlcv(ticker, htf, limit=150)
        df_htf = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        fs, fu, fl, fdir = calculate_frama(df_htf, FRAMA_LEN, FRAMA_MULT)
        htf_close = df_htf["close"].iloc[-2]
        htf_frama = fs.iloc[-2]
        bias = 1 if htf_close > htf_frama else -1
        _htf_cache[ticker] = (bias, now)
        print(f"[HTF] {ticker} -> {htf} | close={htf_close:.2f} frama={htf_frama:.2f} bias={'BULL' if bias==1 else 'BEAR'}")
        return bias
    except Exception as e:
        print(f"[WARN] HTF Bias ({ticker} {htf}): {e}")
        return 0

# =====================================================================
# 🎯  SL & TP CALCULATION
# =====================================================================
def calculate_sl(entry_price: float, side: str, fs: pd.Series, fu: pd.Series,
                 fl: pd.Series, atr14: pd.Series, idx: int) -> float:
    atr_v = max(float(atr14.iloc[idx]), 1e-8)
    if side == "long":
        sl_frama = float(fl.iloc[idx])
        sl_atr   = entry_price - 1.5 * atr_v
        return max(sl_frama, sl_atr)   # tighter SL wins (closer to price)
    else:
        sl_frama = float(fu.iloc[idx])
        sl_atr   = entry_price + 1.5 * atr_v
        return min(sl_frama, sl_atr)

def check_tp_sl_hit(st: dict, high: float, low: float) -> str | None:
    trade = st.get("active_trade")
    if not trade:
        return None
    side = trade["side"]
    sl   = trade["sl"]
    tp   = trade["tp"]
    if side == "long":
        if low  <= sl: return "sl"
        if high >= tp: return "tp"
    else:
        if high >= sl: return "sl"
        if low  <= tp: return "tp"
    return None

def close_trade(st: dict, exit_price: float, result: str, ticker: str, tf: str):
    trade = st.get("active_trade")
    if not trade:
        return None
    entry      = trade["entry"]
    side       = trade["side"]
    bars_held  = st.get("bars_in_trade", 0)
    pnl_pct    = (exit_price - entry) / entry * 100 if side == "long" else (entry - exit_price) / entry * 100
    closed_trade = {
        "side":      side,
        "entry":     entry,
        "sl":        trade["sl"],
        "tp":        trade["tp"],
        "exit":      exit_price,
        "exit_time": datetime.now(timezone.utc).isoformat(),
        "result":    result,
        "pnl_pct":   round(pnl_pct, 4),
        "bars_held": bars_held,
        "lev":       trade.get("lev", 1),
    }
    st["trade_history"].append(closed_trade)
    st["trade_history"] = st["trade_history"][-50:]
    update_signal_record(ticker, tf, side, exit_price, result, bars_held)
    st["active_trade"]  = None
    st["bars_in_trade"] = 0
    st["last_closure_notified"] = False  # FIX #3: reset notification flag
    print(f"[TRADE] Closed {side.upper()} | Entry:{entry} | Exit:{exit_price} | Result:{result.upper()} | PnL:{pnl_pct:.2f}% | Bars:{bars_held}")
    return closed_trade

# =====================================================================
# 🧠  SIGNAL LOGIC
# =====================================================================
def check_signals(ticker: str, timeframe: str, st: dict):
    scan_stats["total_scans"] += 1
    scan_stats["last_scan_time"] = datetime.now(timezone.utc).isoformat()
    try:
        htf_bias = get_htf_bias(ticker, timeframe)
        bars = exchange.fetch_ohlcv(ticker, timeframe, limit=900)
        df   = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])

        current_bar_time = int(df["timestamp"].iloc[-2])
        is_new_bar = st["last_processed_bar_time"] is None or current_bar_time > st["last_processed_bar_time"]
        st["last_processed_bar_time"] = current_bar_time

        last_high  = float(df["high"].iloc[-2])
        last_low   = float(df["low"].iloc[-2])
        last_close = float(df["close"].iloc[-2])

        # Check active trade TP/SL on last confirmed bar
        trade = st.get("active_trade")
        if trade:
            update_signal_mae_mfe(ticker, timeframe, trade["side"], last_close)
            # FIX #13: only increment bars_in_trade if trade remains open after this bar
            hit = check_tp_sl_hit(st, last_high, last_low)
            if hit:
                exit_price = trade["sl"] if hit == "sl" else trade["tp"]
                close_trade(st, exit_price, hit, ticker, timeframe)
            elif st.get("bars_in_trade", 0) >= MAX_HOLD_BARS:
                close_trade(st, last_close, "cancelled", ticker, timeframe)
                print(f"[TRADE] Force-closed {trade['side'].upper()} after {MAX_HOLD_BARS} bars")
            else:
                st["bars_in_trade"] = st.get("bars_in_trade", 0) + 1

        # Indicators
        atr14            = calculate_atr(df, ATR_PERIOD)
        atr_pct          = (atr14 / df["close"]) * 100
        chop             = calculate_chop(df, CHOP_LENGTH)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        mfi              = calculate_mfi(df, MFI_LEN)
        level_os, level_ob = run_kmeans_mfi(mfi, MFI_TRAINING)
        and_osc, and_sig = calculate_andean(df, AND_LEN, AND_SIG_LEN)
        ut_buy, ut_sell  = calculate_ut_bot(df, UT_SENSITIVITY, UT_PERIOD, use_ha=UT_HEIKIN_ASHI)

        idx      = len(df) - 2
        bar_idx  = idx
        bar_time = int(df["timestamp"].iloc[idx])

        close_v   = float(df["close"].iloc[idx])
        open_v    = float(df["open"].iloc[idx])
        atr_v     = max(float(atr14.iloc[idx]), 1e-8)
        atr_pct_v = float(atr_pct.iloc[idx])
        chop_v    = float(chop.iloc[idx])

        atr_ok  = ATR_MIN <= atr_pct_v <= ATR_MAX
        chop_ok = chop_v < CHOP_THRESHOLD

        frama_slope = float(fs.iloc[idx]) - float(fs.iloc[idx - 1])
        slope_long  = frama_slope > 0
        slope_short = frama_slope < 0

        frama_dir_v = int(fdir.iloc[idx])
        frama_bull  = frama_dir_v == 1
        frama_bear  = frama_dir_v == -1

        htf_bull = htf_bias == 1
        htf_bear = htf_bias == -1

        hh10_prev = float(df["high"].iloc[max(0, idx-10):idx].max())
        ll10_prev = float(df["low"].iloc[max(0, idx-10):idx].min())
        fake_break_long  = float(df["high"].iloc[idx]) > hh10_prev and close_v < hh10_prev
        fake_break_short = float(df["low"].iloc[idx])  < ll10_prev and close_v > ll10_prev

        ll5_prev = float(df["low"].iloc[max(0, idx-5):idx].min())
        hh5_prev = float(df["high"].iloc[max(0, idx-5):idx].max())
        liq_sweep_long  = float(df["low"].iloc[idx])  < ll5_prev and close_v > ll5_prev and close_v > open_v
        liq_sweep_short = float(df["high"].iloc[idx]) > hh5_prev and close_v < hh5_prev and close_v < open_v

        filter_long = (
            frama_bull and chop_ok and atr_ok and slope_long
            and htf_bull
            and not fake_break_long
            and not liq_sweep_short
        )
        filter_short = (
            frama_bear and chop_ok and atr_ok and slope_short
            and htf_bear
            and not fake_break_short
            and not liq_sweep_long
        )

        ha_status = "HA" if UT_HEIKIN_ASHI else "Normal"
        print(f"[DEBUG] {ticker} {timeframe} | UT:{ha_status} | "
              f"frama={'BULL' if frama_bull else 'BEAR' if frama_bear else 'RANGE'} | "
              f"htf={'BULL' if htf_bull else 'BEAR'} | "
              f"slope_L={slope_long} slope_S={slope_short} | "
              f"chop_ok={chop_ok}({chop_v:.1f}) | atr_ok={atr_ok}({atr_pct_v:.2f}%) | "
              f"fake_L={fake_break_long} liq_S={liq_sweep_short} | "
              f"filter_L={filter_long} filter_S={filter_short} | "
              f"new_bar={is_new_bar}")

        def crossover(s, lvl, i):
            return float(s.iloc[i]) > lvl and float(s.iloc[i-1]) <= lvl
        def crossunder(s, lvl, i):
            return float(s.iloc[i]) < lvl and float(s.iloc[i-1]) >= lvl
        def crossover2(s1, s2, i):
            return float(s1.iloc[i]) > float(s2.iloc[i]) and float(s1.iloc[i-1]) <= float(s2.iloc[i-1])
        def crossunder2(s1, s2, i):
            return float(s1.iloc[i]) < float(s2.iloc[i]) and float(s1.iloc[i-1]) >= float(s2.iloc[i-1])

        mfi_bull_sig = crossover(mfi,  level_os, idx)
        mfi_bear_sig = crossunder(mfi, level_ob, idx)
        and_bull_sig = crossover2(and_osc, and_sig, idx)
        and_bear_sig = crossunder2(and_osc, and_sig, idx)

        def bars_since_crossover(s, lvl, cur):
            for k in range(cur, max(cur - LOOKBACK - 1, 0), -1):
                if float(s.iloc[k]) > lvl and float(s.iloc[k-1]) <= lvl:
                    return cur - k
            return 999
        def bars_since_crossunder(s, lvl, cur):
            for k in range(cur, max(cur - LOOKBACK - 1, 0), -1):
                if float(s.iloc[k]) < lvl and float(s.iloc[k-1]) >= lvl:
                    return cur - k
            return 999
        def bars_since_crossover2(s1, s2, cur):
            for k in range(cur, max(cur - LOOKBACK - 1, 0), -1):
                if float(s1.iloc[k]) > float(s2.iloc[k]) and float(s1.iloc[k-1]) <= float(s2.iloc[k-1]):
                    return cur - k
            return 999
        def bars_since_crossunder2(s1, s2, cur):
            for k in range(cur, max(cur - LOOKBACK - 1, 0), -1):
                if float(s1.iloc[k]) < float(s2.iloc[k]) and float(s1.iloc[k-1]) >= float(s2.iloc[k-1]):
                    return cur - k
            return 999

        bs_and_bull = bars_since_crossover2(and_osc, and_sig, idx)
        bs_mfi_bull = bars_since_crossover(mfi, level_os, idx)
        bs_and_bear = bars_since_crossunder2(and_osc, and_sig, idx)
        bs_mfi_bear = bars_since_crossunder(mfi, level_ob, idx)

        confirm_long_a  = (mfi_bull_sig and bs_and_bull <= LOOKBACK) or (and_bull_sig and bs_mfi_bull <= LOOKBACK)
        confirm_short_a = (mfi_bear_sig and bs_and_bear <= LOOKBACK) or (and_bear_sig and bs_mfi_bear <= LOOKBACK)

        def cooldown_ok(last_bar):
            return last_bar is None or (bar_idx - last_bar) > COOLDOWN_BARS

        a_long_cd_ok  = cooldown_ok(st["last_a_long_bar"])
        a_short_cd_ok = cooldown_ok(st["last_a_short_bar"])
        u_long_cd_ok  = cooldown_ok(st["last_u_long_bar"])
        u_short_cd_ok = cooldown_ok(st["last_u_short_bar"])

        a_in_pos = st["a_in_long"] or st["a_in_short"]
        u_in_pos = st["u_in_long"] or st["u_in_short"]

        sig_a_long  = confirm_long_a  and filter_long  and not a_in_pos and a_long_cd_ok
        sig_a_short = confirm_short_a and filter_short and not a_in_pos and a_short_cd_ok
        sig_u_long  = bool(ut_buy.iloc[idx])  and filter_long  and not u_in_pos and u_long_cd_ok  and is_new_bar
        sig_u_short = bool(ut_sell.iloc[idx]) and filter_short and not u_in_pos and u_short_cd_ok and is_new_bar

        if sig_a_long:
            st["a_in_long"] = True;  st["a_in_short"] = False
            st["a_long_bar"] = bar_idx; st["last_a_long_bar"] = bar_idx
        if sig_a_short:
            st["a_in_short"] = True; st["a_in_long"] = False
            st["a_short_bar"] = bar_idx; st["last_a_short_bar"] = bar_idx
        if sig_u_long:
            st["u_in_long"] = True;  st["u_in_short"] = False
            st["u_long_bar"] = bar_idx; st["last_u_long_bar"] = bar_idx
        if sig_u_short:
            st["u_in_short"] = True; st["u_in_long"] = False
            st["u_short_bar"] = bar_idx; st["last_u_short_bar"] = bar_idx

        regime = "CHAOS" if atr_pct_v > ATR_MAX else "TREND" if atr_pct_v > ATR_MIN * 1.5 else "NORMAL"

        frama_sl_long  = max(1.0, min(3.5, abs(close_v - float(fl.iloc[idx])) / atr_v))
        frama_sl_short = max(1.0, min(3.5, abs(float(fu.iloc[idx]) - close_v) / atr_v))
        sugg_sl  = frama_sl_long if (sig_a_long or sig_u_long) else frama_sl_short
        sugg_lev = max(1, min(MAX_ALLOWED_LEV, math.floor(TARGET_RISK_DEP / max(sugg_sl, 0.1))))
        if regime == "CHAOS": sugg_lev = max(1, math.floor(sugg_lev * 0.5))
        if regime == "TREND": sugg_lev = min(MAX_ALLOWED_LEV, math.floor(sugg_lev * 1.2))

        def calc_confidence(is_long: bool) -> int:
            score  = 20 if chop_ok else 0
            score += 20 if atr_ok  else 0
            score += 15 if (frama_bull if is_long else frama_bear) else 0
            a_sig  = sig_a_long  if is_long else sig_a_short
            u_sig  = sig_u_long  if is_long else sig_u_short
            score += 25 if (a_sig and u_sig) else 10 if (a_sig or u_sig) else 0
            score += 20 if (htf_bull if is_long else htf_bear) else 0
            return min(score, 100)

        signals      = []
        signal_fired = False

        # --- LONG signals ---
        if sig_a_long or sig_u_long:
            sl = calculate_sl(close_v, "long", fs, fu, fl, atr14, idx)
            tp, tp_desc = calculate_combined_tp(ticker, timeframe, "long", close_v, sl, df, idx, atr14)
            risk = abs(close_v - sl)
            st["active_trade"] = {"side": "long", "entry": close_v, "sl": sl, "tp": tp, "lev": sugg_lev, "bar_opened": bar_idx}
            st["bars_in_trade"] = 0
            add_signal_record(ticker, timeframe, "long", close_v, datetime.now(timezone.utc).isoformat())
            stats = get_signal_stats(ticker, timeframe, "long")
            if sig_a_long:
                signal_fired = True
                signals.append(("A BUY  (Andean+MFI)", close_v, regime, sugg_lev, bar_time, calc_confidence(True),  sl, tp, risk, stats, tp_desc))
            if sig_u_long:
                signal_fired = True
                signals.append(("U BUY  (UT Bot)",     close_v, regime, sugg_lev, bar_time, calc_confidence(True),  sl, tp, risk, stats, tp_desc))

        # --- SHORT signals ---
        if sig_a_short or sig_u_short:
            sl = calculate_sl(close_v, "short", fs, fu, fl, atr14, idx)
            tp, tp_desc = calculate_combined_tp(ticker, timeframe, "short", close_v, sl, df, idx, atr14)
            risk = abs(sl - close_v)
            st["active_trade"] = {"side": "short", "entry": close_v, "sl": sl, "tp": tp, "lev": sugg_lev, "bar_opened": bar_idx}
            st["bars_in_trade"] = 0
            add_signal_record(ticker, timeframe, "short", close_v, datetime.now(timezone.utc).isoformat())
            stats = get_signal_stats(ticker, timeframe, "short")
            if sig_a_short:
                signal_fired = True
                signals.append(("A SELL (Andean+MFI)", close_v, regime, sugg_lev, bar_time, calc_confidence(False), sl, tp, risk, stats, tp_desc))
            if sig_u_short:
                signal_fired = True
                signals.append(("U SELL (UT Bot)",     close_v, regime, sugg_lev, bar_time, calc_confidence(False), sl, tp, risk, stats, tp_desc))

        if signal_fired:
            scan_stats["signals_generated"] += len(signals)
        return signals, bar_time, regime, sugg_lev

    except Exception as e:
        print(f"[ERROR] check_signals({ticker}, {timeframe}): {e}")
        import traceback; traceback.print_exc()
        return [], None, "UNKNOWN", 1

# =====================================================================
# 🤖  DISCORD BOT
# =====================================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def build_embed(ticker, tf, signal_type, price, regime, leverage, confidence,
                sl, tp, risk, stats, tp_desc: str = "") -> discord.Embed:
    is_long    = "BUY" in signal_type
    is_a_track = "Andean" in signal_type
    coin_emoji  = "🟡" if "BTC" in ticker else "🔷" if "ETH" in ticker else "🟣"
    track_emoji = "🔵" if is_a_track else "🟢"
    conf_color  = "🟢" if confidence >= 80 else "🟡" if confidence >= 60 else "🔴"
    mode_label  = "Spot" if MARKET_MODE == "spot" else "Futures"
    ha_label    = "HA" if UT_HEIKIN_ASHI else "Normal"
    rr          = round(abs(tp - price) / max(risk, 1e-8), 2)
    tp_pct      = abs(tp - price) / price * 100
    tp_source   = (f"📚 Adaptive (last {stats['count']} signals, {TP_PERCENTILE*100:.0f}th %ile)"
                   if stats["count"] >= 5 else "📐 Fixed R:R = 2.0 (not enough history)")

    embed = discord.Embed(
        title=f"🚨 MUFCA v3.1 {coin_emoji} {'📈 LONG' if is_long else '📉 SHORT'}",
        color=discord.Color.green() if is_long else discord.Color.red(),
    )
    embed.add_field(name="📈 Pair",         value=f"**{ticker}**",                          inline=True)
    embed.add_field(name="⏱ TF",            value=tf.upper(),                               inline=True)
    embed.add_field(name=f"{track_emoji} Track", value=signal_type.strip(),                 inline=True)
    embed.add_field(name="🧬 HTF Bias",     value=f"✅ {HTF_BIAS.upper()} FRAMA confirmed", inline=True)
    embed.add_field(name="💵 Entry",         value=f"${price:,.4f}",                        inline=True)
    embed.add_field(name="🛑 Stop Loss",     value=f"${sl:,.4f}",                           inline=True)
    embed.add_field(name="🎯 Take Profit",   value=f"${tp:,.4f} (+{tp_pct:.2f}%)",          inline=True)
    embed.add_field(name="📊 Risk/Reward",   value=f"1:{rr}",                               inline=True)
    embed.add_field(name="⚙️ Regime",        value=regime,                                  inline=True)
    embed.add_field(name="⚠️ Leverage",      value=f"x{leverage}",                          inline=True)
    embed.add_field(name=f"{conf_color} AI Conf", value=f"{confidence}%",                  inline=True)
    embed.add_field(name="🕯️ UT Bot",        value=f"Heikin Ashi: {'✅' if UT_HEIKIN_ASHI else '❌'}", inline=True)
    embed.add_field(name="📚 TP Source",     value=tp_source,                               inline=False)
    if stats["count"] >= 5:
        embed.add_field(name="📈 Signal Stats",
                        value=f"Avg MFE: {stats['avg_mfe']:.2f}% | Best: {stats['best']:.2f}% | Signals: {stats['count']} | Win Rate: {stats['win_rate']:.1f}%",
                        inline=False)
        embed.add_field(name="📊 Exit Breakdown",
                        value=f"🎯 TP: {stats['tp_hits']} | 🛑 SL: {stats['sl_hits']} | ⏱️ Cancelled: {stats['cancelled']} | Avg PnL: {stats['avg_pnl']:.2f}%",
                        inline=False)
    if tp_desc:
        embed.add_field(name="🧠 TP Logic", value=tp_desc, inline=False)
    embed.set_footer(text=f"MUFCA [AtomDC] v3.1 • Gate.io {mode_label} • HTF:{HTF_BIAS.upper()} • UT:{ha_label}")
    return embed


# FIX #6: track whether backtest is running
_backtest_running = False

@bot.event
async def on_ready():
    ha_status = "HA ON" if UT_HEIKIN_ASHI else "HA OFF"
    print(f"✅ {bot.user.name} started! Mode: {MARKET_MODE.upper()} | HTF: {HTF_BIAS.upper()} | UT: {ha_status} | Pairs: {' | '.join(TICKERS)}")
    # FIX #6: run backtest first, then start scanner
    asyncio.create_task(_startup_sequence())

async def _startup_sequence():
    """FIX #6: Ensure backtest completes before starting market scanner."""
    global _backtest_running
    _backtest_running = True
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_startup_backtest)
    _backtest_running = False
    print("[STARTUP] Backtest complete — adaptive TP ready. Starting scanner...")
    if not market_scanner.is_running():
        market_scanner.start()


@bot.command(name="status")
async def status_cmd(ctx):
    ha_status = "✅ ON" if UT_HEIKIN_ASHI else "❌ OFF"
    lines = [
        f"**MUFCA v3.1 — Scanner Status**\n",
        f"🧬 HTF Bias: **{HTF_BIAS.upper()}**\n",
        f"🕯️ UT Bot Heikin Ashi: **{ha_status}**\n",
        f"📚 Adaptive TP: last **{SIGNAL_HISTORY_LIMIT}** signals | **{TP_PERCENTILE*100:.0f}th** percentile\n",
    ]
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st    = state[ticker][tf]
            last  = st["last_bar_time"]
            ts    = f"<t:{last // 1000}:R>" if last else "no data"
            a_pos = "LONG" if st["a_in_long"] else "SHORT" if st["a_in_short"] else "—"
            u_pos = "LONG" if st["u_in_long"] else "SHORT" if st["u_in_short"] else "—"
            trade = st.get("active_trade")
            trade_info = ""
            if trade:
                trade_info = f" | 🎯 {trade['side'].upper()} @ ${trade['entry']} SL:${trade['sl']} TP:${trade['tp']}"
            lines.append(f"• `{ticker}` `{tf}` — bar: {ts} | A: **{a_pos}** | U: **{u_pos}**{trade_info}")
    # FIX #10: paginate if message too long
    msg = "\n".join(lines)
    if len(msg) > 1900:
        await ctx.send(msg[:1900] + "\n... (truncated)")
    else:
        await ctx.send(msg)


@bot.command(name="scan")
async def scan_cmd(ctx, ticker: str = "BTC/USDT", tf: str = "1h"):
    ticker = ticker.upper(); tf = tf.lower()
    await ctx.send(f"🔍 Scanning `{ticker}` `{tf}`…")
    st = state.get(ticker, {}).get(tf) or make_state()
    signals, bar_time, regime, lev = check_signals(ticker, tf, st)
    if signals:
        for sig_type, price, reg, leverage, bt, conf, sl, tp, risk, stats, tp_desc in signals:
            embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf, sl, tp, risk, stats, tp_desc)
            await ctx.send(embed=embed)
    else:
        await ctx.send(f"⏳ No signals for `{ticker}` `{tf}`. Regime: **{regime}**")


@bot.command(name="pairs")
async def pairs_cmd(ctx):
    if not TICKERS:
        await ctx.send("📭 Pair list is empty.")
        return
    lines = ["**📋 Scanned Pairs:**\n"]
    for t in TICKERS:
        lines.append(f"• `{t}`")
    await ctx.send("\n".join(lines))


@bot.command(name="add")
async def add_cmd(ctx, ticker: str = ""):
    if not ticker:
        await ctx.send("❌ Please specify a pair. Example: `!add SOL/USDT`")
        return
    ticker = ticker.upper()
    if ticker in TICKERS:
        await ctx.send(f"⚠️ `{ticker}` is already in the list.")
        return
    await ctx.send(f"🔍 Checking `{ticker}` on Gate.io…")
    try:
        markets = exchange.load_markets()
        if ticker not in markets:
            await ctx.send(f"❌ Pair `{ticker}` not found on Gate.io.")
            return
    except Exception as e:
        await ctx.send(f"❌ Check failed: {e}")
        return
    TICKERS.append(ticker)
    ensure_state(ticker)
    save_tickers(TICKERS)
    await ctx.send(f"✅ `{ticker}` added! Scanning: {' | '.join(TICKERS)}")
    print(f"[PAIRS] Added pair: {ticker}")


@bot.command(name="remove")
async def remove_cmd(ctx, ticker: str = ""):
    if not ticker:
        await ctx.send("❌ Please specify a pair. Example: `!remove SOL/USDT`")
        return
    ticker = ticker.upper()
    if ticker not in TICKERS:
        await ctx.send(f"⚠️ `{ticker}` is not in the list.")
        return
    if len(TICKERS) == 1:
        await ctx.send("❌ Cannot remove the last pair.")
        return
    TICKERS.remove(ticker)
    save_tickers(TICKERS)
    await ctx.send(f"🗑️ `{ticker}` removed. Remaining: {' | '.join(TICKERS)}")
    print(f"[PAIRS] Removed pair: {ticker}")


@bot.command(name="mode")
async def mode_cmd(ctx, new_mode: str = ""):
    global MARKET_MODE, exchange
    if not new_mode:
        label = "🔵 Spot" if MARKET_MODE == "spot" else "🟠 Futures"
        await ctx.send(f"Current mode: **{label}**\nTo switch: `!mode spot` or `!mode futures`")
        return
    new_mode = new_mode.lower()
    if new_mode not in ("spot", "futures"):
        await ctx.send("❌ Valid modes: `spot` or `futures`")
        return
    if new_mode == MARKET_MODE:
        await ctx.send(f"⚠️ Already in **{MARKET_MODE}** mode.")
        return
    MARKET_MODE = new_mode
    exchange    = make_exchange(MARKET_MODE)
    save_mode(MARKET_MODE)
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            state[ticker][tf] = make_state()
    label = "🔵 Spot (Gate.io Spot)" if MARKET_MODE == "spot" else "🟠 Futures (Gate.io Perpetual)"
    await ctx.send(f"✅ Switched to **{label}**\n⚠️ Position states have been reset.")
    print(f"[MODE] Switched to {MARKET_MODE}")


@bot.command(name="utha")
async def utha_cmd(ctx, arg: str = ""):
    global UT_HEIKIN_ASHI
    if not arg:
        status = "✅ ON" if UT_HEIKIN_ASHI else "❌ OFF"
        await ctx.send(f"🕯️ Heikin Ashi for UT Bot: **{status}**\nTo change: `!utha on` or `!utha off`")
        return
    arg = arg.lower()
    if arg not in ("on", "off"):
        await ctx.send("❌ Valid values: `on` or `off`")
        return
    new_value = arg == "on"
    if new_value == UT_HEIKIN_ASHI:
        status = "✅ already ON" if UT_HEIKIN_ASHI else "❌ already OFF"
        await ctx.send(f"⚠️ Heikin Ashi for UT Bot is {status}.")
        return
    UT_HEIKIN_ASHI = new_value
    save_ut_ha(UT_HEIKIN_ASHI)
    status = "✅ ENABLED" if UT_HEIKIN_ASHI else "❌ DISABLED"
    await ctx.send(f"🕯️ Heikin Ashi for UT Bot **{status}**.")
    print(f"[UT_HA] Heikin Ashi {'enabled' if UT_HEIKIN_ASHI else 'disabled'}")


@bot.command(name="htf")
async def htf_cmd(ctx, new_htf: str = ""):
    global HTF_BIAS
    if not new_htf:
        await ctx.send(f"🧬 Current HTF Bias: **{HTF_BIAS.upper()}**\n"
                       f"Available: `1d`, `4h`, `1h`, `1w`\n"
                       f"To change: `!htf 4h`")
        return
    new_htf = new_htf.lower()
    valid_htfs = ("1d", "4h", "1h", "2h", "6h", "12h", "1w", "3d")
    if new_htf not in valid_htfs:
        await ctx.send(f"❌ Valid HTF values: {', '.join(valid_htfs)}")
        return
    if new_htf == HTF_BIAS:
        await ctx.send(f"⚠️ HTF Bias is already **{HTF_BIAS.upper()}**.")
        return
    old_htf  = HTF_BIAS
    HTF_BIAS = new_htf
    _htf_cache = {}  # clear cache on HTF change
    save_htf(HTF_BIAS)
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            state[ticker][tf] = make_state()
    await ctx.send(f"🧬 HTF Bias changed: **{old_htf.upper()}** → **{HTF_BIAS.upper()}**\n"
                   f"⚠️ Position states have been reset.")
    print(f"[HTF] Changed from {old_htf} to {HTF_BIAS}")


@bot.command(name="tpconfig")
async def tpconfig_cmd(ctx, param: str = "", value: str = ""):
    global SIGNAL_HISTORY_LIMIT, TP_PERCENTILE, SAFE_TP_PERCENTILE
    if not param:
        await ctx.send(
            f"**📚 Adaptive TP Configuration:**\n"
            f"• History limit: **{SIGNAL_HISTORY_LIMIT}** signals\n"
            f"• Aggressive percentile: **{TP_PERCENTILE*100:.0f}th** (TP hit in ~{100-TP_PERCENTILE*100:.0f}% of cases)\n"
            f"• Safe percentile: **{SAFE_TP_PERCENTILE*100:.0f}th** (TP hit in ~{100-SAFE_TP_PERCENTILE*100:.0f}% of cases)\n"
            f"• Min TP: **{MIN_TP_PCT}%** | Max TP: **{MAX_TP_PCT}%**\n"
            f"• Max hold: **{MAX_HOLD_BARS}** bars\n"
            f"\nTo change: `!tpconfig limit 30` | `!tpconfig percentile 70` | `!tpconfig safe 50` | `!tpconfig mode safe`"
        )
        return
    param = param.lower()
    if param == "limit":
        try:
            new_limit = int(value)
            if not (5 <= new_limit <= 200):
                await ctx.send("❌ Limit must be between 5 and 200")
                return
            old = SIGNAL_HISTORY_LIMIT
            SIGNAL_HISTORY_LIMIT = new_limit
            await ctx.send(f"✅ History limit changed: **{old}** → **{new_limit}** signals")
        except ValueError:
            await ctx.send("❌ Invalid number")
    elif param == "percentile":
        try:
            new_pct = float(value)
            if not (10 <= new_pct <= 99):
                await ctx.send("❌ Percentile must be between 10 and 99")
                return
            old = TP_PERCENTILE
            TP_PERCENTILE = new_pct / 100
            await ctx.send(f"✅ Aggressive percentile changed: **{old*100:.0f}th** → **{new_pct:.0f}th**")
        except ValueError:
            await ctx.send("❌ Invalid number")
    elif param == "safe":
        try:
            new_pct = float(value)
            if not (10 <= new_pct <= 99):
                await ctx.send("❌ Safe percentile must be between 10 and 99")
                return
            old = SAFE_TP_PERCENTILE
            SAFE_TP_PERCENTILE = new_pct / 100
            await ctx.send(f"✅ Safe percentile changed: **{old*100:.0f}th** → **{new_pct:.0f}th**")
        except ValueError:
            await ctx.send("❌ Invalid number")
    elif param == "mode":
        if value.lower() == "safe":
            await ctx.send(
                f"🛡️ **Safe Mode Enabled**\n"
                f"TP will use **{SAFE_TP_PERCENTILE*100:.0f}th percentile** (median-based, more conservative).\n"
                f"This increases win rate but reduces average profit per trade.\n"
                f"Use `!tpconfig mode aggressive` to switch back."
            )
        elif value.lower() == "aggressive":
            await ctx.send(
                f"⚡ **Aggressive Mode Enabled**\n"
                f"TP will use **{TP_PERCENTILE*100:.0f}th percentile** (top-quartile, higher profit).\n"
                f"This reduces win rate but increases average profit per trade.\n"
                f"Use `!tpconfig mode safe` to switch to conservative."
            )
        else:
            await ctx.send("❌ Mode must be `safe` or `aggressive`")
    else:
        await ctx.send("❌ Unknown parameter. Use `limit`, `percentile`, `safe`, or `mode`")

@bot.command(name="history")
async def history_cmd(ctx, ticker: str = "", tf: str = ""):
    if not ticker:
        lines = ["**📊 Trade History:**\n"]
        for t in TICKERS:
            for timeframe in TIMEFRAMES:
                st     = state[t][timeframe]
                trades = st.get("trade_history", [])
                if trades:
                    lines.append(f"\n**`{t}` `{timeframe}` — {len(trades)} trades:**")
                    for i, trade in enumerate(trades[-5:], 1):
                        emoji = "🟢" if trade["pnl_pct"] > 0 else "🔴"
                        lines.append(f"{emoji} #{i} {trade['side'].upper()} | Entry: ${trade['entry']} → Exit: ${trade['exit']} | PnL: {trade['pnl_pct']:.2f}% | {trade['result'].upper()}")
        if len(lines) == 1:
            await ctx.send("📭 No trade history yet.")
            return
        await ctx.send("\n".join(lines))
        return

    ticker = ticker.upper()
    if tf:
        tf = tf.lower()
        st = state.get(ticker, {}).get(tf)
        if not st:
            await ctx.send(f"❌ No data for `{ticker}` `{tf}`")
            return
        trades = st.get("trade_history", [])
        if not trades:
            await ctx.send(f"📭 No trade history for `{ticker}` `{tf}`")
            return
        lines = [f"**📊 `{ticker}` `{tf}` Trade History ({len(trades)} trades):**\n"]
        for i, trade in enumerate(trades[-10:], 1):
            emoji = "🟢" if trade["pnl_pct"] > 0 else "🔴"
            lines.append(f"{emoji} #{i} {trade['side'].upper()} | Entry: ${trade['entry']} → Exit: ${trade['exit']} | PnL: {trade['pnl_pct']:.2f}% | {trade['result'].upper()}")
        await ctx.send("\n".join(lines))
    else:
        lines = [f"**📊 `{ticker}` Trade History:**\n"]
        for timeframe in TIMEFRAMES:
            st = state.get(ticker, {}).get(timeframe)
            if st:
                trades = st.get("trade_history", [])
                if trades:
                    lines.append(f"\n**`{timeframe}` — {len(trades)} trades:**")
                    for i, trade in enumerate(trades[-5:], 1):
                        emoji = "🟢" if trade["pnl_pct"] > 0 else "🔴"
                        lines.append(f"{emoji} #{i} {trade['side'].upper()} | Entry: ${trade['entry']} → Exit: ${trade['exit']} | PnL: {trade['pnl_pct']:.2f}% | {trade['result'].upper()}")
        await ctx.send("\n".join(lines))


@bot.command(name="signals")
async def signals_cmd(ctx, ticker: str = "", tf: str = "", side: str = ""):
    history = load_signals_history()
    if not ticker:
        lines = ["**📚 Signal History Summary:**\n"]
        for t in history:
            for timeframe in history[t]:
                for s in ("long", "short"):
                    records = [r for r in history[t][timeframe].get(s, []) if r["exit_type"] != "open"]
                    if records:
                        stats = get_signal_stats(t, timeframe, s)
                        lines.append(f"• `{t}` `{timeframe}` {s.upper()}: {stats['count']} signals | "
                                    f"Win Rate: {stats['win_rate']:.1f}% | "
                                    f"🎯TP:{stats['tp_hits']} 🛑SL:{stats['sl_hits']} ⏱️TO:{stats['cancelled']} | "
                                    f"Avg MFE: {stats['avg_mfe']:.2f}% | Avg PnL: {stats['avg_pnl']:.2f}%")
        if len(lines) == 1:
            await ctx.send("📭 No signal history yet.")
            return
        await ctx.send("\n".join(lines))
        return

    ticker = ticker.upper()
    if ticker not in history:
        await ctx.send(f"📭 No history for `{ticker}`")
        return
    if not tf:
        lines = [f"**📚 `{ticker}` Signal History:**"]
        for timeframe in history[ticker]:
            for s in ("long", "short"):
                records = [r for r in history[ticker][timeframe].get(s, []) if r["exit_type"] != "open"]
                if records:
                    stats = get_signal_stats(ticker, timeframe, s)
                    lines.append(f"• `{timeframe}` {s.upper()}: {stats['count']} signals | "
                                f"Win Rate: {stats['win_rate']:.1f}% | "
                                f"🎯TP:{stats['tp_hits']} 🛑SL:{stats['sl_hits']} ⏱️TO:{stats['cancelled']} | "
                                f"Avg MFE: {stats['avg_mfe']:.2f}% | Avg PnL: {stats['avg_pnl']:.2f}%")
        await ctx.send("\n".join(lines))
        return

    tf = tf.lower()
    if tf not in history[ticker]:
        await ctx.send(f"📭 No history for `{ticker}` `{tf}`")
        return
    if not side:
        lines = [f"**📚 `{ticker}` `{tf}` Signal History:**"]
        for s in ("long", "short"):
            records = [r for r in history[ticker][tf].get(s, []) if r["exit_type"] != "open"]
            if records:
                stats = get_signal_stats(ticker, tf, s)
                lines.append(f"• {s.upper()}: {stats['count']} signals | "
                            f"Win Rate: {stats['win_rate']:.1f}% | "
                            f"🎯TP:{stats['tp_hits']} 🛑SL:{stats['sl_hits']} ⏱️TO:{stats['cancelled']} | "
                            f"Avg MFE: {stats['avg_mfe']:.2f}% | Avg PnL: {stats['avg_pnl']:.2f}%")
        await ctx.send("\n".join(lines))
        return

    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("❌ Side must be `long` or `short`")
        return
    records = [r for r in history[ticker][tf].get(side, []) if r["exit_type"] != "open"]
    if not records:
        await ctx.send(f"📭 No {side} history for `{ticker}` `{tf}`")
        return
    stats = get_signal_stats(ticker, tf, side)
    lines = [f"**📚 `{ticker}` `{tf}` {side.upper()} Signal History ({len(records)} signals):**"]
    lines.append(f"📊 Win Rate: {stats['win_rate']:.1f}% | 🎯TP:{stats['tp_hits']} 🛑SL:{stats['sl_hits']} ⏱️TO:{stats['cancelled']} | Avg PnL: {stats['avg_pnl']:.2f}%")
    for i, rec in enumerate(records[-15:], 1):
        emoji = "🟢" if rec["moved_pct"] > 0 else "🔴"
        exit_emoji = "🎯" if rec["exit_type"] == "tp" else "🛑" if rec["exit_type"] == "sl" else "⏱️"
        lines.append(
            f"{emoji} #{i} Entry: ${rec['entry']} → Exit: ${rec['exit']} | "
            f"MFE: {rec['max_favorable_pct']:.2f}% | MAE: {rec['max_adverse_pct']:.2f}% | "
            f"{exit_emoji} {rec['exit_type'].upper()} | PnL: {rec['moved_pct']:.2f}%"
        )
    await ctx.send("\n".join(lines))

@bot.command(name="tp")
async def tp_cmd(ctx, side: str = "long", ticker: str = "BTC/USDT", tf: str = "1h"):
    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("Side must be `long` or `short`")
        return
    ticker = ticker.upper(); tf = tf.lower()
    try:
        bars = exchange.fetch_ohlcv(ticker, tf, limit=100)
        df   = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        last_close = float(df["close"].iloc[-2])
        atr14      = calculate_atr(df, ATR_PERIOD)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        idx  = len(df) - 2
        sl   = calculate_sl(last_close, side, fs, fu, fl, atr14, idx)
        tp, tp_desc = calculate_combined_tp(ticker, tf, side, last_close, sl, df, idx, atr14)
        stats = get_signal_stats(ticker, tf, side)
        risk  = abs(last_close - sl)
        rr    = round(abs(tp - last_close) / max(risk, 1e-8), 2)
        tp_pct = abs(tp - last_close) / last_close * 100

        lines = [f"**📊 Adaptive TP Preview — `{ticker}` `{tf}` {side.upper()}:**"]
        lines.append(f"• Current price: **${last_close:,.4f}**")
        lines.append(f"• Stop Loss: **${sl:,.4f}** (risk: ${risk:,.4f})")
        lines.append(f"• Take Profit: **${tp:,.4f}** (+{tp_pct:.2f}%)")
        lines.append(f"• Risk/Reward: **1:{rr}**")
        if stats["count"] >= 5:
            lines.append(f"• Based on **{stats['count']}** historical signals")
            lines.append(f"• Avg MFE: **{stats['avg_mfe']:.2f}%** | Best: **{stats['best']:.2f}%**")
            lines.append(f"• Percentile used: **{TP_PERCENTILE*100:.0f}th**")
        else:
            lines.append(f"• ⚠️ Only **{stats['count']}** signals in history — using fallback R:R 2.0")
        await ctx.send("\n".join(lines))
    except Exception as e:
        await ctx.send(f"Error: {e}")


@bot.command(name="debug")
async def debug_cmd(ctx):
    lines = ["**🔍 Debug Information:**"]
    lines.append(f"• Total scans: **{scan_stats['total_scans']}**")
    lines.append(f"• Signals generated: **{scan_stats['signals_generated']}**")
    lines.append(f"• Last scan: {scan_stats['last_scan_time'] or 'never'}")

    history      = load_signals_history()
    total_sigs   = sum(len(history[t][tf].get(s, [])) for t in history for tf in history[t] for s in ("long","short"))
    total_closed = sum(1 for t in history for tf in history[t] for s in ("long","short")
                       for r in history[t][tf].get(s, []) if r["exit_type"] != "open")

    lines.append(f"• Signal history: **{total_sigs}** records ({total_closed} closed)")
    lines.append(f"• History file: **{'Yes' if os.path.exists(SIGNALS_HISTORY_FILE) else 'No'}**")
    if os.path.exists(SIGNALS_HISTORY_FILE):
        lines.append(f"• File size: **{os.path.getsize(SIGNALS_HISTORY_FILE)} bytes**")

    active_count = sum(1 for t in TICKERS for tf in TIMEFRAMES if state[t][tf].get("active_trade"))
    lines.append(f"• Active trades: **{active_count}**")
    await ctx.send("\n".join(lines))


@bot.command(name="reset")
async def reset_cmd(ctx, confirm: str = ""):
    if confirm.lower() != "yes":
        await ctx.send(
            "⚠️ This will DELETE all signal history and trade data!\n"
            "To confirm, type: `!reset yes`"
        )
        return
    global _signals_history_cache
    _signals_history_cache = {}
    if os.path.exists(SIGNALS_HISTORY_FILE):
        os.remove(SIGNALS_HISTORY_FILE)
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            state[ticker][tf]["trade_history"] = []
            state[ticker][tf]["active_trade"]  = None
            state[ticker][tf]["bars_in_trade"] = 0
    await ctx.send("🗑️ History cleared. Running fresh backtest…")
    total = 0
    import time
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            count = backtest_history(ticker, tf, num_bars=3000)
            total += count
            await ctx.send(f"✅ `{ticker}` `{tf}`: {count} signals found")
            time.sleep(0.5)
    await ctx.send(f"🎓 Fresh backtest complete! Total signals: **{total}**\nRun `!signals` to see statistics.")


@bot.command(name="sim")
async def sim_cmd(ctx, side: str = "long", ticker: str = "BTC/USDT", tf: str = "1h"):
    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("Side must be `long` or `short`")
        return
    ticker = ticker.upper(); tf = tf.lower()
    await ctx.send(f"Simulating {side.upper()} signal for `{ticker}` `{tf}`…")
    try:
        bars = exchange.fetch_ohlcv(ticker, tf, limit=100)
        df   = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        last_close = float(df["close"].iloc[-2])
        atr14      = calculate_atr(df, ATR_PERIOD)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        idx = len(df) - 2
        sl  = calculate_sl(last_close, side, fs, fu, fl, atr14, idx)
        tp, tp_desc = calculate_combined_tp(ticker, tf, side, last_close, sl, df, idx, atr14)

        add_signal_record(ticker, tf, side, last_close, datetime.now(timezone.utc).isoformat())
        update_signal_record(ticker, tf, side, tp, "tp", 5)
        stats = get_signal_stats(ticker, tf, side)

        lines = [f"✅ Simulated {side.upper()} signal recorded!"]
        lines.append(f"• Entry: **${last_close:,.4f}**")
        lines.append(f"• SL: **${sl:,.4f}**")
        lines.append(f"• TP: **${tp:,.4f}**")
        lines.append(f"• History now has **{stats['count']}** closed {side} signals")
        lines.append(f"• Run `!signals {ticker} {tf} {side}` to see details")
        await ctx.send("\n".join(lines))
    except Exception as e:
        await ctx.send(f"Simulation failed: {e}")
        import traceback
        await ctx.send(f"```\n{traceback.format_exc()[:1000]}\n```")


@bot.command(name="forcerun")
async def forcerun_cmd(ctx, side: str = "long", ticker: str = "BTC/USDT", tf: str = "1h"):
    """!forcerun [long|short] [TICKER] [TF] — force a signal bypassing all filters (for testing)"""
    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("Side must be `long` or `short`")
        return
    ticker = ticker.upper(); tf = tf.lower()
    await ctx.send(f"Force-running {side.upper()} signal for `{ticker}` `{tf}` (bypassing filters)…")
    try:
        bars = exchange.fetch_ohlcv(ticker, tf, limit=100)
        df   = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        last_close = float(df["close"].iloc[-2])
        atr14      = calculate_atr(df, ATR_PERIOD)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        idx  = len(df) - 2
        sl   = calculate_sl(last_close, side, fs, fu, fl, atr14, idx)
        tp, tp_desc = calculate_combined_tp(ticker, tf, side, last_close, sl, df, idx, atr14)
        risk = abs(last_close - sl)

        st = state.get(ticker, {}).get(tf) or make_state()
        st["active_trade"]  = {"side": side, "entry": last_close, "sl": sl, "tp": tp, "lev": 3, "bar_opened": idx}
        st["bars_in_trade"] = 0
        add_signal_record(ticker, tf, side, last_close, datetime.now(timezone.utc).isoformat())
        stats = get_signal_stats(ticker, tf, side)

        rr     = round(abs(tp - last_close) / max(risk, 1e-8), 2)
        tp_pct = abs(tp - last_close) / last_close * 100

        embed = discord.Embed(
            title=f"⚡ FORCE SIGNAL {'📈 LONG' if side == 'long' else '📉 SHORT'}",
            color=discord.Color.green() if side == "long" else discord.Color.red(),
        )
        embed.add_field(name="Pair",   value=f"**{ticker}**",          inline=True)
        embed.add_field(name="TF",     value=tf.upper(),                inline=True)
        embed.add_field(name="Entry",  value=f"${last_close:,.4f}",     inline=True)
        embed.add_field(name="SL",     value=f"${sl:,.4f}",             inline=True)
        embed.add_field(name="TP",     value=f"${tp:,.4f} (+{tp_pct:.2f}%)", inline=True)
        embed.add_field(name="R:R",    value=f"1:{rr}",                 inline=True)
        embed.add_field(name="⚠️ WARNING", value="Bypassed all filters — for testing only!", inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Force run failed: {e}")
        import traceback
        await ctx.send(f"```\n{traceback.format_exc()[:1000]}\n```")


@tasks.loop(seconds=20)
async def market_scanner():
    channel = discord.utils.get(bot.get_all_channels(), name=CHANNEL_NAME)
    if channel is None:
        print(f"[WARN] Channel '{CHANNEL_NAME}' not found!")
        return

    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st = state[ticker][tf]
            signals, bar_time, regime, lev = check_signals(ticker, tf, st)

            if bar_time and bar_time != st["last_bar_time"]:
                st["last_bar_time"] = bar_time
                for sig_type, price, reg, leverage, bt, conf, sl, tp, risk, stats, tp_desc in signals:
                    embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf, sl, tp, risk, stats, tp_desc)
                    await channel.send(embed=embed)
                    print(f"[SIGNAL] {ticker} {tf} | {sig_type} @ {price:.4f} | SL:{sl:.4f} TP:{tp:.4f} | conf={conf}%")

            # FIX #3: Notify on fresh trade closure (with deduplication flag)
            trade = st.get("active_trade")
            if not trade and st.get("trade_history") and not st.get("last_closure_notified", False):
                last = st["trade_history"][-1]
                if last.get("exit_time"):
                    exit_dt = datetime.fromisoformat(last["exit_time"])
                    age = (datetime.now(timezone.utc) - exit_dt).total_seconds()
                    if age < 35:
                        emoji = "🟢" if last["pnl_pct"] > 0 else "🔴"
                        await channel.send(
                            f"{emoji} **Trade Closed** | `{ticker}` `{tf}` | "
                            f"{last['side'].upper()} | Entry: ${last['entry']} → Exit: ${last['exit']} | "
                            f"PnL: **{last['pnl_pct']:.2f}%** | Result: **{last['result'].upper()}** | Bars: {last['bars_held']}"
                        )
                        st["last_closure_notified"] = True

            await asyncio.sleep(0.5)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in .env file!")
    bot.run(DISCORD_TOKEN)
