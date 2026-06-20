import asyncio
import math
import os
import numpy as np
import pandas as pd
import discord
from discord.ext import tasks, commands
from datetime import datetime, timezone
import logging
from typing import Optional

import ccxt

from config import (
    DISCORD_TOKEN,
    CHANNEL_NAME,
    TICKERS,
    TIMEFRAMES,
    DATA_DIR,
    SIGNAL_HISTORY_LIMIT,
    TP_PERCENTILE,
    SAFE_TP_PERCENTILE,
    USE_SAFE_TP,
    MIN_TP_PCT,
    MAX_TP_PCT,
    MAX_HOLD_BARS,
    UT_HEIKIN_ASHI,
    HTF_BIAS,
    MARKET_MODE,
    CHOP_THRESHOLD,
    PAIRS_FILE,
    SIGNALS_HISTORY_FILE,
    ATR_PERIOD,
    FRAMA_LEN,
    FRAMA_MULT,
    save_tickers,
)
from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe
from indicators import calculate_atr, calculate_frama
from signals import check_signals, backtest_history, make_state, get_signal_stats, calculate_sl
from state import load_signals_history, calculate_combined_tp, add_signal_record, update_signal_record

logger = logging.getLogger(__name__)

# =====================================================================
# 🤖  DISCORD BOT
# =====================================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Глобальное состояние
state = {ticker: {tf: make_state() for tf in TIMEFRAMES} for ticker in TICKERS}
scan_stats = {"total_scans": 0, "signals_generated": 0, "last_scan_time": None}

# =====================================================================
# 🚀  ЗАПУСК
# =====================================================================

async def startup_sequence(exchange: ccxt.Exchange):
    """Запуск: сначала бэктест, потом сканер."""
    logger.info("=" * 60)
    logger.info("[STARTUP] Running historical backtest to populate signal history...")
    logger.info("=" * 60)

    total = 0
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            count = await asyncio.to_thread(
                backtest_history, exchange, ticker, tf, 3000
            )
            total += count
            await asyncio.sleep(0.5)

    logger.info("=" * 60)
    logger.info(f"[STARTUP] Backtest complete! Total historical signals: {total}")
    logger.info("=" * 60)

    if not market_scanner.is_running():
        market_scanner.start()

# =====================================================================
# 📊  BUILDER EMBED
# =====================================================================

def build_embed(ticker, tf, signal_type, price, regime, leverage, confidence,
                sl, tp, risk, stats, tp_desc: str = "") -> discord.Embed:
    is_long = "BUY" in signal_type
    is_a_track = "Andean" in signal_type
    coin_emoji = "🟡" if "BTC" in ticker else "🔷" if "ETH" in ticker else "🟣"
    track_emoji = "🔵" if is_a_track else "🟢"
    conf_color = "🟢" if confidence >= 80 else "🟡" if confidence >= 60 else "🔴"
    mode_label = "Spot" if MARKET_MODE == "spot" else "Futures"
    ha_label = "HA" if UT_HEIKIN_ASHI else "Normal"
    rr = round(abs(tp - price) / max(risk, 1e-8), 2)
    tp_pct = abs(tp - price) / price * 100

    active_pct = SAFE_TP_PERCENTILE if USE_SAFE_TP else TP_PERCENTILE
    active_mode = "SAFE" if USE_SAFE_TP else "AGGR"
    tp_source = (
        f"📚 Adaptive (last {stats['count']} signals, {active_pct*100:.0f}th %ile [{active_mode}])"
        if stats["count"] >= 5 else "📐 Fixed R:R = 2.0"
    )

    embed = discord.Embed(
        title=f"🚨 MUFCA v3.1 {coin_emoji} {'📈 LONG' if is_long else '📉 SHORT'}",
        color=discord.Color.green() if is_long else discord.Color.red(),
    )
    embed.add_field(name="📈 Pair", value=f"**{ticker}**", inline=True)
    embed.add_field(name="⏱ TF", value=tf.upper(), inline=True)
    embed.add_field(name=f"{track_emoji} Track", value=signal_type.strip(), inline=True)
    embed.add_field(name="🧬 HTF Bias", value=f"✅ {HTF_BIAS.upper()} FRAMA confirmed", inline=True)
    embed.add_field(name="💵 Entry", value=f"${round(price, 2):,.2f}", inline=True)
    embed.add_field(name="🛑 Stop Loss", value=f"${round(sl, 2):,.2f}", inline=True)
    embed.add_field(name="🎯 Take Profit", value=f"${round(tp, 2):,.2f} (+{tp_pct:.2f}%)", inline=True)
    embed.add_field(name="📊 Risk/Reward", value=f"1:{rr}", inline=True)
    embed.add_field(name="⚙️ Regime", value=regime, inline=True)
    embed.add_field(name="⚠️ Leverage", value=f"x{leverage}", inline=True)
    embed.add_field(name=f"{conf_color} AI Conf", value=f"{confidence}%", inline=True)
    embed.add_field(name="🕯️ UT Bot", value=f"Heikin Ashi: {'✅' if UT_HEIKIN_ASHI else '❌'}", inline=True)
    embed.add_field(name="📚 TP Source", value=tp_source, inline=False)
    if stats["count"] >= 5:
        embed.add_field(name="📈 Signal Stats",
                        value=f"Avg MFE: {stats['avg_mfe']:.2f}% | Best: {stats['best']:.2f}% | Signals: {stats['count']}",
                        inline=False)
    if tp_desc:
        embed.add_field(name="🧠 TP Logic", value=tp_desc, inline=False)
    embed.set_footer(text=f"MUFCA [AtomDC] v3.1 • Gate.io {mode_label} • HTF:{HTF_BIAS.upper()} • UT:{ha_label}")
    return embed

# =====================================================================
# 📡  SCANNER LOOP
# =====================================================================

@tasks.loop(seconds=20)
async def market_scanner():
    """Основной цикл сканирования."""
    channel = discord.utils.get(bot.get_all_channels(), name=CHANNEL_NAME)
    if channel is None:
        logger.warning(f"[WARN] Channel '{CHANNEL_NAME}' not found!")
        return

    exchange = getattr(market_scanner, "exchange", None)
    if exchange is None:
        return

    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            try:
                st = state[ticker][tf]
                signals, bar_time, regime, lev = await check_signals(exchange, ticker, tf, st)

                scan_stats["total_scans"] += 1
                scan_stats["last_scan_time"] = datetime.now(timezone.utc).isoformat()

                if bar_time and bar_time != st["last_bar_time"]:
                    st["last_bar_time"] = bar_time
                    for sig_type, price, reg, leverage, bt, conf, sl, tp, risk, stats, tp_desc in signals:
                        embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf, sl, tp, risk, stats, tp_desc)
                        try:
                            await channel.send(embed=embed)
                            scan_stats["signals_generated"] += 1  # ✅ ИСПРАВЛЕНО
                        except discord.HTTPException as e:
                            logger.error(f"Failed to send signal: {e}")
                        logger.info(f"[SIGNAL] {ticker} {tf} | {sig_type} @ {price:.4f}")

                # Уведомление о закрытии
                trade = st.get("active_trade")
                if not trade and st.get("trade_history") and not st.get("last_closure_notified", False):
                    last = st["trade_history"][-1]
                    if last.get("exit_time"):
                        try:
                            exit_dt = datetime.fromisoformat(last["exit_time"])
                            age = (datetime.now(timezone.utc) - exit_dt).total_seconds()
                            if age < 35:
                                emoji = "🟢" if last["pnl_pct"] > 0 else "🔴"
                                await channel.send(
                                    f"{emoji} **Trade Closed** | `{ticker}` `{tf}` | "
                                    f"{last['side'].upper()} | Entry: ${round(last['entry'], 2)} → Exit: ${round(last['exit'], 2)} | "
                                    f"PnL: **{last['pnl_pct']:.2f}%** | Result: **{last['result'].upper()}** | Bars: {last['bars_held']}"
                                )
                                st["last_closure_notified"] = True
                        except ValueError:
                            logger.warning(f"Invalid exit_time format: {last.get('exit_time')}")

                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Scanner error for {ticker} {tf}: {e}", exc_info=True)
                await asyncio.sleep(0.5)

# =====================================================================
# 🤖  DISCORD COMMANDS
# =====================================================================

@bot.event
async def on_ready():
    logger.info(f"✅ {bot.user.name} started! Mode: {MARKET_MODE.upper()} | HTF: {HTF_BIAS.upper()} | Pairs: {' | '.join(TICKERS)}")

    if MARKET_MODE == "futures":
        exchange = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    else:
        exchange = ccxt.gate({"enableRateLimit": True})

    setattr(market_scanner, "exchange", exchange)
    asyncio.create_task(startup_sequence(exchange))

@bot.command(name="status")
async def status_cmd(ctx):
    ha_status = "✅ ON" if UT_HEIKIN_ASHI else "❌ OFF"
    lines = [
        f"**MUFCA v3.1 — Scanner Status**\n",
        f"🧬 HTF Bias: **{HTF_BIAS.upper()}**\n",
        f"🕯️ UT Bot Heikin Ashi: **{ha_status}**\n",
        f"📚 Adaptive TP: last **{SIGNAL_HISTORY_LIMIT}** signals | **{(SAFE_TP_PERCENTILE if USE_SAFE_TP else TP_PERCENTILE)*100:.0f}th** percentile ({'SAFE 🛡️' if USE_SAFE_TP else 'AGGRESSIVE ⚡'})\n",
    ]
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st = state[ticker][tf]
            last = st["last_bar_time"]
            ts = f"<t:{int(last) // 1000}:R>" if last else "no data"
            a_pos = "LONG" if st["a_in_long"] else "SHORT" if st["a_in_short"] else "—"
            u_pos = "LONG" if st["u_in_long"] else "SHORT" if st["u_in_short"] else "—"
            trade = st.get("active_trade")
            trade_info = ""
            if trade:
                trade_info = f" | 🎯 {trade['side'].upper()} @ ${round(trade['entry'], 2)} SL:${round(trade['sl'], 2)} TP:${round(trade['tp'], 2)}"
            lines.append(f"• `{ticker}` `{tf}` — bar: {ts} | A: **{a_pos}** | U: **{u_pos}**{trade_info}")

    msg = "\n".join(lines)
    if len(msg) > 1900:
        await ctx.send(msg[:1900] + "\n... (truncated)")
    else:
        await ctx.send(msg)

@bot.command(name="scan")
async def scan_cmd(ctx, ticker: str = "BTC/USDT", tf: str = "1h"):
    ticker = ticker.upper()
    tf = tf.lower()

    if tf not in TIMEFRAMES:
        await ctx.send(f"❌ Unknown timeframe. Available: {', '.join(TIMEFRAMES)}")
        return

    await ctx.send(f"🔍 Scanning `{ticker}` `{tf}`…")

    exchange = getattr(market_scanner, "exchange", None)
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    # ✅ ИСПРАВЛЕНО: используем временный state, чтобы не мутировать основной
    temp_state = make_state()
    try:
        signals, bar_time, regime, lev = await check_signals(exchange, ticker, tf, temp_state)
    except Exception as e:
        logger.error(f"Manual scan error: {e}", exc_info=True)
        await ctx.send(f"❌ Scan error: {e}")
        return

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

    exchange = getattr(market_scanner, "exchange", None)
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    await ctx.send(f"🔍 Checking `{ticker}` on Gate.io…")
    try:
        markets = await asyncio.to_thread(exchange.load_markets)
        if ticker not in markets:
            await ctx.send(f"❌ Pair `{ticker}` not found on Gate.io.")
            return
    except Exception as e:
        await ctx.send(f"❌ Check failed: {e}")
        return

    TICKERS.append(ticker)
    state[ticker] = {tf: make_state() for tf in TIMEFRAMES}

    # ✅ ИСПРАВЛЕНО: сохраняем через функцию config
    save_tickers(TICKERS)

    # ✅ ИСПРАВЛЕНО: запускаем бэктест для новой пары
    await ctx.send(f"🔄 Running backtest for `{ticker}`...")
    total = 0
    for tf in TIMEFRAMES:
        try:
            count = await asyncio.to_thread(backtest_history, exchange, ticker, tf, 3000)
            total += count
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Backtest error for {ticker} {tf}: {e}")

    await ctx.send(f"✅ `{ticker}` added! Backtest: {total} signals. Scanning: {' | '.join(TICKERS)}")

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
    if ticker in state:
        del state[ticker]

    save_tickers(TICKERS)

    await ctx.send(f"🗑️ `{ticker}` removed. Remaining: {' | '.join(TICKERS)}")

@bot.command(name="mode")
async def mode_cmd(ctx, new_mode: str = ""):
    global MARKET_MODE
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

    from config import MODE_FILE, save_mode
    save_mode(MARKET_MODE)

    if MARKET_MODE == "futures":
        exchange = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    else:
        exchange = ccxt.gate({"enableRateLimit": True})
    setattr(market_scanner, "exchange", exchange)

    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st = make_state()
            try:
                bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=3)
                if bars and len(bars) >= 2:
                    st["last_bar_time"] = int(bars[-2][0])
                    st["last_processed_bar_time"] = int(bars[-2][0])
            except Exception:
                pass
            state[ticker][tf] = st

    label = "🔵 Spot (Gate.io Spot)" if MARKET_MODE == "spot" else "🟠 Futures (Gate.io Perpetual)"
    await ctx.send(f"✅ Switched to **{label}**\n⚠️ Position states have been reset.")

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
    from config import save_ut_ha
    save_ut_ha(UT_HEIKIN_ASHI)
    status = "✅ ENABLED" if UT_HEIKIN_ASHI else "❌ DISABLED"
    await ctx.send(f"🕯️ Heikin Ashi for UT Bot **{status}**.")

@bot.command(name="htf")
async def htf_cmd(ctx, new_htf: str = ""):
    global HTF_BIAS
    if not new_htf:
        await ctx.send(f"🧬 Current HTF Bias: **{HTF_BIAS.upper()}**\n"
                       f"Available: `1d`, `4h`, `1h`, `1w`\n"
                       f"To change: `!htf 4h`")
        return

    new_htf = new_htf.lower()
    valid_htfs = ("1d", "4h", "2h", "6h", "12h", "1w", "3d")
    if new_htf not in valid_htfs:
        await ctx.send(f"❌ Valid HTF values: {', '.join(valid_htfs)}")
        return

    if new_htf == HTF_BIAS:
        await ctx.send(f"⚠️ HTF Bias is already **{HTF_BIAS.upper()}**.")
        return

    old_htf = HTF_BIAS
    HTF_BIAS = new_htf
    from config import save_htf
    save_htf(HTF_BIAS)

    exchange = getattr(market_scanner, "exchange", None)
    if exchange:
        for ticker in TICKERS:
            for tf in TIMEFRAMES:
                st = make_state()
                try:
                    bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=3)
                    if bars and len(bars) >= 2:
                        st["last_bar_time"] = int(bars[-2][0])
                        st["last_processed_bar_time"] = int(bars[-2][0])
                except Exception:
                    pass
                state[ticker][tf] = st

    await ctx.send(f"🧬 HTF Bias changed: **{old_htf.upper()}** → **{HTF_BIAS.upper()}**\n"
                   f"⚠️ Position states have been reset.")

@bot.command(name="tpconfig")
async def tpconfig_cmd(ctx, param: str = "", value: str = ""):
    global SIGNAL_HISTORY_LIMIT, TP_PERCENTILE, SAFE_TP_PERCENTILE, USE_SAFE_TP
    active_pct = SAFE_TP_PERCENTILE if USE_SAFE_TP else TP_PERCENTILE
    active_mode = "SAFE 🛡️" if USE_SAFE_TP else "AGGRESSIVE ⚡"

    if not param:
        await ctx.send(
            f"**📚 Adaptive TP Configuration:**\n"
            f"• Active mode: **{active_mode}** | Percentile: **{active_pct*100:.0f}th**\n"
            f"• Aggressive percentile: **{TP_PERCENTILE*100:.0f}th**\n"
            f"• Safe percentile: **{SAFE_TP_PERCENTILE*100:.0f}th**\n"
            f"• History limit: **{SIGNAL_HISTORY_LIMIT}** signals\n"
            f"• Min TP: **{MIN_TP_PCT}%** | Max TP: **{MAX_TP_PCT}%**\n"
            f"• Max hold: **{MAX_HOLD_BARS}** bars\n"
            f"\nTo change: `!tpconfig mode safe` | `!tpconfig mode aggressive` | "
            f"`!tpconfig limit 30` | `!tpconfig percentile 70` | `!tpconfig safe 50`"
        )
        return

    param = param.lower()
    if param == "mode":
        if value.lower() == "safe":
            USE_SAFE_TP = True
            await ctx.send(f"🛡️ **Safe mode enabled** — TP now uses **{SAFE_TP_PERCENTILE*100:.0f}th percentile**.")
        elif value.lower() in ("aggressive", "aggr"):
            USE_SAFE_TP = False
            await ctx.send(f"⚡ **Aggressive mode enabled** — TP now uses **{TP_PERCENTILE*100:.0f}th percentile**.")
        else:
            await ctx.send("❌ Mode must be `safe` or `aggressive`")
    elif param == "limit":
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
    else:
        await ctx.send("❌ Unknown parameter. Use `mode`, `limit`, `percentile`, or `safe`")

@bot.command(name="chop")
async def chop_cmd(ctx, tf: str = "", value: str = ""):
    if not tf:
        lines = ["**📊 CHOP Threshold Settings:**"]
        for t, v in CHOP_THRESHOLD.items():
            lines.append(f"• `{t}`: **{v}** (below = trend, above = sideways)")
        lines.append("\nTo change: `!chop 1h 55` or `!chop 4h 61.8`")
        await ctx.send("\n".join(lines))
        return

    tf = tf.lower()
    if tf not in TIMEFRAMES:
        await ctx.send(f"❌ Unknown timeframe. Available: {', '.join(f'`{t}`' for t in TIMEFRAMES)}")
        return

    if not value:
        current = CHOP_THRESHOLD.get(tf, 61.8)
        await ctx.send(f"📊 CHOP threshold for `{tf}`: **{current}**\nTo change: `!chop {tf} 55`")
        return

    try:
        new_val = float(value)
        if not (20.0 <= new_val <= 90.0):
            await ctx.send("❌ Value must be between 20 and 90")
            return
        old = CHOP_THRESHOLD.get(tf, 61.8)
        CHOP_THRESHOLD[tf] = new_val
        await ctx.send(f"✅ CHOP threshold for `{tf}` changed: **{old}** → **{new_val}**")
    except ValueError:
        await ctx.send("❌ Invalid number")

@bot.command(name="history")
async def history_cmd(ctx, ticker: str = "", tf: str = ""):
    lines = []

    if not ticker:
        lines = ["**📊 Trade History:**\n"]
        for t in TICKERS:
            for timeframe in TIMEFRAMES:
                st = state[t][timeframe]
                trades = st.get("trade_history", [])
                if trades:
                    lines.append(f"\n**`{t}` `{timeframe}` — {len(trades)} trades:**")
                    for i, trade in enumerate(trades[-5:], 1):
                        emoji = "🟢" if trade["pnl_pct"] > 0 else "🔴"
                        lines.append(f"{emoji} #{i} {trade['side'].upper()} | PnL: {trade['pnl_pct']:.2f}% | {trade['result'].upper()}")

        if len(lines) == 1:
            await ctx.send("📭 No trade history yet.")
            return
    else:
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
                lines.append(f"{emoji} #{i} {trade['side'].upper()} | PnL: {trade['pnl_pct']:.2f}% | {trade['result'].upper()}")
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
                            lines.append(f"{emoji} #{i} {trade['side'].upper()} | PnL: {trade['pnl_pct']:.2f}% | {trade['result'].upper()}")

    msg = "\n".join(lines)
    # ✅ ИСПРАВЛЕНО: разбиваем длинные сообщения
    while msg:
        chunk = msg[:1900]
        if len(msg) > 1900:
            chunk = chunk[:chunk.rfind("\n")] if "\n" in chunk else chunk
        await ctx.send(chunk)
        msg = msg[len(chunk):].lstrip("\n")

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
        lines.append(
            f"{emoji} #{i} Entry: ${rec['entry']} → Exit: ${rec['exit']} | "
            f"MFE: {rec['max_favorable_pct']:.2f}% | MAE: {rec['max_adverse_pct']:.2f}% | "
            f"Result: {rec['exit_type'].upper()}"
        )
    await ctx.send("\n".join(lines))

@bot.command(name="tp")
async def tp_cmd(ctx, side: str = "long", ticker: str = "BTC/USDT", tf: str = "1h"):
    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("Side must be `long` or `short`")
        return

    ticker = ticker.upper()
    tf = tf.lower()

    if tf not in TIMEFRAMES:
        await ctx.send(f"❌ Unknown timeframe. Available: {', '.join(TIMEFRAMES)}")
        return

    exchange = getattr(market_scanner, "exchange", None)
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    try:
        bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=100)
        df = parse_ohlcv(bars)
        if not validate_dataframe(df, 50):
            await ctx.send("❌ Not enough data")
            return

        last_close = float(df["close"].iloc[-2])
        atr14 = calculate_atr(df, ATR_PERIOD)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        idx = len(df) - 2
        sl = calculate_sl(last_close, side, fs, fu, fl, atr14, idx)
        tp, tp_desc = calculate_combined_tp(ticker, tf, side, last_close, sl, df, idx, atr14)
        stats = get_signal_stats(ticker, tf, side)
        risk = abs(last_close - sl)
        rr = round(abs(tp - last_close) / max(risk, 1e-8), 2)
        tp_pct = abs(tp - last_close) / last_close * 100

        lines = [f"**📊 Adaptive TP Preview — `{ticker}` `{tf}` {side.upper()}:**"]
        lines.append(f"• Current price: **${round(last_close, 2):,.2f}**")
        lines.append(f"• Stop Loss: **${round(sl, 2):,.2f}** (risk: ${round(risk, 2):,.2f})")
        lines.append(f"• Take Profit: **${round(tp, 2):,.2f}** (+{tp_pct:.2f}%)")
        lines.append(f"• Risk/Reward: **1:{rr}**")
        if stats["count"] >= 5:
            lines.append(f"• Based on **{stats['count']}** historical signals")
            lines.append(f"• Avg MFE: **{stats['avg_mfe']:.2f}%** | Best: **{stats['best']:.2f}%**")
        else:
            lines.append(f"• ⚠️ Only **{stats['count']}** signals in history — using fallback R:R 2.0")
        await ctx.send("\n".join(lines))
    except Exception as e:
        logger.error(f"TP command error: {e}", exc_info=True)
        await ctx.send(f"❌ Error: {e}")

@bot.command(name="debug")
async def debug_cmd(ctx):
    lines = ["**🔍 Debug Information:**"]
    lines.append(f"• Total scans: **{scan_stats['total_scans']}**")
    lines.append(f"• Signals generated: **{scan_stats['signals_generated']}**")
    lines.append(f"• Last scan: {scan_stats['last_scan_time'] or 'never'}")

    history = load_signals_history()
    total_sigs = sum(len(history[t][tf].get(s, [])) for t in history for tf in history[t] for s in ("long", "short"))
    total_closed = sum(1 for t in history for tf in history[t] for s in ("long", "short")
                       for r in history[t][tf].get(s, []) if r["exit_type"] != "open")

    lines.append(f"• Signal history: **{total_sigs}** records ({total_closed} closed)")
    lines.append(f"• History file: **{'Yes' if os.path.exists(SIGNALS_HISTORY_FILE) else 'No'}**")

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

    if os.path.exists(SIGNALS_HISTORY_FILE):
        os.remove(SIGNALS_HISTORY_FILE)

    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            state[ticker][tf]["trade_history"] = []
            state[ticker][tf]["active_trade"] = None
            state[ticker][tf]["bars_in_trade"] = 0

    await ctx.send("🗑️ History cleared. Running fresh backtest…")

    exchange = getattr(market_scanner, "exchange", None)
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    total = 0
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            try:
                count = await asyncio.to_thread(backtest_history, exchange, ticker, tf, 3000)
                total += count
                await ctx.send(f"✅ `{ticker}` `{tf}`: {count} signals found")
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Reset backtest error: {e}")
                await ctx.send(f"❌ `{ticker}` `{tf}` backtest failed: {e}")

    await ctx.send(f"🎓 Fresh backtest complete! Total signals: **{total}**\nRun `!signals` to see statistics.")

@bot.command(name="sim")
async def sim_cmd(ctx, side: str = "long", ticker: str = "BTC/USDT", tf: str = "1h"):
    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("Side must be `long` or `short`")
        return

    ticker = ticker.upper()
    tf = tf.lower()

    if tf not in TIMEFRAMES:
        await ctx.send(f"❌ Unknown timeframe. Available: {', '.join(TIMEFRAMES)}")
        return

    await ctx.send(f"Simulating {side.upper()} signal for `{ticker}` `{tf}`…")

    exchange = getattr(market_scanner, "exchange", None)
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    try:
        bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=100)
        df = parse_ohlcv(bars)
        last_close = float(df["close"].iloc[-2])
        atr14 = calculate_atr(df, ATR_PERIOD)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        idx = len(df) - 2
        sl = calculate_sl(last_close, side, fs, fu, fl, atr14, idx)
        tp, tp_desc = calculate_combined_tp(ticker, tf, side, last_close, sl, df, idx, atr14)

        add_signal_record(ticker, tf, side, last_close, datetime.now(timezone.utc).isoformat())
        update_signal_record(ticker, tf, side, tp, "tp", 5)
        stats = get_signal_stats(ticker, tf, side)

        lines = [f"✅ Simulated {side.upper()} signal recorded!"]
        lines.append(f"• Entry: **${round(last_close, 2):,.2f}**")
        lines.append(f"• SL: **${round(sl, 2):,.2f}**")
        lines.append(f"• TP: **${round(tp, 2):,.2f}**")
        lines.append(f"• History now has **{stats['count']}** closed {side} signals")
        await ctx.send("\n".join(lines))
    except Exception as e:
        logger.error(f"Sim command error: {e}", exc_info=True)
        await ctx.send(f"❌ Simulation failed: {e}")

@bot.command(name="forcerun")
async def forcerun_cmd(ctx, side: str = "long", ticker: str = "BTC/USDT", tf: str = "1h"):
    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("Side must be `long` or `short`")
        return

    ticker = ticker.upper()
    tf = tf.lower()

    if tf not in TIMEFRAMES:
        await ctx.send(f"❌ Unknown timeframe. Available: {', '.join(TIMEFRAMES)}")
        return

    await ctx.send(f"Force-running {side.upper()} signal for `{ticker}` `{tf}` (bypassing filters)…")

    exchange = getattr(market_scanner, "exchange", None)
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    try:
        bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=100)
        df = parse_ohlcv(bars)
        last_close = float(df["close"].iloc[-2])
        atr14 = calculate_atr(df, ATR_PERIOD)
        fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
        idx = len(df) - 2
        sl = calculate_sl(last_close, side, fs, fu, fl, atr14, idx)
        tp, tp_desc = calculate_combined_tp(ticker, tf, side, last_close, sl, df, idx, atr14)
        risk = abs(last_close - sl)

        st = state.get(ticker, {}).get(tf) or make_state()
        st["active_trade"] = {"side": side, "entry": last_close, "sl": sl, "tp": tp, "lev": 3, "bar_opened": idx}
        st["bars_in_trade"] = 0
        state[ticker][tf] = st
        add_signal_record(ticker, tf, side, last_close, datetime.now(timezone.utc).isoformat())
        stats = get_signal_stats(ticker, tf, side)

        rr = round(abs(tp - last_close) / max(risk, 1e-8), 2)
        tp_pct = abs(tp - last_close) / last_close * 100

        embed = discord.Embed(
            title=f"⚡ FORCE SIGNAL {'📈 LONG' if side == 'long' else '📉 SHORT'}",
            color=discord.Color.green() if side == "long" else discord.Color.red(),
        )
        embed.add_field(name="Pair", value=f"**{ticker}**", inline=True)
        embed.add_field(name="TF", value=tf.upper(), inline=True)
        embed.add_field(name="Entry", value=f"${round(last_close, 2):,.2f}", inline=True)
        embed.add_field(name="SL", value=f"${round(sl, 2):,.2f}", inline=True)
        embed.add_field(name="TP", value=f"${round(tp, 2):,.2f} (+{tp_pct:.2f}%)", inline=True)
        embed.add_field(name="R:R", value=f"1:{rr}", inline=True)
        embed.add_field(name="⚠️ WARNING", value="Bypassed all filters — for testing only!", inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Forcerun error: {e}", exc_info=True)
        await ctx.send(f"❌ Force run failed: {e}")
        import traceback
        await ctx.send(f"```\n{traceback.format_exc()[:1000]}\n```")

# =====================================================================
# 🚀  ЗАПУСК
# =====================================================================

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in .env file!")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    bot.run(DISCORD_TOKEN)
