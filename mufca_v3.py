import os
import asyncio
import math
import numpy as np
import pandas as pd
import ccxt
import discord
from discord.ext import tasks, commands
from dotenv import load_dotenv

load_dotenv()

# =====================================================================
# ⚙️  НАСТРОЙКИ
# =====================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CHANNEL_NAME  = os.getenv("CHANNEL_NAME", "general")

DEFAULT_TICKERS = ["BTC/USDT", "ETH/USDT"]
TIMEFRAMES      = ["1h", "4h"]
PAIRS_FILE      = "pairs.json"

# =====================================================================
# 💾 ДИНАМИЧЕСКИЙ СПИСОК ПАР — сохраняется в файл
# =====================================================================
import json

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
        print(f"[WARN] Не удалось сохранить пары: {e}")

# Глобальный список пар — изменяется командами
TICKERS: list[str] = load_tickers()

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

exchange = ccxt.gate({"enableRateLimit": True})

# =====================================================================
# 🗂️  СОСТОЯНИЕ
# =====================================================================
def make_state():
    return {
        "a_in_long":        False,
        "a_in_short":       False,
        "a_long_bar":       None,
        "a_short_bar":      None,
        "u_in_long":        False,
        "u_in_short":       False,
        "u_long_bar":       None,
        "u_short_bar":      None,
        "last_a_long_bar":  None,
        "last_a_short_bar": None,
        "last_u_long_bar":  None,
        "last_u_short_bar": None,
        "last_bar_time":    None,
    }

state: dict = {ticker: {tf: make_state() for tf in TIMEFRAMES} for ticker in TICKERS}

def ensure_state(ticker: str):
    """Добавить состояние для новой пары если его нет."""
    if ticker not in state:
        state[ticker] = {tf: make_state() for tf in TIMEFRAMES}

# =====================================================================
# 📊 ИНДИКАТОРЫ
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

    close = df["close"].values
    frama_ma = np.zeros(len(df))
    frama_ma[0] = close[0]
    for i in range(1, len(df)):
        frama_ma[i] = alpha[i] * close[i] + (1.0 - alpha[i]) * frama_ma[i - 1]

    fs = pd.Series(frama_ma, index=df.index)

    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift()).abs()
    lc  = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
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
        cl1 = vals[np.abs(vals - c1) < np.abs(vals - c2)]
        cl2 = vals[np.abs(vals - c2) <= np.abs(vals - c1)]
        c1  = float(cl1.mean()) if len(cl1) > 0 else c1
        c2  = float(cl2.mean()) if len(cl2) > 0 else c2
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


def calculate_ut_bot(df: pd.DataFrame, sensitivity: float = 1.0, period: int = 10):
    src    = df["close"].values
    n_loss = (sensitivity * calculate_atr(df, period)).values
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
    src_s   = df["close"]
    ut_buy  = (src_s > ts_s) & (src_s.shift(1) <= ts_s.shift(1))
    ut_sell = (src_s < ts_s) & (src_s.shift(1) >= ts_s.shift(1))
    return ut_buy, ut_sell


def get_htf_bias(ticker: str, timeframe: str) -> int:
    """
    FIX: HTF всегда 4H для 1H и 1D для 4H — идентично Pine дефолту.
    htf_bull = htf_close > htf_frama
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
# 🧠 ГЛАВНАЯ ЛОГИКА СИГНАЛОВ
# =====================================================================

def check_signals(ticker: str, timeframe: str, st: dict):
    try:
        htf_bias = get_htf_bias(ticker, timeframe)
        bars = exchange.fetch_ohlcv(ticker, timeframe, limit=900)
        df   = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])

        atr14    = calculate_atr(df, ATR_PERIOD)
        atr_pct  = (atr14 / df["close"]) * 100
        chop     = calculate_chop(df, CHOP_LENGTH)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        mfi      = calculate_mfi(df, MFI_LEN)
        level_os, level_ob = run_kmeans_mfi(mfi, MFI_TRAINING)
        and_osc, and_sig   = calculate_andean(df, AND_LEN, AND_SIG_LEN)
        ut_buy, ut_sell    = calculate_ut_bot(df, UT_SENSITIVITY, UT_PERIOD)

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

        # Fake Breakout Filter
        hh10_prev = float(df["high"].iloc[max(0, idx-10):idx].max())
        ll10_prev = float(df["low"].iloc[max(0, idx-10):idx].min())
        fake_break_long  = float(df["high"].iloc[idx]) > hh10_prev and close_v < hh10_prev
        fake_break_short = float(df["low"].iloc[idx])  < ll10_prev and close_v > ll10_prev

        # Liquidity Sweep Filter
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

        # DEBUG лог — видим почему сигнал есть или нет
        print(f"[DEBUG] {ticker} {timeframe} | "
              f"frama={'BULL' if frama_bull else 'BEAR' if frama_bear else 'RANGE'} | "
              f"htf={'BULL' if htf_bull else 'BEAR'} | "
              f"slope_L={slope_long} slope_S={slope_short} | "
              f"chop_ok={chop_ok}({chop_v:.1f}) | atr_ok={atr_ok}({atr_pct_v:.2f}%) | "
              f"fake_L={fake_break_long} liq_S={liq_sweep_short} | "
              f"filter_L={filter_long} filter_S={filter_short}")

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

        confirm_long_a  = (mfi_bull_sig and bs_and_bull <= LOOKBACK) or \
                          (and_bull_sig and bs_mfi_bull <= LOOKBACK)
        confirm_short_a = (mfi_bear_sig and bs_and_bear <= LOOKBACK) or \
                          (and_bear_sig and bs_mfi_bear <= LOOKBACK)

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
        sig_u_long  = bool(ut_buy.iloc[idx])  and filter_long  and not u_in_pos and u_long_cd_ok
        sig_u_short = bool(ut_sell.iloc[idx]) and filter_short and not u_in_pos and u_short_cd_ok

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
# 🤖 DISCORD БОТ
# =====================================================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def build_embed(ticker, tf, signal_type, price, regime, leverage, confidence) -> discord.Embed:
    is_long     = "BUY" in signal_type
    is_a_track  = "Andean" in signal_type
    htf_name    = "4H FRAMA" if tf == "1h" else "1D FRAMA"
    coin_emoji  = "🟡" if "BTC" in ticker else "🔷"
    track_emoji = "🔵" if is_a_track else "🟢"
    conf_color  = "🟢" if confidence >= 80 else "🟡" if confidence >= 60 else "🔴"

    embed = discord.Embed(
        title=f"🚨 MUFCA v3.0 {coin_emoji} {'📈 LONG' if is_long else '📉 SHORT'}",
        color=discord.Color.green() if is_long else discord.Color.red(),
    )
    embed.add_field(name="📈 Пара",               value=f"**{ticker}**",               inline=True)
    embed.add_field(name="⏱ ТФ",                 value=tf.upper(),                     inline=True)
    embed.add_field(name=f"{track_emoji} Трек",   value=signal_type.strip(),            inline=True)
    embed.add_field(name="🧬 HTF Bias",           value=f"✅ {htf_name} подтверждён",  inline=True)
    embed.add_field(name="💵 Цена входа",          value=f"${price:,.4f}",              inline=True)
    embed.add_field(name="⚙️ Режим",              value=regime,                         inline=True)
    embed.add_field(name="⚠️ Плечо",              value=f"x{leverage}",                inline=True)
    embed.add_field(name=f"{conf_color} AI Conf", value=f"{confidence}%",              inline=True)
    embed.set_footer(text="MUFCA [AtomDC] v3.0 • Идентичная логика индикатора")
    return embed


@bot.event
async def on_ready():
    print(f"✅ {bot.user.name} запущен! Пары: {' | '.join(TICKERS)}")
    market_scanner.start()


@bot.command(name="status")
async def status_cmd(ctx):
    lines = ["**MUFCA v3.0 — статус сканера**\n"]
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st    = state[ticker][tf]
            last  = st["last_bar_time"]
            ts    = f"<t:{last // 1000}:R>" if last else "нет данных"
            a_pos = "LONG" if st["a_in_long"] else "SHORT" if st["a_in_short"] else "—"
            u_pos = "LONG" if st["u_in_long"] else "SHORT" if st["u_in_short"] else "—"
            lines.append(f"• `{ticker}` `{tf}` — бар: {ts} | A: **{a_pos}** | U: **{u_pos}**")
    await ctx.send("\n".join(lines))


@bot.command(name="scan")
async def scan_cmd(ctx, ticker: str = "BTC/USDT", tf: str = "1h"):
    """!scan [TICKER] [TF]  пример: !scan ETH/USDT 4h"""
    ticker = ticker.upper(); tf = tf.lower()
    await ctx.send(f"🔍 Сканирую `{ticker}` `{tf}`…")
    st = state.get(ticker, {}).get(tf) or make_state()
    signals, bar_time, regime, lev = check_signals(ticker, tf, st)
    if signals:
        for sig_type, price, reg, leverage, bt, conf in signals:
            embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf)
            await ctx.send(embed=embed)
    else:
        await ctx.send(f"⏳ Сигналов по `{ticker}` `{tf}` нет. Режим: **{regime}**")


@bot.command(name="pairs")
async def pairs_cmd(ctx):
    """!pairs — показать текущий список пар"""
    if not TICKERS:
        await ctx.send("📭 Список пар пуст.")
        return
    lines = ["**📋 Сканируемые пары:**\n"]
    for t in TICKERS:
        lines.append(f"• `{t}`")
    await ctx.send("\n".join(lines))


@bot.command(name="add")
async def add_cmd(ctx, ticker: str = ""):
    """!add SOL/USDT — добавить пару в сканер"""
    if not ticker:
        await ctx.send("❌ Укажи пару. Пример: `!add SOL/USDT`")
        return
    ticker = ticker.upper()
    if ticker in TICKERS:
        await ctx.send(f"⚠️ `{ticker}` уже в списке.")
        return
    # Проверяем что пара существует на бирже
    await ctx.send(f"🔍 Проверяю `{ticker}` на Gate.io…")
    try:
        markets = exchange.load_markets()
        if ticker not in markets:
            await ctx.send(f"❌ Пара `{ticker}` не найдена на Gate.io.")
            return
    except Exception as e:
        await ctx.send(f"❌ Ошибка проверки: {e}")
        return
    TICKERS.append(ticker)
    ensure_state(ticker)
    save_tickers(TICKERS)
    await ctx.send(f"✅ `{ticker}` добавлен! Сканируется: {' | '.join(TICKERS)}")
    print(f"[PAIRS] Добавлена пара: {ticker}")


@bot.command(name="remove")
async def remove_cmd(ctx, ticker: str = ""):
    """!remove SOL/USDT — убрать пару из сканера"""
    if not ticker:
        await ctx.send("❌ Укажи пару. Пример: `!remove SOL/USDT`")
        return
    ticker = ticker.upper()
    if ticker not in TICKERS:
        await ctx.send(f"⚠️ `{ticker}` нет в списке.")
        return
    if len(TICKERS) == 1:
        await ctx.send("❌ Нельзя удалить последнюю пару.")
        return
    TICKERS.remove(ticker)
    save_tickers(TICKERS)
    await ctx.send(f"🗑️ `{ticker}` удалён. Осталось: {' | '.join(TICKERS)}")
    print(f"[PAIRS] Удалена пара: {ticker}")



@tasks.loop(seconds=20)
async def market_scanner():
    channel = discord.utils.get(bot.get_all_channels(), name=CHANNEL_NAME)
    if channel is None:
        print(f"[WARN] Канал '{CHANNEL_NAME}' не найден!")
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

            await asyncio.sleep(0.5)  # FIX: был time.sleep — блокировал async loop


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Не задан DISCORD_TOKEN в .env файле!")
    bot.run(DISCORD_TOKEN)
