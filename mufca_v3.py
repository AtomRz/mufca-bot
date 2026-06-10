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
# 💾 DYNAMIC PAIRS LIST — persisted to file
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
# ⚙️  INDICATOR PARAMETERS (matching Pine Script defaults)
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
# 🕯️  HEIKIN ASHI SETTING FOR UT BOT
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
# 📡  MARKET MODE — spot or futures
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
# 🗂️  STATE — positions and cooldown per pair/timeframe
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
    }

state: dict = {ticker: {tf: make_state() for tf in TIMEFRAMES} for ticker in TICKERS}

def ensure_state(ticker: str):
    """Add state for a new pair if it doesn't exist."""
    if ticker not in state:
        state[ticker] = {tf: make_state() for tf in TIMEFRAMES}

# =====================================================================
# 📊  INDICATORS — identical to Pine Script
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
    hh      = df["high"].rolling(window=length).max()
    ll      = df["low"].rolling(window=length).min()
    return 100 * np.log10(atr_sum / (hh - ll + 1e-8)) / np.log10(length)


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
    """KMeans clustering identical to Pine Script: init min/max, 10 iterations."""
    vals = mfi.dropna().tail(training_size).values
    if len(vals) < 2:
        return 20.0, 80.0
    c1, c2 = float(vals.min()), float(vals.max())
    for _ in range(10):
        cl1 = vals[np.abs(vals - c1) < np.abs(vals - c2)]
        cl2 = vals[np.abs(vals - c2) <= np.abs(vals - c1)]
        c1  = float(cl1.mean()) if len(cl1) > 0 else c1
        c2  = float(cl2.mean()) if len(cl2) > 0 else c2
    return min(c1, c2), max(c1, c2)


def calculate_andean(df: pd.DataFrame, length: int = 23, sig_len: int = 6):
    """Andean Oscillator — identical to Pine Script."""
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
    """Convert OHLCV DataFrame to Heikin Ashi candles."""
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
    """UT Bot — identical to Pine Script. Optional Heikin Ashi source."""
    df_ut  = heikin_ashi(df) if use_ha else df.copy()
    src    = df_ut["close"].values
    n_loss = (sensitivity * calculate_atr(df_ut, period)).values
    ts     = np.zeros(len(df))
    ts[0]  = src[0]
    for i in range(1, len(df)):
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
    """
    HTF Bias — identical to Pine: htf_bull = htf_close > htf_frama.
    Returns 1 (bull), -1 (bear), 0 (unknown).
    """
    htf = "4h" if timeframe == "1h" else "1d"
    try:
        bars   = exchange.fetch_ohlcv(ticker, htf, limit=150)
        df_htf = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
        fs, fu, fl, fdir = calculate_frama(df_htf, FRAMA_LEN, FRAMA_MULT)
        htf_close = df_htf["close"].iloc[-2]
        htf_frama = fs.iloc[-2]
        bias = 1 if htf_close > htf_frama else -1
        print(f"[HTF] {ticker} {timeframe} → {htf} | close={htf_close:.2f} frama={htf_frama:.2f} bias={'BULL' if bias==1 else 'BEAR'}")
        return bias
    except Exception as e:
        print(f"[WARN] HTF Bias ({ticker} {htf}): {e}")
        return 0


# =====================================================================
# 🧠  SIGNAL LOGIC — identical to Pine Script sections 7-9
# =====================================================================

def check_signals(ticker: str, timeframe: str, st: dict):
    """
    Returns: (signals list, bar_time, regime, leverage)
    signals: [(signal_type, price, regime, leverage, bar_time, confidence), ...]
    """
    try:
        htf_bias = get_htf_bias(ticker, timeframe)
        bars = exchange.fetch_ohlcv(ticker, timeframe, limit=900)
        df   = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])

        # Track new confirmed bars (equivalent to barstate.isconfirmed in Pine)
        current_bar_time = int(df["timestamp"].iloc[-2])
        is_new_bar = st["last_processed_bar_time"] is None or current_bar_time > st["last_processed_bar_time"]
        st["last_processed_bar_time"] = current_bar_time

        # Indicators
        atr14            = calculate_atr(df, ATR_PERIOD)
        atr_pct          = (atr14 / df["close"]) * 100
        chop             = calculate_chop(df, CHOP_LENGTH)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        mfi              = calculate_mfi(df, MFI_LEN)
        level_os, level_ob = run_kmeans_mfi(mfi, MFI_TRAINING)
        and_osc, and_sig = calculate_andean(df, AND_LEN, AND_SIG_LEN)
        ut_buy, ut_sell  = calculate_ut_bot(df, UT_SENSITIVITY, UT_PERIOD, use_ha=UT_HEIKIN_ASHI)

        # Last confirmed bar
        idx      = len(df) - 2
        bar_idx  = idx
        bar_time = int(df["timestamp"].iloc[idx])

        close_v   = float(df["close"].iloc[idx])
        open_v    = float(df["open"].iloc[idx])
        atr_v     = max(float(atr14.iloc[idx]), 1e-8)
        atr_pct_v = float(atr_pct.iloc[idx])
        chop_v    = float(chop.iloc[idx])

        # Base filters
        atr_ok  = ATR_MIN <= atr_pct_v <= ATR_MAX
        chop_ok = chop_v < CHOP_THRESHOLD

        # FRAMA slope filter
        frama_slope = float(fs.iloc[idx]) - float(fs.iloc[idx - 1])
        slope_long  = frama_slope > 0
        slope_short = frama_slope < 0

        frama_dir_v = int(fdir.iloc[idx])
        frama_bull  = frama_dir_v == 1
        frama_bear  = frama_dir_v == -1

        htf_bull = htf_bias == 1
        htf_bear = htf_bias == -1

        # Fake Breakout Filter
        hh10_prev    = float(df["high"].iloc[max(0, idx-10):idx].max())
        ll10_prev    = float(df["low"].iloc[max(0, idx-10):idx].min())
        fake_break_long  = float(df["high"].iloc[idx]) > hh10_prev and close_v < hh10_prev
        fake_break_short = float(df["low"].iloc[idx])  < ll10_prev and close_v > ll10_prev

        # Liquidity Sweep Filter
        ll5_prev = float(df["low"].iloc[max(0, idx-5):idx].min())
        hh5_prev = float(df["high"].iloc[max(0, idx-5):idx].max())
        liq_sweep_long  = float(df["low"].iloc[idx])  < ll5_prev and close_v > ll5_prev and close_v > open_v
        liq_sweep_short = float(df["high"].iloc[idx]) > hh5_prev and close_v < hh5_prev and close_v < open_v

        # Combined filters (Pine Script section 8)
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

        # Debug log
        ha_status = "HA" if UT_HEIKIN_ASHI else "Normal"
        print(f"[DEBUG] {ticker} {timeframe} | UT:{ha_status} | "
              f"frama={'BULL' if frama_bull else 'BEAR' if frama_bear else 'RANGE'} | "
              f"htf={'BULL' if htf_bull else 'BEAR'} | "
              f"slope_L={slope_long} slope_S={slope_short} | "
              f"chop_ok={chop_ok}({chop_v:.1f}) | atr_ok={atr_ok}({atr_pct_v:.2f}%) | "
              f"fake_L={fake_break_long} liq_S={liq_sweep_short} | "
              f"filter_L={filter_long} filter_S={filter_short} | "
              f"new_bar={is_new_bar}")

        # Crossover helpers
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

        # Bars since crossover (confirmation window)
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

        confirm_long_a  = (mfi_bull_sig and bs_and_bull <= LOOKBACK) or \
                          (and_bull_sig and bs_mfi_bull <= LOOKBACK)
        confirm_short_a = (mfi_bear_sig and bs_and_bear <= LOOKBACK) or \
                          (and_bear_sig and bs_mfi_bear <= LOOKBACK)

        # Cooldown check
        def cooldown_ok(last_bar):
            return last_bar is None or (bar_idx - last_bar) > COOLDOWN_BARS

        a_long_cd_ok  = cooldown_ok(st["last_a_long_bar"])
        a_short_cd_ok = cooldown_ok(st["last_a_short_bar"])
        u_long_cd_ok  = cooldown_ok(st["last_u_long_bar"])
        u_short_cd_ok = cooldown_ok(st["last_u_short_bar"])

        # Position guard
        a_in_pos = st["a_in_long"] or st["a_in_short"]
        u_in_pos = st["u_in_long"] or st["u_in_short"]

        # Final signals (Pine Script section 12)
        sig_a_long  = confirm_long_a  and filter_long  and not a_in_pos and a_long_cd_ok
        sig_a_short = confirm_short_a and filter_short and not a_in_pos and a_short_cd_ok

        # UT signals only fire on a new confirmed bar (barstate.isconfirmed equivalent)
        sig_u_long  = bool(ut_buy.iloc[idx])  and filter_long  and not u_in_pos and u_long_cd_ok  and is_new_bar
        sig_u_short = bool(ut_sell.iloc[idx]) and filter_short and not u_in_pos and u_short_cd_ok and is_new_bar

        # Update state
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

        # Market regime
        regime = "CHAOS" if atr_pct_v > ATR_MAX else "TREND" if atr_pct_v > ATR_MIN * 1.5 else "NORMAL"

        # Suggested leverage (Pine Script section 11)
        frama_sl_long  = max(1.0, min(3.5, abs(close_v - float(fl.iloc[idx])) / atr_v))
        frama_sl_short = max(1.0, min(3.5, abs(float(fu.iloc[idx]) - close_v) / atr_v))
        sugg_sl  = frama_sl_long if (sig_a_long or sig_u_long) else frama_sl_short
        sugg_lev = max(1, min(MAX_ALLOWED_LEV, math.floor(TARGET_RISK_DEP / max(sugg_sl, 0.1))))
        if regime == "CHAOS": sugg_lev = max(1, math.floor(sugg_lev * 0.5))
        if regime == "TREND": sugg_lev = min(MAX_ALLOWED_LEV, math.floor(sugg_lev * 1.2))

        # AI Confidence score (Pine Script section 12)
        def calc_confidence(is_long: bool) -> int:
            score  = 20 if chop_ok else 0
            score += 20 if atr_ok  else 0
            score += 15 if (frama_bull if is_long else frama_bear) else 0
            a_sig  = sig_a_long if is_long else sig_a_short
            u_sig  = sig_u_long if is_long else sig_u_short
            score += 25 if (a_sig and u_sig) else 10 if (a_sig or u_sig) else 0
            score += 20 if (htf_bull if is_long else htf_bear) else 0
            return min(score, 100)

        signals = []
        if sig_a_long:
            signals.append(("A BUY  (Andean+MFI)", close_v, regime, sugg_lev, bar_time, calc_confidence(True)))
        if sig_a_short:
            signals.append(("A SELL (Andean+MFI)", close_v, regime, sugg_lev, bar_time, calc_confidence(False)))
        if sig_u_long:
            signals.append(("U BUY  (UT Bot)", close_v, regime, sugg_lev, bar_time, calc_confidence(True)))
        if sig_u_short:
            signals.append(("U SELL (UT Bot)", close_v, regime, sugg_lev, bar_time, calc_confidence(False)))

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


def build_embed(ticker, tf, signal_type, price, regime, leverage, confidence) -> discord.Embed:
    is_long     = "BUY" in signal_type
    is_a_track  = "Andean" in signal_type
    htf_name    = "4H FRAMA" if tf == "1h" else "1D FRAMA"
    coin_emoji  = "🟡" if "BTC" in ticker else "🔷" if "ETH" in ticker else "🟣"
    track_emoji = "🔵" if is_a_track else "🟢"
    conf_color  = "🟢" if confidence >= 80 else "🟡" if confidence >= 60 else "🔴"
    mode_label  = "Spot" if MARKET_MODE == "spot" else "Futures"
    ha_label    = "HA" if UT_HEIKIN_ASHI else "Normal"

    embed = discord.Embed(
        title=f"🚨 MUFCA v3.0 {coin_emoji} {'📈 LONG' if is_long else '📉 SHORT'}",
        color=discord.Color.green() if is_long else discord.Color.red(),
    )
    embed.add_field(name="📈 Pair",               value=f"**{ticker}**",               inline=True)
    embed.add_field(name="⏱ TF",                 value=tf.upper(),                     inline=True)
    embed.add_field(name=f"{track_emoji} Track",  value=signal_type.strip(),            inline=True)
    embed.add_field(name="🧬 HTF Bias",           value=f"✅ {htf_name} confirmed",    inline=True)
    embed.add_field(name="💵 Entry Price",         value=f"${price:,.4f}",              inline=True)
    embed.add_field(name="⚙️ Regime",             value=regime,                         inline=True)
    embed.add_field(name="⚠️ Leverage",           value=f"x{leverage}",                inline=True)
    embed.add_field(name=f"{conf_color} AI Conf", value=f"{confidence}%",              inline=True)
    embed.add_field(name="🕯️ UT Bot",             value=f"Heikin Ashi: {'✅' if UT_HEIKIN_ASHI else '❌'}", inline=True)
    embed.set_footer(text=f"MUFCA [AtomDC] v3.0 • Gate.io {mode_label} • UT:{ha_label}")
    return embed


@bot.event
async def on_ready():
    ha_status = "HA ON" if UT_HEIKIN_ASHI else "HA OFF"
    print(f"✅ {bot.user.name} started! Mode: {MARKET_MODE.upper()} | UT: {ha_status} | Pairs: {' | '.join(TICKERS)}")
    market_scanner.start()


@bot.command(name="status")
async def status_cmd(ctx):
    """!status — show scanner status for all pairs"""
    ha_status = "✅ ON" if UT_HEIKIN_ASHI else "❌ OFF"
    lines = [f"**MUFCA v3.0 — Scanner Status**\n",
             f"🕯️ UT Bot Heikin Ashi: **{ha_status}**\n"]
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st    = state[ticker][tf]
            last  = st["last_bar_time"]
            ts    = f"<t:{last // 1000}:R>" if last else "no data"
            a_pos = "LONG" if st["a_in_long"] else "SHORT" if st["a_in_short"] else "—"
            u_pos = "LONG" if st["u_in_long"] else "SHORT" if st["u_in_short"] else "—"
            lines.append(f"• `{ticker}` `{tf}` — bar: {ts} | A: **{a_pos}** | U: **{u_pos}**")
    await ctx.send("\n".join(lines))


@bot.command(name="scan")
async def scan_cmd(ctx, ticker: str = "BTC/USDT", tf: str = "1h"):
    """!scan [TICKER] [TF] — manual signal request. Example: !scan ETH/USDT 4h"""
    ticker = ticker.upper(); tf = tf.lower()
    await ctx.send(f"🔍 Scanning `{ticker}` `{tf}`…")
    st = state.get(ticker, {}).get(tf) or make_state()
    signals, bar_time, regime, lev = check_signals(ticker, tf, st)
    if signals:
        for sig_type, price, reg, leverage, bt, conf in signals:
            embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf)
            await ctx.send(embed=embed)
    else:
        await ctx.send(f"⏳ No signals for `{ticker}` `{tf}`. Regime: **{regime}**")


@bot.command(name="pairs")
async def pairs_cmd(ctx):
    """!pairs — show current scanned pairs"""
    if not TICKERS:
        await ctx.send("📭 Pair list is empty.")
        return
    lines = ["**📋 Scanned Pairs:**\n"]
    for t in TICKERS:
        lines.append(f"• `{t}`")
    await ctx.send("\n".join(lines))


@bot.command(name="add")
async def add_cmd(ctx, ticker: str = ""):
    """!add SOL/USDT — add a pair to the scanner"""
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
    """!remove SOL/USDT — remove a pair from the scanner"""
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
    """!mode spot | !mode futures — switch exchange market type"""
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
    """!utha on | !utha off — enable/disable Heikin Ashi for UT Bot"""
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
                for sig_type, price, reg, leverage, bt, conf in signals:
                    embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf)
                    await channel.send(embed=embed)
                    print(f"[SIGNAL] {ticker} {tf} | {sig_type} @ {price:.4f} | conf={conf}%")

            await asyncio.sleep(0.5)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in .env file!")
    bot.run(DISCORD_TOKEN)
