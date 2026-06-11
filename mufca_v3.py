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
MIN_TP_PCT           = 0.3
MAX_TP_PCT           = 8.0
MAX_HOLD_BARS        = 20  # force close after N bars

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
# 📚  SIGNALS HISTORY — for adaptive TP learning
# =====================================================================
SIGNALS_HISTORY_FILE = "signals_history.json"

def load_signals_history() -> dict:
    try:
        with open(SIGNALS_HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_signals_history(history: dict):
    try:
        with open(SIGNALS_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"[WARN] Failed to save signals history: {e}")

def add_signal_record(ticker: str, tf: str, side: str, entry: float, timestamp: str):
    """Add a new signal record when position opens."""
    history = load_signals_history()
    if ticker not in history:
        history[ticker] = {}
    if tf not in history[ticker]:
        history[ticker][tf] = {"long": [], "short": []}

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
    """Update the last open signal record with exit data."""
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
    """Update max favorable/adverse excursion for the last open signal."""
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        return

    records = history[ticker][tf][side]
    for rec in reversed(records):
        if rec["exit_type"] == "open":
            entry = rec["entry"]
            if side == "long":
                favorable = (current_price - entry) / entry * 100
                adverse = (entry - current_price) / entry * 100
            else:
                favorable = (entry - current_price) / entry * 100
                adverse = (current_price - entry) / entry * 100

            # FIXED: proper round() syntax
            rec["max_favorable_pct"] = round(max(float(rec.get("max_favorable_pct", 0)), favorable), 4)
            rec["max_adverse_pct"] = round(max(float(rec.get("max_adverse_pct", 0)), adverse), 4)
            save_signals_history(history)
            return

def calculate_adaptive_tp(ticker: str, tf: str, side: str, entry: float, current_sl: float) -> float:
    """Calculate adaptive TP based on historical signal performance."""
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        risk = abs(entry - current_sl)
        tp = entry + (2.0 * risk) if side == "long" else entry - (2.0 * risk)
        print(f"[ADAPTIVE_TP] {ticker} {tf} {side} | NO HISTORY | fallback TP={tp:.4f}")
        return round(tp, 4)

    records = history[ticker][tf][side]
    closed = [r for r in records if r["exit_type"] in ("tp", "sl", "cancelled")]

    if len(closed) < 3:
        risk = abs(entry - current_sl)
        tp = entry + (2.0 * risk) if side == "long" else entry - (2.0 * risk)
        print(f"[ADAPTIVE_TP] {ticker} {tf} {side} | Only {len(closed)} closed signals | fallback TP={tp:.4f}")
        return round(tp, 4)

    recent = closed[-SIGNAL_HISTORY_LIMIT:]
    favorable_pcts = []
    for r in recent:
        mfe = r.get("max_favorable_pct", 0)
        if mfe == 0 and r.get("moved_pct", 0) != 0:
            mfe = abs(r["moved_pct"])
        favorable_pcts.append(max(mfe, 0.1))

    tp_pct = np.percentile(favorable_pcts, TP_PERCENTILE * 100)
    tp_pct = max(MIN_TP_PCT, min(MAX_TP_PCT, tp_pct))

    if side == "long":
        tp = entry * (1 + tp_pct / 100)
    else:
        tp = entry * (1 - tp_pct / 100)

    print(f"[ADAPTIVE_TP] {ticker} {tf} {side} | entry={entry:.4f} | history={len(recent)} signals | tp_pct={tp_pct:.2f}% | TP={tp:.4f}")
    return round(tp, 4)

def get_signal_stats(ticker: str, tf: str, side: str) -> dict:
    """Get statistics for adaptive TP display."""
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        return {"count": 0, "avg_mfe": 0, "median_mfe": 0, "tp_pct": 0}

    records = history[ticker][tf][side]
    closed = [r for r in records if r["exit_type"] in ("tp", "sl", "cancelled")]
    if len(closed) < 1:
        return {"count": 0, "avg_mfe": 0, "median_mfe": 0, "tp_pct": 0}

    recent = closed[-SIGNAL_HISTORY_LIMIT:]
    favorable_pcts = []
    for r in recent:
        mfe = r.get("max_favorable_pct", 0)
        if mfe == 0 and r.get("moved_pct", 0) != 0:
            mfe = abs(r["moved_pct"])
        favorable_pcts.append(max(mfe, 0.1))

    return {
        "count": len(recent),
        "avg_mfe": round(float(np.mean(favorable_pcts)), 2),
        "median_mfe": round(float(np.median(favorable_pcts)), 2),
        "tp_pct": round(float(np.percentile(favorable_pcts, TP_PERCENTILE * 100)), 2),
        "best": round(float(max(favorable_pcts)), 2),
        "worst": round(float(min(favorable_pcts)), 2),
    }

# =====================================================================
# 🔙  BACKTEST — populate signal history from historical bars
# =====================================================================

def backtest_history(ticker: str, tf: str, num_bars: int = 2000) -> int:
    """
    Scan historical bars to find past signals and simulate their outcomes.
    Populates signals_history.json with synthetic data for adaptive TP.
    Returns number of signals found.
    """
    print(f"[BACKTEST] Starting backtest for {ticker} {tf} ({num_bars} bars)...")

    try:
        bars = exchange.fetch_ohlcv(ticker, tf, limit=num_bars)
        df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])

        if len(df) < 100:
            print(f"[BACKTEST] Not enough bars for {ticker} {tf}")
            return 0

        # Pre-calculate indicators for all bars
        atr14 = calculate_atr(df, ATR_PERIOD)
        atr_pct = (atr14 / df["close"]) * 100
        chop = calculate_chop(df, CHOP_LENGTH)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        mfi = calculate_mfi(df, MFI_LEN)
        level_os, level_ob = run_kmeans_mfi(mfi, MFI_TRAINING)
        and_osc, and_sig = calculate_andean(df, AND_LEN, AND_SIG_LEN)
        ut_buy, ut_sell = calculate_ut_bot(df, UT_SENSITIVITY, UT_PERIOD, use_ha=UT_HEIKIN_ASHI)

        signals_found = 0

        # Scan from bar 50 to len-20 (need room for future bars to check TP/SL)
        for idx in range(50, len(df) - 60):
            close_v = float(df["close"].iloc[idx])
            open_v = float(df["open"].iloc[idx])
            atr_v = max(float(atr14.iloc[idx]), 1e-8)
            atr_pct_v = float(atr_pct.iloc[idx])
            chop_v = float(chop.iloc[idx])

            atr_ok = ATR_MIN <= atr_pct_v <= ATR_MAX
            chop_ok = chop_v < CHOP_THRESHOLD

            frama_slope = float(fs.iloc[idx]) - float(fs.iloc[idx - 1])
            slope_long = frama_slope > 0
            slope_short = frama_slope < 0

            frama_dir_v = int(fdir.iloc[idx])
            frama_bull = frama_dir_v == 1
            frama_bear = frama_dir_v == -1

            # Skip HTF bias for backtest (assume aligned)
            htf_bull = True
            htf_bear = True

            # Fake breakout filters
            hh10_prev = float(df["high"].iloc[max(0, idx-10):idx].max())
            ll10_prev = float(df["low"].iloc[max(0, idx-10):idx].min())
            fake_break_long = float(df["high"].iloc[idx]) > hh10_prev and close_v < hh10_prev
            fake_break_short = float(df["low"].iloc[idx]) < ll10_prev and close_v > ll10_prev

            ll5_prev = float(df["low"].iloc[max(0, idx-5):idx].min())
            hh5_prev = float(df["high"].iloc[max(0, idx-5):idx].max())
            liq_sweep_long = float(df["low"].iloc[idx]) < ll5_prev and close_v > ll5_prev and close_v > open_v
            liq_sweep_short = float(df["high"].iloc[idx]) > hh5_prev and close_v < hh5_prev and close_v < open_v

            filter_long = frama_bull and chop_ok and atr_ok and slope_long and not fake_break_long and not liq_sweep_short
            filter_short = frama_bear and chop_ok and atr_ok and slope_short and not fake_break_short and not liq_sweep_long

            # Signal detection
            def crossover(s, lvl, i):
                return float(s.iloc[i]) > lvl and float(s.iloc[i-1]) <= lvl
            def crossunder(s, lvl, i):
                return float(s.iloc[i]) < lvl and float(s.iloc[i-1]) >= lvl
            def crossover2(s1, s2, i):
                return float(s1.iloc[i]) > float(s2.iloc[i]) and float(s1.iloc[i-1]) <= float(s2.iloc[i-1])
            def crossunder2(s1, s2, i):
                return float(s1.iloc[i]) < float(s2.iloc[i]) and float(s1.iloc[i-1]) >= float(s2.iloc[i-1])

            mfi_bull_sig = crossover(mfi, level_os, idx)
            mfi_bear_sig = crossunder(mfi, level_ob, idx)
            and_bull_sig = crossover2(and_osc, and_sig, idx)
            and_bear_sig = crossunder2(and_osc, and_sig, idx)

            sig_a_long = mfi_bull_sig and filter_long
            sig_a_short = mfi_bear_sig and filter_short
            sig_u_long = bool(ut_buy.iloc[idx]) and filter_long
            sig_u_short = bool(ut_sell.iloc[idx]) and filter_short

            if sig_a_long or sig_u_long:
                side = "long"
                sl = calculate_sl(close_v, side, fs, fu, fl, atr14, idx)

                # Simulate future: check if TP or SL hit first in next 20 bars
                tp_hit = False
                sl_hit = False
                max_favorable = 0.0
                max_adverse = 0.0
                exit_price = close_v
                bars_held = 0

                for future_idx in range(idx + 1, min(idx + 61, len(df))):
                    future_high = float(df["high"].iloc[future_idx])
                    future_low = float(df["low"].iloc[future_idx])
                    future_close = float(df["close"].iloc[future_idx])

                    # Track MFE/MAE
                    favorable = (future_high - close_v) / close_v * 100
                    adverse = (close_v - future_low) / close_v * 100
                    max_favorable = max(max_favorable, favorable)
                    max_adverse = max(max_adverse, adverse)

                    # Check SL first (more conservative)
                    if future_low <= sl:
                        sl_hit = True
                        exit_price = sl
                        bars_held = future_idx - idx
                        break

                    # Then check if we can find a reasonable TP
                    # Use the max favorable move as "optimal" TP
                    # But cap it to avoid outliers
                    optimal_tp_pct = min(max_favorable, MAX_TP_PCT)
                    tp = close_v * (1 + optimal_tp_pct / 100)

                    if future_high >= tp:
                        tp_hit = True
                        exit_price = tp
                        bars_held = future_idx - idx
                        break

                    bars_held = future_idx - idx

                # Record the signal
                history = load_signals_history()
                if ticker not in history:
                    history[ticker] = {}
                if tf not in history[ticker]:
                    history[ticker][tf] = {"long": [], "short": []}

                exit_type = "tp" if tp_hit else "sl" if sl_hit else "cancelled"
                moved_pct = (exit_price - close_v) / close_v * 100

                history[ticker][tf][side].append({
                    "entry": round(close_v, 4),
                    "exit": round(exit_price, 4),
                    "exit_type": exit_type,
                    "bars_held": bars_held,
                    "moved_pct": round(moved_pct, 4),
                    "timestamp": str(int(df["timestamp"].iloc[idx])),
                    "max_favorable_pct": round(max_favorable, 4),
                    "max_adverse_pct": round(max_adverse, 4),
                })

                # Keep only last N*3
                history[ticker][tf][side] = history[ticker][tf][side][-(SIGNAL_HISTORY_LIMIT * 3):]
                save_signals_history(history)
                signals_found += 1

            elif sig_a_short or sig_u_short:
                side = "short"
                sl = calculate_sl(close_v, side, fs, fu, fl, atr14, idx)

                tp_hit = False
                sl_hit = False
                max_favorable = 0.0
                max_adverse = 0.0
                exit_price = close_v
                bars_held = 0

                for future_idx in range(idx + 1, min(idx + 61, len(df))):
                    future_high = float(df["high"].iloc[future_idx])
                    future_low = float(df["low"].iloc[future_idx])
                    future_close = float(df["close"].iloc[future_idx])

                    favorable = (close_v - future_low) / close_v * 100
                    adverse = (future_high - close_v) / close_v * 100
                    max_favorable = max(max_favorable, favorable)
                    max_adverse = max(max_adverse, adverse)

                    if future_high >= sl:
                        sl_hit = True
                        exit_price = sl
                        bars_held = future_idx - idx
                        break

                    optimal_tp_pct = min(max_favorable, MAX_TP_PCT)
                    tp = close_v * (1 - optimal_tp_pct / 100)

                    if future_low <= tp:
                        tp_hit = True
                        exit_price = tp
                        bars_held = future_idx - idx
                        break

                    bars_held = future_idx - idx

                history = load_signals_history()
                if ticker not in history:
                    history[ticker] = {}
                if tf not in history[ticker]:
                    history[ticker][tf] = {"long": [], "short": []}

                exit_type = "tp" if tp_hit else "sl" if sl_hit else "cancelled"
                moved_pct = (close_v - exit_price) / close_v * 100

                history[ticker][tf][side].append({
                    "entry": round(close_v, 4),
                    "exit": round(exit_price, 4),
                    "exit_type": exit_type,
                    "bars_held": bars_held,
                    "moved_pct": round(moved_pct, 4),
                    "timestamp": str(int(df["timestamp"].iloc[idx])),
                    "max_favorable_pct": round(max_favorable, 4),
                    "max_adverse_pct": round(max_adverse, 4),
                })

                history[ticker][tf][side] = history[ticker][tf][side][-(SIGNAL_HISTORY_LIMIT * 3):]
                save_signals_history(history)
                signals_found += 1

        print(f"[BACKTEST] {ticker} {tf}: found {signals_found} historical signals")
        return signals_found

    except Exception as e:
        print(f"[ERROR] Backtest failed for {ticker} {tf}: {e}")
        import traceback
        traceback.print_exc()
        return 0


def run_startup_backtest():
    """Run backtest for all pairs/timeframes on startup."""
    print("=" * 60)
    print("[STARTUP] Running historical backtest to populate signal history...")
    print("=" * 60)

    total_signals = 0
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            count = backtest_history(ticker, tf, num_bars=2000)
            total_signals += count
            # Small delay to avoid rate limits
            import time
            time.sleep(0.5)

    print("=" * 60)
    print(f"[STARTUP] Backtest complete! Total historical signals: {total_signals}")
    print("=" * 60)


# =====================================================================
# 🗂️  STATE
# =====================================================================
def make_state():
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
    }

state: dict = {ticker: {tf: make_state() for tf in TIMEFRAMES} for ticker in TICKERS}

# =====================================================================
# 🔍  SCAN COUNTER — for debugging
# =====================================================================
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
    return 100 * np.log10(atr_sum / (hh - ll + 1e-8)) / np.log10(length)

def calculate_frama(df: pd.DataFrame, length: int = 22, mult: float = 2.1):
    n = int(length / 2)
    hh1 = df["high"].rolling(window=n).max()
    ll1 = df["low"].rolling(window=n).min()
    n1 = (hh1 - ll1) / n
    hh2 = df["high"].shift(n).rolling(window=n).max()
    ll2 = df["low"].shift(n).rolling(window=n).min()
    n2 = (hh2 - ll2) / n
    hh3 = df["high"].rolling(window=length).max()
    ll3 = df["low"].rolling(window=length).min()
    n3 = (hh3 - ll3) / length

    with np.errstate(divide="ignore", invalid="ignore"):
        dimen = np.where(
            (n1 > 0) & (n2 > 0) & (n3 > 0),
            (np.log(n1 + n2 + 1e-8) - np.log(n3 + 1e-8)) / np.log(2.0),
            0.0,
        )
    alpha = np.clip(np.exp(-4.6 * (dimen - 1.0)), 0.01, 1.0)

    close = df["close"].values
    frama_ma = np.zeros(len(df))
    frama_ma[0] = close[0]
    for i in range(1, len(df)):
        frama_ma[i] = alpha[i] * close[i] + (1.0 - alpha[i]) * frama_ma[i - 1]

    fs = pd.Series(frama_ma, index=df.index)
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    fatr = tr.rolling(window=length).mean()
    fu = fs + fatr * mult
    fl = fs - fatr * mult

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
    hlc3 = (df["high"] + df["low"] + df["close"]) / 3.0
    mf = hlc3 * df["volume"]
    pos = np.where(hlc3 > hlc3.shift(1), mf, 0.0)
    neg = np.where(hlc3 < hlc3.shift(1), mf, 0.0)
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
        cl1 = vals[np.abs(vals - c1) < np.abs(vals - c2)]
        cl2 = vals[np.abs(vals - c2) <= np.abs(vals - c1)]
        c1 = float(cl1.mean()) if len(cl1) > 0 else c1
        c2 = float(cl2.mean()) if len(cl2) > 0 else c2
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
    ha = df.copy()
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open = pd.Series(index=df.index, dtype=float)
    ha_open.iloc[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2.0
    ha["open"] = ha_open
    ha["high"] = pd.concat([df["high"], ha_open, ha_close], axis=1).max(axis=1)
    ha["low"] = pd.concat([df["low"], ha_open, ha_close], axis=1).min(axis=1)
    ha["close"] = ha_close
    return ha

def calculate_ut_bot(df: pd.DataFrame, sensitivity: float = 1.0, period: int = 10, use_ha: bool = False):
    df_ut = heikin_ashi(df) if use_ha else df.copy()
    src = df_ut["close"].values
    n_loss = (sensitivity * calculate_atr(df_ut, period)).values
    ts = np.zeros(len(df))
    ts[0] = src[0]
    for i in range(1, len(df)):
        prev = ts[i-1]
        if src[i] > prev and src[i-1] > prev:
            ts[i] = max(prev, src[i] - n_loss[i])
        elif src[i] < prev and src[i-1] < prev:
            ts[i] = min(prev, src[i] + n_loss[i])
        else:
            ts[i] = src[i] - n_loss[i] if src[i] > prev else src[i] + n_loss[i]
    ts_s = pd.Series(ts, index=df.index)
    src_s = df_ut["close"]
    ut_buy = (src_s > ts_s) & (src_s.shift(1) <= ts_s.shift(1))
    ut_sell = (src_s < ts_s) & (src_s.shift(1) >= ts_s.shift(1))
    return ut_buy, ut_sell

def get_htf_bias(ticker: str, timeframe: str) -> int:
    htf = HTF_BIAS
    try:
        bars = exchange.fetch_ohlcv(ticker, htf, limit=150)
        df_htf = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        fs, fu, fl, fdir = calculate_frama(df_htf, FRAMA_LEN, FRAMA_MULT)
        htf_close = df_htf["close"].iloc[-2]
        htf_frama = fs.iloc[-2]
        bias = 1 if htf_close > htf_frama else -1
        print(f"[HTF] {ticker} -> {htf} | close={htf_close:.2f} frama={htf_frama:.2f} bias={'BULL' if bias==1 else 'BEAR'}")
        return bias
    except Exception as e:
        print(f"[WARN] HTF Bias ({ticker} {htf}): {e}")
        return 0

# =====================================================================
# 🎯  SL & TP CALCULATION
# =====================================================================

def calculate_sl(entry_price: float, side: str, fs: pd.Series, fu: pd.Series, fl: pd.Series, atr14: pd.Series, idx: int) -> float:
    atr_v = max(float(atr14.iloc[idx]), 1e-8)
    if side == "long":
        sl_frama = float(fl.iloc[idx])
        sl_atr = entry_price - 1.5 * atr_v
        return max(sl_frama, sl_atr)
    else:
        sl_frama = float(fu.iloc[idx])
        sl_atr = entry_price + 1.5 * atr_v
        return min(sl_frama, sl_atr)

def check_tp_sl_hit(st: dict, high: float, low: float) -> str | None:
    trade = st.get("active_trade")
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

def close_trade(st: dict, exit_price: float, result: str, ticker: str, tf: str):
    trade = st.get("active_trade")
    if not trade:
        return None
    entry = trade["entry"]
    side = trade["side"]
    bars_held = st.get("bars_in_trade", 0)
    if side == "long":
        pnl_pct = (exit_price - entry) / entry * 100
    else:
        pnl_pct = (entry - exit_price) / entry * 100
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
    st["trade_history"].append(closed_trade)
    st["trade_history"] = st["trade_history"][-50:]
    update_signal_record(ticker, tf, side, exit_price, result, bars_held)
    st["active_trade"] = None
    st["bars_in_trade"] = 0
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
        df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])

        current_bar_time = int(df["timestamp"].iloc[-2])
        is_new_bar = st["last_processed_bar_time"] is None or current_bar_time > st["last_processed_bar_time"]
        st["last_processed_bar_time"] = current_bar_time

        # Data from LAST confirmed bar (idx = -2)
        last_high = float(df["high"].iloc[-2])
        last_low = float(df["low"].iloc[-2])
        last_close = float(df["close"].iloc[-2])
        last_open = float(df["open"].iloc[-2])

        # Check TP/SL for active trade using LAST bar's high/low
        trade = st.get("active_trade")
        if trade:
            update_signal_mae_mfe(ticker, timeframe, trade["side"], last_close)
            st["bars_in_trade"] = st.get("bars_in_trade", 0) + 1

            hit = check_tp_sl_hit(st, last_high, last_low)
            if hit:
                exit_price = trade["sl"] if hit == "sl" else trade["tp"]
                close_trade(st, exit_price, hit, ticker, timeframe)
            elif st.get("bars_in_trade", 0) >= MAX_HOLD_BARS:
                # Force close after max hold bars
                close_trade(st, last_close, "cancelled", ticker, timeframe)
                print(f"[TRADE] Force-closed {trade['side'].upper()} after {MAX_HOLD_BARS} bars")

        # Indicators
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
        chop_ok = chop_v < CHOP_THRESHOLD

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

        mfi_bull_sig = crossover(mfi, level_os, idx)
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

        confirm_long_a = (mfi_bull_sig and bs_and_bull <= LOOKBACK) or \
                         (and_bull_sig and bs_mfi_bull <= LOOKBACK)
        confirm_short_a = (mfi_bear_sig and bs_and_bear <= LOOKBACK) or \
                          (and_bear_sig and bs_mfi_bear <= LOOKBACK)

        def cooldown_ok(last_bar):
            return last_bar is None or (bar_idx - last_bar) > COOLDOWN_BARS

        a_long_cd_ok = cooldown_ok(st["last_a_long_bar"])
        a_short_cd_ok = cooldown_ok(st["last_a_short_bar"])
        u_long_cd_ok = cooldown_ok(st["last_u_long_bar"])
        u_short_cd_ok = cooldown_ok(st["last_u_short_bar"])

        a_in_pos = st["a_in_long"] or st["a_in_short"]
        u_in_pos = st["u_in_long"] or st["u_in_short"]

        sig_a_long = confirm_long_a and filter_long and not a_in_pos and a_long_cd_ok
        sig_a_short = confirm_short_a and filter_short and not a_in_pos and a_short_cd_ok

        sig_u_long = bool(ut_buy.iloc[idx]) and filter_long and not u_in_pos and u_long_cd_ok and is_new_bar
        sig_u_short = bool(ut_sell.iloc[idx]) and filter_short and not u_in_pos and u_short_cd_ok and is_new_bar

        if sig_a_long:
            st["a_in_long"] = True; st["a_in_short"] = False
            st["a_long_bar"] = bar_idx; st["last_a_long_bar"] = bar_idx
        if sig_a_short:
            st["a_in_short"] = True; st["a_in_long"] = False
            st["a_short_bar"] = bar_idx; st["last_a_short_bar"] = bar_idx
        if sig_u_long:
            st["u_in_long"] = True; st["u_in_short"] = False
            st["u_long_bar"] = bar_idx; st["last_u_long_bar"] = bar_idx
        if sig_u_short:
            st["u_in_short"] = True; st["u_in_long"] = False
            st["u_short_bar"] = bar_idx; st["last_u_short_bar"] = bar_idx

        regime = "CHAOS" if atr_pct_v > ATR_MAX else "TREND" if atr_pct_v > ATR_MIN * 1.5 else "NORMAL"

        frama_sl_long = max(1.0, min(3.5, abs(close_v - float(fl.iloc[idx])) / atr_v))
        frama_sl_short = max(1.0, min(3.5, abs(float(fu.iloc[idx]) - close_v) / atr_v))
        sugg_sl = frama_sl_long if (sig_a_long or sig_u_long) else frama_sl_short
        sugg_lev = max(1, min(MAX_ALLOWED_LEV, math.floor(TARGET_RISK_DEP / max(sugg_sl, 0.1))))
        if regime == "CHAOS": sugg_lev = max(1, math.floor(sugg_lev * 0.5))
        if regime == "TREND": sugg_lev = min(MAX_ALLOWED_LEV, math.floor(sugg_lev * 1.2))

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
        signal_fired = False

        if sig_a_long or sig_u_long:
            sl = calculate_sl(close_v, "long", fs, fu, fl, atr14, idx)
            tp = calculate_adaptive_tp(ticker, timeframe, "long", close_v, sl)
            risk = abs(close_v - sl)
            st["active_trade"] = {"side": "long", "entry": close_v, "sl": sl, "tp": tp, "lev": sugg_lev, "bar_opened": bar_idx}
            st["bars_in_trade"] = 0
            add_signal_record(ticker, timeframe, "long", close_v, datetime.now(timezone.utc).isoformat())
            stats = get_signal_stats(ticker, timeframe, "long")
            if sig_a_long:
                signal_fired = True
                signals.append(("A BUY  (Andean+MFI)", close_v, regime, sugg_lev, bar_time, calc_confidence(True), sl, tp, risk, stats))
            if sig_u_long:
                signal_fired = True
                signals.append(("U BUY  (UT Bot)", close_v, regime, sugg_lev, bar_time, calc_confidence(True), sl, tp, risk, stats))

        if sig_a_short or sig_u_short:
            sl = calculate_sl(close_v, "short", fs, fu, fl, atr14, idx)
            tp = calculate_adaptive_tp(ticker, timeframe, "short", close_v, sl)
            risk = abs(sl - close_v)
            st["active_trade"] = {"side": "short", "entry": close_v, "sl": sl, "tp": tp, "lev": sugg_lev, "bar_opened": bar_idx}
            st["bars_in_trade"] = 0
            add_signal_record(ticker, timeframe, "short", close_v, datetime.now(timezone.utc).isoformat())
            stats = get_signal_stats(ticker, timeframe, "short")
            if sig_a_short:
                signals.append(("A SELL (Andean+MFI)", close_v, regime, sugg_lev, bar_time, calc_confidence(False), sl, tp, risk, stats))
            if sig_u_short:
                signals.append(("U SELL (UT Bot)", close_v, regime, sugg_lev, bar_time, calc_confidence(False), sl, tp, risk, stats))

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


def build_embed(ticker, tf, signal_type, price, regime, leverage, confidence, sl, tp, risk, stats) -> discord.Embed:
    is_long = "BUY" in signal_type
    is_a_track = "Andean" in signal_type
    coin_emoji = "🟡" if "BTC" in ticker else "🔷" if "ETH" in ticker else "🟣"
    track_emoji = "🔵" if is_a_track else "🟢"
    conf_color = "🟢" if confidence >= 80 else "🟡" if confidence >= 60 else "🔴"
    mode_label = "Spot" if MARKET_MODE == "spot" else "Futures"
    ha_label = "HA" if UT_HEIKIN_ASHI else "Normal"
    rr = round(abs(tp - price) / max(risk, 1e-8), 2)
    tp_source = f"📚 Adaptive (last {stats['count']} signals, {TP_PERCENTILE*100:.0f}th %ile)" if stats['count'] >= 5 else "📐 Fixed R:R = 2.0 (not enough history)"
    tp_pct = abs(tp - price) / price * 100

    embed = discord.Embed(
        title=f"🚨 MUFCA v3.0 {coin_emoji} {'📈 LONG' if is_long else '📉 SHORT'}",
        color=discord.Color.green() if is_long else discord.Color.red(),
    )
    embed.add_field(name="📈 Pair", value=f"**{ticker}**", inline=True)
    embed.add_field(name="⏱ TF", value=tf.upper(), inline=True)
    embed.add_field(name=f"{track_emoji} Track", value=signal_type.strip(), inline=True)
    embed.add_field(name="🧬 HTF Bias", value=f"✅ {HTF_BIAS.upper()} FRAMA confirmed", inline=True)
    embed.add_field(name="💵 Entry Price", value=f"${price:,.4f}", inline=True)
    embed.add_field(name="🛑 Stop Loss", value=f"${sl:,.4f}", inline=True)
    embed.add_field(name="🎯 Take Profit", value=f"${tp:,.4f} (+{tp_pct:.2f}%)", inline=True)
    embed.add_field(name="📊 Risk/Reward", value=f"1:{rr}", inline=True)
    embed.add_field(name="📚 TP Source", value=tp_source, inline=False)
    if stats['count'] >= 5:
        embed.add_field(name="📈 Signal Stats", value=f"Avg MFE: {stats['avg_mfe']:.2f}% | Median: {stats['median_mfe']:.2f}% | Best: {stats['best']:.2f}%", inline=False)
    embed.add_field(name="⚙️ Regime", value=regime, inline=True)
    embed.add_field(name="⚠️ Leverage", value=f"x{leverage}", inline=True)
    embed.add_field(name=f"{conf_color} AI Conf", value=f"{confidence}%", inline=True)
    embed.add_field(name="🕯️ UT Bot", value=f"Heikin Ashi: {'✅' if UT_HEIKIN_ASHI else '❌'}", inline=True)
    embed.set_footer(text=f"MUFCA [AtomDC] v3.0 • Gate.io {mode_label} • HTF:{HTF_BIAS.upper()} • UT:{ha_label}")
    return embed


@bot.event
async def on_ready():
    ha_status = "HA ON" if UT_HEIKIN_ASHI else "HA OFF"
    print(f"✅ {bot.user.name} started! Mode: {MARKET_MODE.upper()} | HTF: {HTF_BIAS.upper()} | UT: {ha_status} | Pairs: {' | '.join(TICKERS)}")

    # Run backtest on startup to populate signal history
    run_startup_backtest()

    market_scanner.start()


@bot.command(name="status")
async def status_cmd(ctx):
    ha_status = "✅ ON" if UT_HEIKIN_ASHI else "❌ OFF"
    lines = [f"**MUFCA v3.0 — Scanner Status**\n",
             f"🧬 HTF Bias: **{HTF_BIAS.upper()}**\n",
             f"🕯️ UT Bot Heikin Ashi: **{ha_status}**\n",
             f"📚 Adaptive TP: last **{SIGNAL_HISTORY_LIMIT}** signals | **{TP_PERCENTILE*100:.0f}th** percentile\n"]
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st = state[ticker][tf]
            last = st["last_bar_time"]
            ts = f"<t:{last // 1000}:R>" if last else "no data"
            a_pos = "LONG" if st["a_in_long"] else "SHORT" if st["a_in_short"] else "—"
            u_pos = "LONG" if st["u_in_long"] else "SHORT" if st["u_in_short"] else "—"
            trade = st.get("active_trade")
            trade_info = ""
            if trade:
                trade_info = f" | 🎯 Active: {trade['side'].upper()} @ ${trade['entry']} SL:${trade['sl']} TP:${trade['tp']}"
            lines.append(f"• `{ticker}` `{tf}` — bar: {ts} | A: **{a_pos}** | U: **{u_pos}**{trade_info}")
    await ctx.send("\n".join(lines))


@bot.command(name="scan")
async def scan_cmd(ctx, ticker: str = "BTC/USDT", tf: str = "1h"):
    ticker = ticker.upper(); tf = tf.lower()
    await ctx.send(f"🔍 Scanning `{ticker}` `{tf}`…")
    st = state.get(ticker, {}).get(tf) or make_state()
    signals, bar_time, regime, lev = check_signals(ticker, tf, st)
    if signals:
        for sig_type, price, reg, leverage, bt, conf, sl, tp, risk, stats in signals:
            embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf, sl, tp, risk, stats)
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
    exchange = make_exchange(MARKET_MODE)
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
    old_htf = HTF_BIAS
    HTF_BIAS = new_htf
    save_htf(HTF_BIAS)
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            state[ticker][tf] = make_state()
    await ctx.send(f"🧬 HTF Bias changed: **{old_htf.upper()}** → **{HTF_BIAS.upper()}**\n"
                   f"⚠️ Position states have been reset.")
    print(f"[HTF] Changed from {old_htf} to {HTF_BIAS}")


@bot.command(name="tpconfig")
async def tpconfig_cmd(ctx, param: str = "", value: str = ""):
    global SIGNAL_HISTORY_LIMIT, TP_PERCENTILE
    if not param:
        await ctx.send(f"**📚 Adaptive TP Configuration:**\n"
                       f"• History limit: **{SIGNAL_HISTORY_LIMIT}** signals\n"
                       f"• Percentile: **{TP_PERCENTILE*100:.0f}th** (TP hit in {TP_PERCENTILE*100:.0f}% of cases)\n"
                       f"• Min TP: **{MIN_TP_PCT}%** | Max TP: **{MAX_TP_PCT}%**\n"
                       f"• Max hold: **{MAX_HOLD_BARS}** bars\n"
                       f"\nTo change: `!tpconfig limit 30` or `!tpconfig percentile 70`")
        return
    param = param.lower()
    if param == "limit":
        try:
            new_limit = int(value)
            if new_limit < 5 or new_limit > 200:
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
            if new_pct < 10 or new_pct > 99:
                await ctx.send("❌ Percentile must be between 10 and 99")
                return
            old = TP_PERCENTILE
            TP_PERCENTILE = new_pct / 100
            await ctx.send(f"✅ Percentile changed: **{old*100:.0f}th** → **{new_pct:.0f}th**")
        except ValueError:
            await ctx.send("❌ Invalid number")
    else:
        await ctx.send("❌ Unknown parameter. Use `limit` or `percentile`")


@bot.command(name="history")
async def history_cmd(ctx, ticker: str = "", tf: str = ""):
    if not ticker:
        lines = ["**📊 Trade History:**\n"]
        for t in TICKERS:
            for timeframe in TIMEFRAMES:
                st = state[t][timeframe]
                trades = st.get("trade_history", [])
                if trades:
                    lines.append(f"\n**`{t}` `{timeframe}` — {len(trades)} trades:**")
                    for i, trade in enumerate(trades[-5:], 1):
                        emoji = "🟢" if trade['pnl_pct'] > 0 else "🔴"
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
            emoji = "🟢" if trade['pnl_pct'] > 0 else "🔴"
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
                        emoji = "🟢" if trade['pnl_pct'] > 0 else "🔴"
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
                        wins = sum(1 for r in records if r["moved_pct"] > 0)
                        avg_mfe = np.mean([r["max_favorable_pct"] for r in records])
                        lines.append(f"• `{t}` `{timeframe}` {s.upper()}: {len(records)} signals | Wins: {wins}/{len(records)} | Avg MFE: {avg_mfe:.2f}%")
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
        lines = [f"**📚 `{ticker}` Signal History:**\n"]
        for timeframe in history[ticker]:
            for s in ("long", "short"):
                records = [r for r in history[ticker][timeframe].get(s, []) if r["exit_type"] != "open"]
                if records:
                    wins = sum(1 for r in records if r["moved_pct"] > 0)
                    avg_mfe = np.mean([r["max_favorable_pct"] for r in records])
                    lines.append(f"• `{timeframe}` {s.upper()}: {len(records)} signals | Wins: {wins}/{len(records)} | Avg MFE: {avg_mfe:.2f}%")
        await ctx.send("\n".join(lines))
        return
    tf = tf.lower()
    if tf not in history[ticker]:
        await ctx.send(f"📭 No history for `{ticker}` `{tf}`")
        return
    if not side:
        lines = [f"**📚 `{ticker}` `{tf}` Signal History:**\n"]
        for s in ("long", "short"):
            records = [r for r in history[ticker][tf].get(s, []) if r["exit_type"] != "open"]
            if records:
                wins = sum(1 for r in records if r["moved_pct"] > 0)
                avg_mfe = np.mean([r["max_favorable_pct"] for r in records])
                lines.append(f"• {s.upper()}: {len(records)} signals | Wins: {wins}/{len(records)} | Avg MFE: {avg_mfe:.2f}%")
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
    lines = [f"**📚 `{ticker}` `{tf}` {side.upper()} Signal History ({len(records)} signals):**\n"]
    for i, rec in enumerate(records[-15:], 1):
        emoji = "🟢" if rec["moved_pct"] > 0 else "🔴"
        lines.append(f"{emoji} #{i} Entry: ${rec['entry']} → Exit: ${rec['exit']} | MFE: {rec['max_favorable_pct']:.2f}% | MAE: {rec['max_adverse_pct']:.2f}% | Result: {rec['exit_type'].upper()}")
    await ctx.send("\n".join(lines))





@bot.command(name="debug")
async def debug_cmd(ctx):
    """!debug - show internal scan statistics and signal history status"""
    lines = []
    lines.append("**Debug Information:**")
    lines.append("• Total scans: **" + str(scan_stats["total_scans"]) + "**")
    lines.append("• Signals generated: **" + str(scan_stats["signals_generated"]) + "**")
    lines.append("• Last scan: " + str(scan_stats["last_scan_time"] or "never"))

    history = load_signals_history()
    total_signals = 0
    total_closed = 0
    for t in history:
        for tf in history[t]:
            for side in ("long", "short"):
                records = history[t][tf].get(side, [])
                total_signals += len(records)
                total_closed += sum(1 for r in records if r["exit_type"] != "open")

    lines.append("• Signal history records: **" + str(total_signals) + "** (" + str(total_closed) + " closed)")
    lines.append("• File exists: **" + ("Yes" if os.path.exists(SIGNALS_HISTORY_FILE) else "No") + "**")

    if os.path.exists(SIGNALS_HISTORY_FILE):
        size = os.path.getsize(SIGNALS_HISTORY_FILE)
        lines.append("• File size: **" + str(size) + " bytes**")

    active_count = 0
    for t in TICKERS:
        for tf in TIMEFRAMES:
            if state[t][tf].get("active_trade"):
                active_count += 1
    lines.append("• Active trades: **" + str(active_count) + "**")

    await ctx.send(chr(10).join(lines))


@bot.command(name="sim")
async def sim_cmd(ctx, side: str = "long", ticker: str = "BTC/USDT", tf: str = "1h"):
    """!sim [long|short] [TICKER] [TF] - simulate a signal to test history recording"""
    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("Side must be `long` or `short`")
        return
    ticker = ticker.upper()
    tf = tf.lower()

    await ctx.send("Simulating " + side.upper() + " signal for `" + ticker + "` `" + tf + "`...")

    try:
        bars = exchange.fetch_ohlcv(ticker, tf, limit=100)
        df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        last_close = float(df["close"].iloc[-2])

        atr14 = calculate_atr(df, ATR_PERIOD)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        idx = len(df) - 2
        sl = calculate_sl(last_close, side, fs, fu, fl, atr14, idx)
        tp = calculate_adaptive_tp(ticker, tf, side, last_close, sl)

        add_signal_record(ticker, tf, side, last_close, datetime.now(timezone.utc).isoformat())

        # Simulate TP hit for testing
        exit_price = tp
        update_signal_record(ticker, tf, side, exit_price, "tp", 5)

        stats = get_signal_stats(ticker, tf, side)

        msg = "Simulated signal recorded!" + chr(10)
        msg += "• Entry: $" + str(round(last_close, 4)) + chr(10)
        msg += "• SL: $" + str(round(sl, 4)) + chr(10)
        msg += "• TP: $" + str(round(tp, 4)) + chr(10)
        msg += "• History now has **" + str(stats["count"]) + "** closed " + side + " signals" + chr(10)
        msg += "• Run `!signals " + ticker + " " + tf + " " + side + "` to see details"

        await ctx.send(msg)
    except Exception as e:
        await ctx.send("Simulation failed: " + str(e))
        import traceback
        await ctx.send("```" + chr(10) + traceback.format_exc()[:1000] + chr(10) + "```")



@bot.command(name="forcerun")
async def forcerun_cmd(ctx, side: str = "long", ticker: str = "BTC/USDT", tf: str = "1h"):
    """!forcerun [long|short] [TICKER] [TF] - force a signal bypassing all filters (for testing)"""
    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("Side must be `long` or `short`")
        return
    ticker = ticker.upper()
    tf = tf.lower()

    await ctx.send("Force-running " + side.upper() + " signal for `" + ticker + "` `" + tf + "` (bypassing filters)...")

    try:
        bars = exchange.fetch_ohlcv(ticker, tf, limit=100)
        df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        last_close = float(df["close"].iloc[-2])

        atr14 = calculate_atr(df, ATR_PERIOD)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        idx = len(df) - 2
        sl = calculate_sl(last_close, side, fs, fu, fl, atr14, idx)
        tp = calculate_adaptive_tp(ticker, tf, side, last_close, sl)
        risk = abs(last_close - sl)

        st = state.get(ticker, {}).get(tf) or make_state()

        # Force set active trade
        st["active_trade"] = {"side": side, "entry": last_close, "sl": sl, "tp": tp, "lev": 3, "bar_opened": idx}
        st["bars_in_trade"] = 0

        # Record in history
        add_signal_record(ticker, tf, side, last_close, datetime.now(timezone.utc).isoformat())

        stats = get_signal_stats(ticker, tf, side)

        rr = round(abs(tp - last_close) / max(risk, 1e-8), 2)
        tp_source = "Adaptive (" + str(stats["count"]) + " signals)" if stats["count"] >= 5 else "Fixed R:R = 2.0"
        tp_pct = abs(tp - last_close) / last_close * 100

        embed = discord.Embed(
            title="FORCE SIGNAL " + ("📈 LONG" if side == "long" else "📉 SHORT"),
            color=discord.Color.green() if side == "long" else discord.Color.red(),
        )
        embed.add_field(name="Pair", value="**" + ticker + "**", inline=True)
        embed.add_field(name="TF", value=tf.upper(), inline=True)
        embed.add_field(name="Entry", value="$" + str(round(last_close, 4)), inline=True)
        embed.add_field(name="SL", value="$" + str(round(sl, 4)), inline=True)
        embed.add_field(name="TP", value="$" + str(round(tp, 4)) + " (+" + str(round(tp_pct, 2)) + "%)", inline=True)
        embed.add_field(name="R:R", value="1:" + str(rr), inline=True)
        embed.add_field(name="TP Source", value=tp_source, inline=False)
        if stats["count"] >= 5:
            embed.add_field(name="Stats", value="Avg MFE: " + str(stats["avg_mfe"]) + "% | Median: " + str(stats["median_mfe"]) + "%", inline=False)
        embed.add_field(name="WARNING", value="This signal BYPASSED all filters for testing!", inline=False)

        await ctx.send(embed=embed)
        await ctx.send("Signal recorded. Use `!debug` to verify, `!signals " + ticker + " " + tf + " " + side + "` to see history.")

    except Exception as e:
        await ctx.send("Force run failed: " + str(e))
        import traceback
        await ctx.send("```" + chr(10) + traceback.format_exc()[:1000] + chr(10) + "```")





@bot.command(name="tp")
async def tp_cmd(ctx, side: str = "long", ticker: str = "BTC/USDT", tf: str = "1h"):
    """!tp [long|short] [TICKER] [TF] - preview adaptive TP for current price without trading"""
    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("Side must be `long` or `short`")
        return
    ticker = ticker.upper()
    tf = tf.lower()

    try:
        bars = exchange.fetch_ohlcv(ticker, tf, limit=100)
        df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        last_close = float(df["close"].iloc[-2])

        atr14 = calculate_atr(df, ATR_PERIOD)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        idx = len(df) - 2
        sl = calculate_sl(last_close, side, fs, fu, fl, atr14, idx)
        tp = calculate_adaptive_tp(ticker, tf, side, last_close, sl)

        stats = get_signal_stats(ticker, tf, side)
        risk = abs(last_close - sl)
        rr = round(abs(tp - last_close) / max(risk, 1e-8), 2)
        tp_pct = abs(tp - last_close) / last_close * 100

        lines = []
        lines.append("**📊 Adaptive TP Preview for `" + ticker + "` `" + tf + "` " + side.upper() + ":**")
        lines.append("• Current price: **$" + str(round(last_close, 4)) + "**")
        lines.append("• Stop Loss: **$" + str(round(sl, 4)) + "** (risk: $" + str(round(risk, 4)) + ")")
        lines.append("• Take Profit: **$" + str(round(tp, 4)) + "** (+" + str(round(tp_pct, 2)) + "%)")
        lines.append("• Risk/Reward: **1:" + str(rr) + "**")

        if stats['count'] >= 5:
            lines.append("• Based on **" + str(stats['count']) + "** historical signals")
            lines.append("• Avg MFE: **" + str(stats['avg_mfe']) + "%** | Median: **" + str(stats['median_mfe']) + "%**")
            lines.append("• Best: **" + str(stats['best']) + "%** | Worst: **" + str(stats['worst']) + "%**")
            lines.append("• Used percentile: **" + str(int(TP_PERCENTILE*100)) + "th** → TP%: **" + str(stats['tp_pct']) + "%**")
        else:
            lines.append("• ⚠️ Only **" + str(stats['count']) + "** signals in history — using fallback R:R = 2.0")

        await ctx.send(chr(10).join(lines))

    except Exception as e:
        await ctx.send("Error: " + str(e))

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
                for sig_type, price, reg, leverage, bt, conf, sl, tp, risk, stats in signals:
                    embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf, sl, tp, risk, stats)
                    await channel.send(embed=embed)
                    print(f"[SIGNAL] {ticker} {tf} | {sig_type} @ {price:.4f} | SL:{sl} TP:{tp} | conf={conf}%")

            # Check for trade closure notifications
            trade = st.get("active_trade")
            if not trade and st.get("trade_history"):
                last = st["trade_history"][-1]
                # Only notify if closed in last 30 seconds (avoid spam on restart)
                if last.get("exit_time"):
                    from datetime import datetime, timezone
                    exit_dt = datetime.fromisoformat(last["exit_time"])
                    age = (datetime.now(timezone.utc) - exit_dt).total_seconds()
                    if age < 35:  # Just closed
                        emoji = "🟢" if last['pnl_pct'] > 0 else "🔴"
                        await channel.send(
                            f"{emoji} **Trade Closed** | `{ticker}` `{tf}` | "
                            f"{last['side'].upper()} | Entry: ${last['entry']} → Exit: ${last['exit']} | "
                            f"PnL: **{last['pnl_pct']:.2f}%** | Result: **{last['result'].upper()}** | Bars: {last['bars_held']}"
                        )

            await asyncio.sleep(0.5)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in .env file!")
    bot.run(DISCORD_TOKEN)
