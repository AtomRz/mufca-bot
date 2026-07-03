"""
discord_commands.py — все Discord !команды MUFCA Bot.

🆕 Вынесено из bot.py (было ~1600 строк в одном файле — Discord-команды,
scanner, embed-билдинг, state вперемешку). Это чистый перенос кода,
логика ни одной команды не менялась.

ВАЖНО про доступ к общему состоянию бота:
`_exchange_ref`, `_onchain_bias_cache`, `_onchain_last_fetch` — это модульные
переменные bot.py, которые команды !mode/!onchain/!reset/!reset_cache
ПЕРЕПРИСВАИВАЮТ (не просто мутируют). Обычный `from bot import _exchange_ref`
скопировал бы значение один раз при импорте и не отслеживал бы дальнейшие
переприсвоения в bot.py — market_scanner продолжал бы видеть старое значение.
Поэтому здесь используется квалифицированный доступ `core.<имя>` (и на чтение,
и на запись) вместо `global` + голого имени — присваивание атрибуту модуля
видно из любого места, которое импортирует этот же модуль.

Для `state`, `scan_stats`, `_state_locks`, `market_scanner`, `_tickers_lock`
голый `global` не нужен в принципе — они только мутируются на месте
(`dict[...] = ...`, `list.append(...)`, вызовы методов), а не переприсваиваются
целиком, так что ссылка на объект через `core.<имя>` работает и для чтения,
и для записи одинаково надёжно.
"""

import asyncio
import os
import time
import logging
from datetime import datetime, timezone

import ccxt
import numpy as np
import discord

import config as _cfg
from config import (
    TICKERS,
    TIMEFRAMES,
    MIN_TP_PCT,
    MAX_TP_PCT,
    MAX_HOLD_BARS,
    CHOP_THRESHOLD,
    SIGNALS_HISTORY_FILE,
    ATR_PERIOD,
    FRAMA_LEN,
    FRAMA_MULT,
    save_tickers,
    save_tp_config,
    save_mode,
    ONCHAIN_ENABLED,
)
from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe
from indicators import calculate_atr, calculate_frama
from volume_indicators import volume_flow_signal_v3
from signals import check_signals, backtest_history, make_state, calculate_adaptive_sl, clear_htf_cache
from onchain import get_onchain_bias, format_onchain_report, clear_onchain_cache, clear_onchain_cache_full
from state import (
    load_signals_history,
    calculate_combined_tp,
    add_signal_record,
    update_signal_record,
    update_signal_mae_mfe,
    clear_history_cache,
    get_signal_stats,
)
from embeds import build_embed, _flow_label

import bot as core  # 🆕 доступ к общему рантайму bot.py (см. пояснение выше)

logger = logging.getLogger(__name__)

# =====================================================================
# 💬  DISCORD КОМАНДЫ
# =====================================================================

@core.bot.command(name="help", aliases=["?"])
async def help_cmd(ctx):
    """Список всех команд MUFCA Bot."""
    lines = [
        "**📖 MUFCA v4.0 — Команды**\n",

        "**📊 Мониторинг**",
        "`!status`       — состояние сканера: пары, треки A/U, volume",
        "`!scan <pair> <tf>` — ручной скан (напр. `!scan BTC/USDT 1h`)",
        "`!history <pair> <tf>` — история сделок (напр. `!history BTC/USDT 4h`)",
        "`!signals <pair> <tf>` — статистика сигналов по паре",
        "`!tp <pair> <tf>` — текущий адаптивный TP",
        "`!chart <pair> <tf>` — свечной график с индикаторами (напр. `!chart BTC 1h`)",
        "`!debug`        — расширенная отладочная информация",
        "`!onchain`      — on-chain анализ (F&G, ETH flows)",
        "",

        "**⚙️ Настройки**",
        "`!mode spot|futures` — переключить режим торговли",
        "`!htf <tf>`     — HTF Bias таймфрейм (напр. `!htf 4h`)",
        "`!utha on|off`  — Heikin Ashi для UT Bot",
        "`!chop <tf> <val>` — порог CHOP (напр. `!chop 1h 55`)",
        "",

        "**📚 Адаптивный TP**",
        "`!tpconfig`           — показать текущий конфиг TP",
        "`!tpconfig mode safe` — безопасный режим (50-й %ile)",
        "`!tpconfig mode aggressive` — агрессивный (75-й %ile)",
        "`!tpconfig percentile 70` — изменить агрессивный %ile",
        "`!tpconfig safe 45`   — изменить безопасный %ile",
        "`!tpconfig limit 30`  — кол-во сигналов для обучения",
        "",

        "**📋 Пары**",
        "`!pairs`        — список активных пар",
        "`!add <pair>`   — добавить пару (напр. `!add SOL/USDT`)",
        "`!remove <pair>` — удалить пару",
        "",

        "**🛠️ Утилиты**",
        "`!sim <pair> <tf> <side>` — симуляция сделки",
        "`!forcerun`     — принудительный запуск сканера",
        "`!reset`        — сбросить всё состояние и историю",
        "`!reset_cache`  — сбросить HTF и on-chain кеш",
        "`!help` / `!?` — эта справка",
    ]
    await ctx.send("\n".join(lines))


@core.bot.command(name="status")
async def status_cmd(ctx):
    ha_status = "✅ ON" if _cfg.UT_HEIKIN_ASHI else "❌ OFF"
    lines = [
        f"**MUFCA v4.0 — Scanner Status**\n",
        f"🧬 HTF Bias: **{_cfg.HTF_BIAS.upper()}**\n",
        f"🕯️ UT Bot Heikin Ashi: **{ha_status}**\n",
        f"📚 Adaptive TP: last **{_cfg.SIGNAL_HISTORY_LIMIT}** signals | **{(_cfg.SAFE_TP_PERCENTILE if _cfg.USE_SAFE_TP else _cfg.TP_PERCENTILE)*100:.0f}th** percentile ({'SAFE 🛡️' if _cfg.USE_SAFE_TP else 'AGGRESSIVE ⚡'})\n",
    ]
    exchange = core._exchange_ref
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st = core.state[ticker][tf]
            last = st["last_bar_time"]
            # 🆕 FIX: Handle both int and string timestamps
            ts = "no data"
            if last is not None:
                try:
                    ts_val = int(last)
                    ts = f"<t:{ts_val // 1000}:R>"
                except (ValueError, TypeError):
                    ts = str(last)
            a_trade = st.get("a_active_trade")
            u_trade = st.get("u_active_trade")

            # Показываем позицию только если флаг И active_trade оба установлены
            # Если только флаг без trade — рассинхрон, показываем предупреждение
            a_flag = "LONG" if st["a_in_long"] else "SHORT" if st["a_in_short"] else None
            u_flag = "LONG" if st["u_in_long"] else "SHORT" if st["u_in_short"] else None

            # 🆕 FIX BUG-HI001: Убрано двойное присваивание a_pos
            # Ранее: a_pos = f"⚠️{a_flag}" → затем a_pos = "—" (перезаписывало!)
            # Теперь: одно присваивание с информативным сообщением
            async with core._state_locks[ticker][tf]:
                if a_flag and not a_trade:
                    logger.warning(f"[STATE] A-track desync fixed for {ticker} {tf}")
                    core.state[ticker][tf]["a_in_long"] = False   # auto-heal
                    core.state[ticker][tf]["a_in_short"] = False
                    a_pos = f"⚠️{a_flag} (fixed)"  # ← Одно присваивание, пользователь видит предупреждение
                else:
                    a_pos = a_flag or "—"

                # 🆕 FIX BUG-HI001: Аналогично для U-трека
                if u_flag and not u_trade:
                    logger.warning(f"[STATE] U-track desync fixed for {ticker} {tf}")
                    core.state[ticker][tf]["u_in_long"] = False   # auto-heal
                    core.state[ticker][tf]["u_in_short"] = False
                    u_pos = f"⚠️{u_flag} (fixed)"  # ← Аналогично для U-трека
                else:
                    u_pos = u_flag or "—"
            trade_info = ""
            if a_trade:
                trade_info += f" | 🎯[A] {a_trade['side'].upper()} @ ${round(a_trade['entry'], 2)}"
            if u_trade:
                trade_info += f" | 🎯[U] {u_trade['side'].upper()} @ ${round(u_trade['entry'], 2)}"

            # 🆕 Volume info
            vol_info = ""
            if exchange:
                try:
                    bars = await safe_fetch_ohlcv(exchange, ticker, tf, limit=50)
                    if bars and len(bars) >= 25:
                        df_v = parse_ohlcv(bars)
                        vol_data = volume_flow_signal_v3(df_v)
                        vol_flow = vol_data["flow"]
                        vol_emoji = "🟢" if vol_flow == "inflow" else "🔴" if vol_flow == "outflow" else "⚪"
                        vol_info = f" | {vol_emoji} {_flow_label(vol_flow)} RV:{vol_data['rel_vol']:.1f}x"
                except Exception:
                    pass

            lines.append(f"• `{ticker}` `{tf}` — bar: {ts} | A: **{a_pos}** | U: **{u_pos}**{trade_info}{vol_info}")

    msg = "\n".join(lines)
    if len(msg) > 1900:
        await ctx.send(msg[:1900] + "\n... (truncated)")
    else:
        await ctx.send(msg)

@core.bot.command(name="scan")
async def scan_cmd(ctx, ticker: str = "BTC/USDT", tf: str = "1h"):
    ticker = ticker.upper()
    tf = tf.lower()

    if tf not in TIMEFRAMES:
        await ctx.send(f"❌ Unknown timeframe. Available: {', '.join(TIMEFRAMES)}")
        return

    await ctx.send(f"🔍 Scanning `{ticker}` `{tf}`…")

    exchange = core._exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    # 🆕 FIX: Use lock during scan command
    if ticker not in core._state_locks:
        core._state_locks[ticker] = {}
    if tf not in core._state_locks.get(ticker, {}):
        core._state_locks[ticker][tf] = asyncio.Lock()

    async with core._state_locks[ticker][tf]:
        # ✅ ИСПРАВЛЕНО: используем временный state, чтобы не мутировать основной
        temp_state = make_state()
        try:
            signals, bar_time, regime, lev = await check_signals(exchange, ticker, tf, temp_state, dry_run=True)
        except Exception as e:
            logger.error(f"Manual scan error: {e}", exc_info=True)
            await ctx.send(f"❌ Scan error: {e}")
            return

    # 🆕 Fetch df for volume info
    bars = await safe_fetch_ohlcv(exchange, ticker, tf, limit=100)
    df = parse_ohlcv(bars) if bars else None

    if signals:
        for sig_type, price, reg, leverage, bt, conf, sl, tp, tp1, risk, stats, tp_desc in signals:
            embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf, sl, tp, tp1, risk, stats, tp_desc, df)
            await ctx.send(embed=embed)
    else:
        # 🆕 Show volume info even when no signal
        vol_info = ""
        try:
            if df is not None and len(df) >= 35:
                vol_data = volume_flow_signal_v3(df)
                vol_flow = vol_data["flow"]
                vol_emoji = "🟢" if vol_flow == "inflow" else "🔴" if vol_flow == "outflow" else "⚪"
                vol_info = f" | {vol_emoji} Vol:{_flow_label(vol_flow)} RV:{vol_data['rel_vol']:.1f}x"
        except Exception as e:
            logger.debug(f"Volume info error in build_embed: {e}")
        await ctx.send(f"⏳ No signals for `{ticker}` `{tf}`. Regime: **{regime}**{vol_info}")

@core.bot.command(name="pairs")
async def pairs_cmd(ctx):
    if not TICKERS:
        await ctx.send("📭 Pair list is empty.")
        return
    lines = ["**📋 Scanned Pairs:**\n"]
    for t in TICKERS:
        lines.append(f"• `{t}`")
    await ctx.send("\n".join(lines))

@core.bot.command(name="add")
async def add_cmd(ctx, ticker: str = ""):
    if not ticker:
        await ctx.send("❌ Please specify a pair. Example: `!add SOL/USDT`")
        return
    ticker = ticker.upper()
    if ticker in TICKERS:
        await ctx.send(f"⚠️ `{ticker}` is already in the list.")
        return

    exchange = core._exchange_ref
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

    async with core._tickers_lock:
        TICKERS.append(ticker)
        core.state[ticker] = {tf: make_state() for tf in TIMEFRAMES}

    # 🆕 FIX: Ensure locks for new ticker
    if ticker not in core._state_locks:
        core._state_locks[ticker] = {}
    for tf in TIMEFRAMES:
        if tf not in core._state_locks[ticker]:
            core._state_locks[ticker][tf] = asyncio.Lock()

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

@core.bot.command(name="remove")
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

    async with core._tickers_lock:
        TICKERS.remove(ticker)
        if ticker in core.state:
            del core.state[ticker]
        if ticker in core._state_locks:
            del core._state_locks[ticker]

    save_tickers(TICKERS)

    await ctx.send(f"🗑️ `{ticker}` removed. Remaining: {' | '.join(TICKERS)}")

@core.bot.command(name="mode")
async def mode_cmd(ctx, new_mode: str = ""):
    # 🆕 FIX BUG-LO001: Не используем global MARKET_MODE напрямую
    # Ранее: global MARKET_MODE — не работает корректно с from config import MARKET_MODE
    # Теперь: модифицируем _cfg.MARKET_MODE, к которому обращаются все модули
    if not new_mode:
        label = "🔵 Spot" if _cfg.MARKET_MODE == "spot" else "🟠 Futures"
        await ctx.send(f"Current mode: **{label}**\nTo switch: `!mode spot` or `!mode futures`")
        return

    new_mode = new_mode.lower()
    if new_mode not in ("spot", "futures"):
        await ctx.send("❌ Valid modes: `spot` or `futures`")
        return

    if new_mode == _cfg.MARKET_MODE:
        await ctx.send(f"⚠️ Already in **{_cfg.MARKET_MODE}** mode.")
        return

    _cfg.MARKET_MODE = new_mode  # ← Меняем через модуль, не через global

    save_mode(_cfg.MARKET_MODE)

    if _cfg.MARKET_MODE == "futures":
        exchange = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    else:
        exchange = ccxt.gate({"enableRateLimit": True})
    core._exchange_ref = exchange

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
            async with core._state_locks[ticker][tf]:
                core.state[ticker][tf] = st

    label = "🔵 Spot (Gate.io Spot)" if _cfg.MARKET_MODE == "spot" else "🟠 Futures (Gate.io Perpetual)"
    await ctx.send(f"✅ Switched to **{label}**\n⚠️ Position states have been reset.")

@core.bot.command(name="utha")
async def utha_cmd(ctx, arg: str = ""):
    if not arg:
        status = "✅ ON" if _cfg.UT_HEIKIN_ASHI else "❌ OFF"
        await ctx.send(f"🕯️ Heikin Ashi for UT Bot: **{status}**\nTo change: `!utha on` or `!utha off`")
        return

    arg = arg.lower()
    if arg not in ("on", "off"):
        await ctx.send("❌ Valid values: `on` or `off`")
        return

    new_value = arg == "on"
    if new_value == _cfg.UT_HEIKIN_ASHI:
        status = "✅ already ON" if _cfg.UT_HEIKIN_ASHI else "❌ already OFF"
        await ctx.send(f"⚠️ Heikin Ashi for UT Bot is {status}.")
        return

    _cfg.UT_HEIKIN_ASHI = new_value
    _cfg.save_ut_ha(_cfg.UT_HEIKIN_ASHI)
    status = "✅ ENABLED" if _cfg.UT_HEIKIN_ASHI else "❌ DISABLED"
    await ctx.send(f"🕯️ Heikin Ashi for UT Bot **{status}**.")

@core.bot.command(name="htf")
async def htf_cmd(ctx, new_htf: str = ""):
    if not new_htf:
        await ctx.send(f"🧬 Current HTF Bias: **{_cfg.HTF_BIAS.upper()}**\n"
                       f"Available: `1d`, `4h`, `1h`, `1w`\n"
                       f"To change: `!htf 4h`")
        return

    new_htf = new_htf.lower()
    valid_htfs = ("1d", "4h", "2h", "6h", "12h", "1w", "3d")
    if new_htf not in valid_htfs:
        await ctx.send(f"❌ Valid HTF values: {', '.join(valid_htfs)}")
        return

    if new_htf == _cfg.HTF_BIAS:
        await ctx.send(f"⚠️ HTF Bias is already **{_cfg.HTF_BIAS.upper()}**.")
        return

    old_htf = _cfg.HTF_BIAS
    _cfg.HTF_BIAS = new_htf
    _cfg.save_htf(_cfg.HTF_BIAS)
    clear_htf_cache()

    exchange = core._exchange_ref
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
                async with core._state_locks[ticker][tf]:
                    core.state[ticker][tf] = st

    await ctx.send(f"🧬 HTF Bias changed: **{old_htf.upper()}** → **{_cfg.HTF_BIAS.upper()}**\n"
                   f"⚠️ Position states have been reset.")

@core.bot.command(name="tpconfig")
async def tpconfig_cmd(ctx, param: str = "", value: str = ""):
    try:
        active_pct = _cfg.SAFE_TP_PERCENTILE if _cfg.USE_SAFE_TP else _cfg.TP_PERCENTILE
        active_mode = "SAFE 🛡️" if _cfg.USE_SAFE_TP else "AGGRESSIVE ⚡"

        if not param:
            await ctx.send(
                "**📚 Adaptive TP Configuration:**\n"
                f"• Active mode: **{active_mode}** | Percentile: **{active_pct*100:.0f}th**\n"
                f"• Aggressive percentile: **{_cfg.TP_PERCENTILE*100:.0f}th**\n"
                f"• Safe percentile: **{_cfg.SAFE_TP_PERCENTILE*100:.0f}th**\n"
                f"• History limit: **{_cfg.SIGNAL_HISTORY_LIMIT}** signals\n"
                f"• Min TP: **{MIN_TP_PCT}%** | Max TP: **{MAX_TP_PCT}%**\n"
                f"• Max hold: **{MAX_HOLD_BARS}** bars\n"
                "\nTo change: `!tpconfig mode safe` | `!tpconfig mode aggressive` | "
                "`!tpconfig limit 30` | `!tpconfig percentile 70` | `!tpconfig safe 50`"
            )
            return

        param = param.lower()
        if param == "mode":
            if value.lower() == "safe":
                _cfg.USE_SAFE_TP = True
                save_tp_config()
                await ctx.send(f"🛡️ **Safe mode enabled** — TP now uses **{_cfg.SAFE_TP_PERCENTILE*100:.0f}th percentile**.")
            elif value.lower() in ("aggressive", "aggr"):
                _cfg.USE_SAFE_TP = False
                save_tp_config()
                await ctx.send(f"⚡ **Aggressive mode enabled** — TP now uses **{_cfg.TP_PERCENTILE*100:.0f}th percentile**.")
            else:
                await ctx.send("❌ Mode must be `safe` or `aggressive`")
        elif param == "limit":
            try:
                new_limit = int(value)
                if not (5 <= new_limit <= 200):
                    await ctx.send("❌ Limit must be between 5 and 200")
                    return
                old = _cfg.SIGNAL_HISTORY_LIMIT
                _cfg.SIGNAL_HISTORY_LIMIT = new_limit
                save_tp_config()
                await ctx.send(f"✅ History limit changed: **{old}** → **{new_limit}** signals")
            except ValueError:
                await ctx.send("❌ Invalid number")
        elif param == "percentile":
            try:
                new_pct = float(value)
                if not (10 <= new_pct <= 99):
                    await ctx.send("❌ Percentile must be between 10 and 99")
                    return
                old = _cfg.TP_PERCENTILE
                _cfg.TP_PERCENTILE = new_pct / 100
                save_tp_config()
                await ctx.send(f"✅ Aggressive percentile changed: **{old*100:.0f}th** → **{new_pct:.0f}th**")
            except ValueError:
                await ctx.send("❌ Invalid number")
        elif param == "safe":
            try:
                new_pct = float(value)
                if not (10 <= new_pct <= 99):
                    await ctx.send("❌ Safe percentile must be between 10 and 99")
                    return
                old = _cfg.SAFE_TP_PERCENTILE
                _cfg.SAFE_TP_PERCENTILE = new_pct / 100
                save_tp_config()
                await ctx.send(f"✅ Safe percentile changed: **{old*100:.0f}th** → **{new_pct:.0f}th**")
            except ValueError:
                await ctx.send("❌ Invalid number")
        else:
            await ctx.send("❌ Unknown parameter. Use `mode`, `limit`, `percentile`, or `safe`")
    except Exception as e:
        logger.error(f"TPConfig command error: {e}", exc_info=True)
        await ctx.send(f"❌ Error: {e}")

@core.bot.command(name="chop")
async def chop_cmd(ctx, tf: str = "", value: str = ""):
    try:
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
            _cfg.save_chop(CHOP_THRESHOLD)
            await ctx.send(f"✅ CHOP threshold for `{tf}` changed: **{old}** → **{new_val}**")
        except ValueError:
            await ctx.send("❌ Invalid number")
    except Exception as e:
        logger.error(f"Chop command error: {e}", exc_info=True)
        await ctx.send(f"❌ Error: {e}")

@core.bot.command(name="history")
async def history_cmd(ctx, ticker: str = "", tf: str = ""):
    try:
        lines = []

        if not ticker:
            lines = ["**📊 Trade History:**\n"]
            for t in TICKERS:
                for timeframe in TIMEFRAMES:
                    st = core.state[t][timeframe]
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
                st = core.state.get(ticker, {}).get(tf)
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
                    st = core.state.get(ticker, {}).get(timeframe)
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
    except Exception as e:
        logger.error(f"History command error: {e}", exc_info=True)
        await ctx.send(f"❌ Error: {e}")

@core.bot.command(name="signals")
async def signals_cmd(ctx, ticker: str = "", tf: str = "", side: str = ""):
    try:
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
    except Exception as e:
        logger.error(f"Signals command error: {e}", exc_info=True)
        await ctx.send(f"❌ Error: {e}")

@core.bot.command(name="tp")
async def tp_cmd(ctx, ticker: str = "BTC/USDT", tf: str = "1h", side: str = "long"):
    side = side.lower()
    if side not in ("long", "short"):
        await ctx.send("Side must be `long` or `short`")
        return

    ticker = ticker.upper()
    tf = tf.lower()

    if tf not in TIMEFRAMES:
        await ctx.send(f"❌ Unknown timeframe. Available: {', '.join(TIMEFRAMES)}")
        return

    exchange = core._exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    # 🆕 FIX: Use lock during sim
    if ticker not in core._state_locks:
        core._state_locks[ticker] = {}
    if tf not in core._state_locks.get(ticker, {}):
        core._state_locks[ticker][tf] = asyncio.Lock()

    async with core._state_locks[ticker][tf]:
        try:
            bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=100)
            df = parse_ohlcv(bars)
            if not validate_dataframe(df, 50):
                await ctx.send("❌ Not enough data")
                return

            # Берём реальную текущую цену через fetch_ticker (не iloc[-2]),
            # иначе на 1h бот показывает цену до 59 минут устаревшей.
            # Индикаторы (ATR, FRAMA) считаем по закрытым барам (iloc[-2]) как обычно.
            try:
                ticker_data = await asyncio.to_thread(exchange.fetch_ticker, ticker)
                last_close = float(
                    ticker_data.get("close") or      # ← приоритет close (консистентно с ботом)
                    ticker_data.get("last") or
                    ticker_data.get("bid") or
                    df["close"].iloc[-2]             # fallback на закрытый бар
                )
            except Exception:
                last_close = float(df["close"].iloc[-2])  # fallback на закрытый бар

            atr14 = calculate_atr(df, ATR_PERIOD)
            fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
            idx = len(df) - 2
            sl, sl_desc = calculate_adaptive_sl(last_close, side, ticker, tf, fs, fu, fl, atr14, idx)
            tp1, tp2, tp_desc = calculate_combined_tp(ticker, tf, side, last_close, sl, df, idx, atr14)
            stats = get_signal_stats(ticker, tf, side)
            risk = abs(last_close - sl)
            rr = round(abs(tp2 - last_close) / max(risk, 1e-8), 2)
            tp1_pct = abs(tp1 - last_close) / last_close * 100
            tp2_pct = abs(tp2 - last_close) / last_close * 100

            lines = [f"**📊 Adaptive TP Preview — `{ticker}` `{tf}` {side.upper()}:**"]
            lines.append(f"• Current price: **${round(last_close, 2):,.2f}**")
            lines.append(f"• Stop Loss: **${round(sl, 2):,.2f}** ({sl_desc})")
            lines.append(f"• 🎯 TP1 (50%): **${round(tp1, 2):,.2f}** (+{tp1_pct:.2f}%)")
            lines.append(f"• 🏁 TP2 (100%): **${round(tp2, 2):,.2f}** (+{tp2_pct:.2f}%)")
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


@core.bot.command(name="chart")
async def chart_cmd(ctx, pair: str = "BTC", tf: str = "1h", limit: int = 50):
    """
    !chart [PAIR] [TIMEFRAME] [LIMIT]
    Примеры:
      !chart          → BTC/USDT 1h 50 свечей
      !chart ETH      → ETH/USDT 1h 50 свечей
      !chart BTC 4h   → BTC/USDT 4h 50 свечей
      !chart BTC 1h 100
    """
    pair = pair.upper()
    if "/" not in pair:
        pair = pair + "/USDT"
    tf = tf.lower()

    if tf not in TIMEFRAMES:
        await ctx.send(f"❌ Unknown timeframe. Available: {', '.join(TIMEFRAMES)}")
        return

    limit = max(20, min(200, limit))

    exchange = core._exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    msg = await ctx.send(f"📊 Generating chart `{pair}` `{tf}` ({limit} bars)...")

    try:
        from chart import generate_chart

        # Snapshot активной сделки если есть
        state_snapshot = None
        pair_state = core.state.get(pair, {})
        for _tf_key, st in pair_state.items():
            if _tf_key == tf:
                a_trade = st.get("a_active_trade")
                u_trade = st.get("u_active_trade") or st.get("active_trade")
                active = a_trade or u_trade
                if active:
                    # 🆕 FIX: раньше передавался позиционный "bar_opened" индекс,
                    # посчитанный на df сигнала — но !chart строит свой df отдельным
                    # fetch'ем (возможно днями позже), и индексы не совпадали, стрелка
                    # уезжала в произвольное место. Теперь передаём реальный timestamp
                    # бара входа, и chart.py сам находит нужный бар в своём df.
                    state_snapshot = {
                        "entry": active.get("entry"),
                        "tp":    active.get("tp"),
                        "sl":    active.get("sl"),
                        "side":  active.get("side"),
                        "entry_time_ms": active.get("bar_opened_time"),
                    }
                break

        buf = await generate_chart(
            exchange=exchange,
            symbol=pair,
            timeframe=tf,
            limit=limit,
            state_snapshot=state_snapshot,
        )

        await msg.delete()
        has_trade = state_snapshot is not None
        trade_note = f" | 🎯 Active {state_snapshot['side'].upper()} @ ${state_snapshot['entry']:,.2f}" if has_trade else ""
        await ctx.send(
            content=f"📊 **{pair}** `{tf}` · {limit} bars{trade_note}",
            file=discord.File(buf, filename=f"{pair.replace('/', '')}_{tf}.png")
        )

    except Exception as e:
        logger.error(f"Chart command error: {e}", exc_info=True)
        await msg.edit(content=f"❌ Chart error: `{e}`")

@core.bot.command(name="debug")
async def debug_cmd(ctx):
    lines = ["**🔍 Debug Information:**"]
    lines.append(f"• Total scans: **{core.scan_stats['total_scans']}**")
    lines.append(f"• Signals generated: **{core.scan_stats['signals_generated']}**")
    lines.append(f"• Last scan: {core.scan_stats['last_scan_time'] or 'never'}")

    history = load_signals_history()
    total_sigs = sum(len(history[t][tf].get(s, [])) for t in history for tf in history[t] for s in ("long", "short"))
    total_closed = sum(1 for t in history for tf in history[t] for s in ("long", "short")
                       for r in history[t][tf].get(s, []) if r["exit_type"] != "open")

    lines.append(f"• Signal history: **{total_sigs}** records ({total_closed} closed)")
    lines.append(f"• History file: **{'Yes' if os.path.exists(SIGNALS_HISTORY_FILE) else 'No'}**")

    active_count = sum(1 for t in TICKERS for tf in TIMEFRAMES if core.state[t][tf].get("active_trade"))
    lines.append(f"• Active trades: **{active_count}**")

    # Volume overview
    exchange = core._exchange_ref
    lines.append("\n**📊 Volume Overview:**")
    if exchange is None:
        lines.append("• Exchange not initialized")
    else:
        for ticker in list(TICKERS):
            for tf in TIMEFRAMES:
                try:
                    bars = await safe_fetch_ohlcv(exchange, ticker, tf, limit=50)
                    if bars and len(bars) >= 25:
                        df_d = parse_ohlcv(bars)
                        vol_data = volume_flow_signal_v3(df_d)
                        vol_flow = vol_data["flow"]
                        vol_emoji = "🟢" if vol_flow == "inflow" else "🔴" if vol_flow == "outflow" else "⚪"
                        lines.append(f"• `{ticker}` `{tf}`: {vol_emoji} {_flow_label(vol_flow)} RV:{vol_data['rel_vol']:.1f}x")
                except Exception:
                    pass

    await ctx.send("\n".join(lines))

@core.bot.command(name="reset")
async def reset_cmd(ctx, confirm: str = ""):
    if confirm.lower() != "yes":
        await ctx.send(
            "⚠️ This will DELETE all signal history and trade data!\n"
            "To confirm, type: `!reset yes`"
        )
        return

    # BUGFIX BUG-CR003: останавливаем сканер перед backtest чтобы исключить
    # race condition — одновременная запись в signals_history.json из двух источников.
    scanner_was_running = core.market_scanner.is_running()
    if scanner_was_running:
        core.market_scanner.stop()
        await asyncio.sleep(1)  # даём текущей итерации завершиться

    if os.path.exists(SIGNALS_HISTORY_FILE):
        os.remove(SIGNALS_HISTORY_FILE)
    clear_history_cache()

    # BUGFIX BUG-HI002: ранее сбрасывались только trade_history/active_trade/bars_in_trade.
    # Не сбрасывались: a_active_trade, u_active_trade, a_in_long/a_in_short, u_in_long/u_in_short,
    # a_bars_in_trade, u_bars_in_trade, last_*_bar, *_last_closure_notified.
    # Треки оставались замороженными после !reset. Теперь полный сброс через make_state().
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            core.state[ticker][tf] = make_state()

    # 🆕 FIX: Clear on-chain cache on reset
    core._onchain_bias_cache = None
    core._onchain_last_fetch = 0.0

    await ctx.send("🗑️ History cleared. Running fresh backtest…")

    exchange = core._exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        if scanner_was_running:
            core.market_scanner.start()
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

    # Перезапускаем сканер после backtest
    if scanner_was_running and not core.market_scanner.is_running():
        core.market_scanner.start()

    await ctx.send(f"🎓 Fresh backtest complete! Total signals: **{total}**\nRun `!signals` to see statistics.")

@core.bot.command(name="sim")
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

    exchange = core._exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    # 🆕 FIX: Use lock during sim
    if ticker not in core._state_locks:
        core._state_locks[ticker] = {}
    if tf not in core._state_locks.get(ticker, {}):
        core._state_locks[ticker][tf] = asyncio.Lock()

    async with core._state_locks[ticker][tf]:
        try:
            bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=100)
            df = parse_ohlcv(bars)

            last_close = float(df["close"].iloc[-2])

            atr14 = calculate_atr(df, ATR_PERIOD)

            fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)

            idx = len(df) - 2

            sl, sl_desc = calculate_adaptive_sl(last_close, side, ticker, tf, fs, fu, fl, atr14, idx)

            tp1, tp2, tp_desc = calculate_combined_tp(ticker, tf, side, last_close, sl, df, idx, atr14)
            tp = tp2


            # 🆕 FIX BUG-HI005: Рассчитываем реалистичный MFE/MAE перед закрытием
            # Ранее: update_signal_mae_mfe НЕ вызывался, max_favorable_pct и max_adverse_pct
            # оставались 0.0, искажая перцентиль TP.
            # Теперь: вызываем update_signal_mae_mfe для SL и TP, чтобы записать реалистичные
            # значения MAE/MFE в историю.
            #
            # 🆕 FIX: помечаем track="sim" + synthetic=True — эта запись идеализированная
            # (полный ход до TP И до SL одновременно) и раньше писалась под тем же track,
            # что и реальные A/U сделки, искажая calculate_adaptive_sl/tp (в т.ч. раздувая
            # адаптивный SL, т.к. max_adverse_pct тут равен полной дистанции до SL).
            # Теперь такие записи исключены из статистики/калибровки (см. state.py).
            add_signal_record(
                ticker, tf, side, last_close, datetime.now(timezone.utc).isoformat(),
                track="sim", synthetic=True,
            )

            # Рассчитываем MFE (максимально благоприятное движение = TP)
            # и MAE (максимально неблагоприятное = SL) для корректной статистики
            update_signal_mae_mfe(ticker, tf, side, tp, track="sim")   # MFE = (TP - entry)/entry
            update_signal_mae_mfe(ticker, tf, side, sl, track="sim")   # MAE = (entry - SL)/entry

            update_signal_record(ticker, tf, side, tp, "tp", 5, track="sim")

            stats = get_signal_stats(ticker, tf, side)


            lines = [f"✅ Simulated {side.upper()} signal recorded!"]

            lines.append(f"• Entry: **${round(last_close, 2):,.2f}**")

            lines.append(f"• SL: **${round(sl, 2):,.2f}**")

            lines.append(f"• TP: **${round(tp, 2):,.2f}**")

            # 🆕 NOTE: stats['count'] теперь считается только по реальным (не-sim)
            # закрытым сигналам — синтетическая sim-запись в калибровку не входит.
            lines.append(f"• Real closed {side} signals in history: **{stats['count']}** (sim-запись помечена отдельно и не влияет на adaptive TP/SL)")

            await ctx.send("\n".join(lines))

        except Exception as e:
            logger.error(f"Sim command error: {e}", exc_info=True)

            await ctx.send(f"❌ Simulation failed: {e}")


@core.bot.command(name="forcerun")
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

    exchange = core._exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    if ticker not in core._state_locks:
        core._state_locks[ticker] = {}
    if tf not in core._state_locks.get(ticker, {}):
        core._state_locks[ticker][tf] = asyncio.Lock()

    async with core._state_locks[ticker][tf]:
        try:
            bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=100)
            df = parse_ohlcv(bars)

            # BUGFIX BUG-ME001: отсутствовала проверка — IndexError при пустом ответе биржи
            if not validate_dataframe(df, 50):
                await ctx.send("❌ Not enough data from exchange")
                return

            last_close = float(df["close"].iloc[-2])

            atr14 = calculate_atr(df, ATR_PERIOD)
            fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
            idx = len(df) - 2

            sl, sl_desc = calculate_adaptive_sl(last_close, side, ticker, tf, fs, fu, fl, atr14, idx)
            tp1, tp2, tp_desc = calculate_combined_tp(ticker, tf, side, last_close, sl, df, idx, atr14)
            tp = tp2
            risk = abs(last_close - sl)

            # BUGFIX BUG-HI003: ранее делали core.state[ticker][tf] = make_state() через st,
            # что безвозвратно уничтожало активные позиции и всю историю сделок.
            # Теперь обновляем только нужные поля существующего state.
            if ticker not in core.state or tf not in core.state.get(ticker, {}):
                state.setdefault(ticker, {})[tf] = make_state()

            st = core.state[ticker][tf]
            track_key = "a_active_trade"  # forcerun всегда на A-треке

            # 🆕 FIX: раньше forcerun безусловно перезаписывал a_active_trade —
            # если A-трек уже держал реальную позицию, она терялась бесследно
            # (без закрытия и без записи в историю), а её "open"-запись в
            # signals_history зависала навсегда. Теперь просто отказываем.
            existing = st.get(track_key)
            if existing:
                await ctx.send(
                    f"⚠️ У A-трека уже есть открытая позиция `{existing['side'].upper()}` "
                    f"по `{ticker}` `{tf}` (entry ${existing['entry']:,.2f}). "
                    f"Сначала дождитесь её закрытия или закройте вручную."
                )
                return

            st[track_key] = {
                "side": side, "entry": last_close, "sl": sl, "tp": tp, "lev": 3,
                "bar_opened": idx, "bar_opened_time": int(df["timestamp"].iloc[idx]),
            }
            st["a_bars_in_trade"] = 0
            st["a_in_long"] = (side == "long")
            st["a_in_short"] = (side == "short")
            # Обновляем legacy поле для совместимости
            st["active_trade"] = st[track_key]
            st["bars_in_trade"] = 0

            # 🆕 FIX: явно помечаем track="a", т.к. forcerun открывает именно a_active_trade
            add_signal_record(ticker, tf, side, last_close, datetime.now(timezone.utc).isoformat(), track="a")
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


@core.bot.command(name="onchain")
async def onchain_cmd(ctx):
    """Показывает текущий on-chain анализ (F&G, ETH flows, влияние на сигналы)."""
    if not ONCHAIN_ENABLED:
        await ctx.send(
            "⚠️ On-Chain анализ отключён.\n"
            "Добавьте в переменные окружения:\n"
            "```\nETHERSCAN_API_KEY=ваш_ключ\nCOINGECKO_API_KEY=ваш_ключ\n```"
        )
        return

    msg = await ctx.send("⏳ Получаю on-chain данные...")
    try:
        bias = await get_onchain_bias()

        # BUGFIX BUG-HI004: НЕ вызываем clear_onchain_cache() при first_run —
        # это сбрасывало _prev_balances и следующий вызов снова давал first_run (infinite loop).
        # При первом запуске baseline уже сохранён в _prev_balances, следующий hourly
        # цикл в core.market_scanner сам посчитает реальную дельту. Просто сообщаем об этом.
        if bias.get("flow_data", {}).get("note") == "first_run":
            report = format_onchain_report(bias)
            await msg.edit(content=report + "\n\n⏳ _ETH flow дельта будет доступна через ~1 час (первый запуск)._")
            return

        core._onchain_bias_cache = bias
        core._onchain_last_fetch = time.time()
        report = format_onchain_report(bias)
        await msg.edit(content=report)
    except Exception as e:
        await msg.edit(content=f"❌ Ошибка on-chain: `{e}`")


@core.bot.command(name="reset_cache")
async def reset_cache_cmd(ctx):
    """Сбрасывает HTF bias cache и on-chain cache вручную."""
    clear_htf_cache()
    clear_onchain_cache_full()  # полный сброс включая baseline балансов
    core._onchain_bias_cache = None
    core._onchain_last_fetch = 0.0
    await ctx.send("✅ HTF bias cache и On-Chain cache сброшены. Следующий скан обновит данные.")
