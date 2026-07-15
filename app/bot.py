import asyncio
import json
import os
import time
import discord
from discord.ext import tasks, commands
from datetime import datetime, timezone
import logging
from typing import Optional, Dict

import ccxt

import config as _cfg
from config import (
    DISCORD_TOKEN,
    CHANNEL_NAME,
    TICKERS,
    TIMEFRAMES,
    DATA_DIR,
    MARKET_MODE,
    ONCHAIN_ENABLED,
)
from utils import safe_fetch_ohlcv, parse_ohlcv
from signals import check_signals, backtest_history, make_state
from onchain import get_onchain_bias
from state import load_signals_history

logger = logging.getLogger(__name__)

# 🆕 NOTE: остальные импорты (validate_dataframe, calculate_atr/frama,
# volume_*, calculate_sl/calculate_adaptive_sl, format_onchain_report,
# clear_*_cache, calculate_combined_tp, add/update_signal_*, get_signal_stats,
# save_tickers/save_tp_config/save_mode, MIN_TP_PCT/MAX_TP_PCT/MAX_HOLD_BARS/
# CHOP_THRESHOLD/PAIRS_FILE/ATR_PERIOD/FRAMA_LEN/FRAMA_MULT/
# SIGNALS_HISTORY_FILE) использовались только внутри @bot.command-хендлеров и
# теперь живут в discord_commands.py вместе с самими командами.

from embeds import build_embed  # 🆕 вынесено в embeds.py (build_embed используется в market_scanner)

# =====================================================================
# 🤖  DISCORD BOT
# =====================================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Глобальное состояние
state = {ticker: {tf: make_state() for tf in TIMEFRAMES} for ticker in TICKERS}

def _heal_state():
    """Исправляет рассинхрон флагов треков при старте."""
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st = state[ticker][tf]
            # Если флаг стоит но active_trade нет — сбрасываем флаг
            if (st.get("a_in_long") or st.get("a_in_short")) and not st.get("a_active_trade"):
                st["a_in_long"] = False
                st["a_in_short"] = False
                logger.warning(f"[HEAL] A-track desync fixed at startup: {ticker} {tf}")
            if (st.get("u_in_long") or st.get("u_in_short")) and not st.get("u_active_trade"):
                st["u_in_long"] = False
                st["u_in_short"] = False
                logger.warning(f"[HEAL] U-track desync fixed at startup: {ticker} {tf}")

_heal_state()
scan_stats = {"total_scans": 0, "signals_generated": 0, "last_scan_time": None}

# 🆕 FIX: Per-ticker/tf asyncio locks to prevent race conditions
_state_locks: Dict[str, Dict[str, asyncio.Lock]] = {}
_tickers_lock = asyncio.Lock()

def _ensure_locks():
    """Ensure locks exist for all tickers/timeframes."""
    for ticker in TICKERS:
        if ticker not in _state_locks:
            _state_locks[ticker] = {}
        for tf in TIMEFRAMES:
            if tf not in _state_locks[ticker]:
                _state_locks[ticker][tf] = asyncio.Lock()

_ensure_locks()

# 🆕 FIX: Module-level exchange reference (more robust than task attribute)
_exchange_ref: Optional[ccxt.Exchange] = None

# 🆕 FIX: Persisted closure notifications
_closure_notified_file = os.path.join(DATA_DIR, "closure_notified.json")
_CLOSURE_NOTIFIED_MAX = 2000  # 🆕 FIX: раньше набор рос бесконечно (по 1 ID на закрытую сделку навсегда)

def _load_closure_notified() -> set:
    """Load set of already-notified trade IDs."""
    try:
        with open(_closure_notified_file, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_closure_notified(notified: set):
    """Save notified trade IDs. Обрезаем до последних _CLOSURE_NOTIFIED_MAX, чтобы файл не рос бесконечно."""
    try:
        ids = list(notified)
        if len(ids) > _CLOSURE_NOTIFIED_MAX:
            ids = ids[-_CLOSURE_NOTIFIED_MAX:]
        temp = _closure_notified_file + ".tmp"
        with open(temp, "w") as f:
            json.dump(ids, f)
        os.replace(temp, _closure_notified_file)
    except Exception as e:
        logger.warning(f"Failed to save closure notifications: {e}")


# =====================================================================
# 🚀  ЗАПУСК
# =====================================================================

async def startup_sequence(exchange: ccxt.Exchange):
    global _exchange_ref
    _exchange_ref = exchange
    """Запуск: сначала бэктест, потом сканер."""
    logger.info("=" * 60)
    logger.info("[STARTUP] Running historical backtest to populate signal history...")
    logger.info("=" * 60)

    # 🆕 FIX: backtest_history() ничего не проверяет и просто ДОПИСЫВАЕТ сигналы
    # поверх существующей истории. Раньше startup_sequence гонял бэктест
    # безусловно на КАЖДОМ рестарте контейнера — а бэктест детерминирован (те же
    # прошлые свечи → те же сигналы), поэтому каждый рестарт задваивал историю
    # (видно по !signals: пары идентичных записей). Теперь бэктест для
    # ticker/tf пропускается, если история для него уже непустая — считаем,
    # что она уже была накоплена (первым запуском или бэктестом, или живой
    # торговлей). Принудительно пересобрать историю — `!reset yes`.
    history = load_signals_history()
    total = 0
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            existing = history.get(ticker, {}).get(tf, {})
            already_populated = any(len(existing.get(side, [])) > 0 for side in ("long", "short"))
            if already_populated:
                logger.info(f"[STARTUP] {ticker} {tf} already has signal history — skipping backtest (use `!reset yes` to force re-run).")
                continue
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
# 📡  SCANNER LOOP
# =====================================================================

# On-chain bias кэш (обновляется раз в час в market_scanner)
_onchain_bias_cache: Optional[Dict] = None
_onchain_last_fetch: float = 0.0

@tasks.loop(seconds=60)
async def market_scanner():
    """Основной цикл сканирования."""
    channel = discord.utils.get(bot.get_all_channels(), name=CHANNEL_NAME)
    if channel is None:
        logger.warning(f"[WARN] Channel '{CHANNEL_NAME}' not found!")
        return

    exchange = _exchange_ref
    if exchange is None:
        return

    # 🆕 Обновляем on-chain bias раз в час (кэш TTL управляется внутри onchain.py)
    global _onchain_bias_cache, _onchain_last_fetch
    now_ts = time.time()
    if ONCHAIN_ENABLED and (now_ts - _onchain_last_fetch) >= 3600:
        try:
            _onchain_bias_cache = await get_onchain_bias()
            _onchain_last_fetch = now_ts
            # Если первый запуск — сбрасываем onchain_bias кеш чтобы следующий
            # hourly цикл пересчитал реальную дельту (baseline уже сохранён в _prev_balances)
            if _onchain_bias_cache.get("flow_data", {}).get("note") == "first_run":
                from onchain import _cache
                _cache.pop("onchain_bias", None)
                logger.info("[ONCHAIN] First run detected — bias cache cleared for next cycle")
            else:
                logger.info(f"[ONCHAIN] Bias refreshed: long={_onchain_bias_cache.get('bias_long',0):+d} short={_onchain_bias_cache.get('bias_short',0):+d}")
        except Exception as e:
            logger.warning(f"[ONCHAIN] Refresh failed: {e}")

    # 🆕 FIX: Load persisted closure notifications
    notified_ids = _load_closure_notified()

    for ticker in list(TICKERS):  # копия списка — защита от мутации через !add/!remove
        for tf in TIMEFRAMES:
            try:
                # 🆕 FIX: Use lock to prevent race with commands
                lock = _state_locks.get(ticker, {}).get(tf)
                if lock is None:
                    lock = asyncio.Lock()
                    if ticker not in _state_locks:
                        _state_locks[ticker] = {}
                    _state_locks[ticker][tf] = lock

                # 🆕 FIX: раньше лок держался весь цикл — check_signals, fetch OHLCV
                # для чарта, генерация чарта (ещё один fetch) и все Discord-сообщения.
                # Из-за этого любая команда (!forcerun/!sim/!tp) на ТУ ЖЕ пару/TF
                # блокировалась на несколько секунд, пока сканер не закончит всю
                # сетевую/Discord-работу. Теперь под локом остаётся только
                # check_signals (там происходит реальное открытие/закрытие позиций
                # и мутация state — это должно быть атомарным относительно команд).
                # Все сетевые вызовы и отправки в Discord вынесены НАРУЖУ лока.
                async with lock:
                    st = state[ticker][tf]
                    signals, bar_time, regime, lev = await check_signals(
                        exchange, ticker, tf, st,
                        onchain_bias=_onchain_bias_cache,
                    )

                    scan_stats["total_scans"] += 1
                    scan_stats["last_scan_time"] = datetime.now(timezone.utc).isoformat()

                    is_new_bar = bool(bar_time and bar_time != st["last_bar_time"])
                    if is_new_bar:
                        st["last_bar_time"] = bar_time

                # ── лок отпущен — дальше только чтение/уведомления, без критичных мутаций ──
                if is_new_bar and signals:
                    # 🆕 Fetch df for volume info
                    bars = await safe_fetch_ohlcv(exchange, ticker, tf, limit=100)
                    df = parse_ohlcv(bars) if bars else None
                    for sig_type, price, reg, leverage, bt, conf, sl, tp, tp1, risk, stats, tp_desc in signals:
                        embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf, sl, tp, tp1, risk, stats, tp_desc, df)
                        try:
                            # Генерируем и прикладываем график к сигналу
                            chart_file = None
                            try:
                                from chart import generate_chart
                                is_long = "BUY" in sig_type or "LONG" in sig_type
                                # 🆕 FIX: раньше здесь считался абсолютный индекс от df,
                                # который фетчился ЗДЕСЬ (limit=100), а generate_chart
                                # внутри себя делает свой отдельный fetch (limit≈300+) —
                                # индексы не совпадали, и стрелка почти всегда улетала
                                # в начало графика. Сигнал всегда формируется на последнем
                                # подтверждённом закрытом баре (iloc[-2]) по правилам бота,
                                # это верно для df ЛЮБОЙ длины — используем offset от конца.
                                state_snapshot = {
                                    "entry": price,
                                    "tp":    tp,
                                    "tp1":   tp1,
                                    "sl":    sl,
                                    "side":  "long" if is_long else "short",
                                    "signal_bar_offset": -2,
                                }
                                chart_buf = await generate_chart(
                                    exchange=exchange,
                                    symbol=ticker,
                                    timeframe=tf,
                                    limit=50,
                                    state_snapshot=state_snapshot,
                                )
                                chart_file = discord.File(chart_buf, filename=f"{ticker.replace('/', '')}_{tf}_signal.png")
                            except Exception as chart_err:
                                logger.warning(f"[CHART] Failed to generate signal chart: {chart_err}")

                            if chart_file:
                                await channel.send(embed=embed, file=chart_file)
                            else:
                                await channel.send(embed=embed)
                            scan_stats["signals_generated"] += 1
                            # 🆕 веб-морда: пушим сигнал всем подключенным WS-клиентам
                            try:
                                from web_api import broadcast_event
                                await broadcast_event({
                                    "type": "signal",
                                    "ticker": ticker,
                                    "tf": tf,
                                    "sig_type": sig_type,
                                    "price": price,
                                    "sl": sl,
                                    "tp": tp,
                                    "tp1": tp1,
                                })
                            except Exception as ws_err:
                                logger.warning(f"[WS] broadcast failed: {ws_err}")
                        except discord.HTTPException as e:
                            logger.error(f"Failed to send signal: {e}")
                        logger.info(f"[SIGNAL] {ticker} {tf} | {sig_type} @ {price:.4f}")

                # 🆕 FIX: Check BOTH tracks for closures independently
                for track in ("a", "u"):
                    trade_key = f"{track}_active_trade"
                    history_key = f"{track}_trade_history"
                    notified_key = f"{track}_last_closure_notified"

                    trade = st.get(trade_key)
                    if not trade and st.get(history_key) and not st.get(notified_key, False):
                        last = st[history_key][-1]
                        if last.get("exit_time"):
                            # Generate unique trade ID with track
                            trade_id = f"{ticker}_{tf}_{track}_{last['exit_time']}"
                            if trade_id not in notified_ids:
                                try:
                                    exit_dt = datetime.fromisoformat(last["exit_time"])
                                    age = (datetime.now(timezone.utc) - exit_dt).total_seconds()
                                    if age < 35:
                                        emoji = "🟢" if last["pnl_pct"] > 0 else "🔴"
                                        track_label = "A" if track == "a" else "U"
                                        await channel.send(
                                            f"{emoji} **Trade Closed [{track_label}-track]** | `{ticker}` `{tf}` | "
                                            f"{last['side'].upper()} | Entry: ${round(last['entry'], 2)} → Exit: ${round(last['exit'], 2)} | "
                                            f"PnL: **{last['pnl_pct']:.2f}%** | Result: **{last['result'].upper()}** | Bars: {last['bars_held']}"
                                        )
                                        notified_ids.add(trade_id)
                                        _save_closure_notified(notified_ids)
                                    st[notified_key] = True
                                except ValueError:
                                    logger.warning(f"Invalid exit_time format: {last.get('exit_time')}")

                # ── TP1 hit check ─────────────────────────────────────────
                for track in ("a", "u"):
                    trade_key = f"{track}_active_trade"
                    trade = st.get(trade_key)
                    if not trade:
                        continue
                    tp1_price = trade.get("tp1")
                    if tp1_price is None or trade.get("tp1_hit"):
                        continue
                    current_price = None
                    try:
                        ticker_data = await asyncio.to_thread(exchange.fetch_ticker, ticker)
                        current_price = ticker_data.get("last")
                    except Exception:
                        pass
                    if current_price is None:
                        continue
                    side = trade.get("side")
                    tp1_reached = (
                        (side == "long"  and current_price >= tp1_price) or
                        (side == "short" and current_price <= tp1_price)
                    )
                    if tp1_reached:
                        trade["tp1_hit"] = True
                        track_label = "A" if track == "a" else "U"
                        entry = trade.get("entry", 0)
                        await channel.send(
                            f"🎯 **TP1 Hit [{track_label}-track]** | `{ticker}` `{tf}` | "
                            f"{side.upper()} | Entry: ${round(entry, 2):,.2f} → TP1: ${round(tp1_price, 2):,.2f}\n"
                            f"⚠️ **Закрой 50% позиции и перенеси SL в безубыток (${round(entry, 2):,.2f})**"
                        )
                        logger.info(f"[TP1] {ticker} {tf} {track_label}-track | TP1 hit @ {current_price}")

                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Scanner error for {ticker} {tf}: {e}", exc_info=True)
                await asyncio.sleep(0.5)

    # 🆕 веб-морда: тик сканера — фронт может обновить статус-панель
    try:
        from web_api import broadcast_event
        await broadcast_event({"type": "scan_tick", "scan_stats": scan_stats})
    except Exception as ws_err:
        logger.warning(f"[WS] broadcast failed: {ws_err}")

# 🆕 FIX: CRITICAL - Task error handler to prevent silent death
@market_scanner.error
async def on_scanner_error(error):
    """Handle scanner loop errors and restart if needed."""
    logger.exception(f"[CRITICAL] Scanner loop crashed: {error}")
    await asyncio.sleep(10)
    if not market_scanner.is_running():
        logger.info("[RECOVERY] Restarting scanner loop...")
        market_scanner.restart()

# =====================================================================
# 🤖  DISCORD COMMANDS
# =====================================================================

# 🆕 FIX BUG-LO002: Флаг защиты от множественных вызовов on_ready
_startup_completed = False

@bot.event
async def on_ready():
    global _startup_completed
    if _startup_completed:
        logger.info("🔄 Reconnect detected — skipping startup sequence.")
        return
    _startup_completed = True

    logger.info(f"✅ {bot.user.name} started! Mode: {MARKET_MODE.upper()} | HTF: {_cfg.HTF_BIAS.upper()} | Pairs: {' | '.join(TICKERS)}")

    if MARKET_MODE == "futures":
        exchange = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    else:
        exchange = ccxt.gate({"enableRateLimit": True})

    global _exchange_ref
    _exchange_ref = exchange
    asyncio.create_task(startup_sequence(exchange))
    await asyncio.sleep(0)  # satisfy async convention

# 🆕 FIX: Global command error handler
@bot.event
async def on_command_error(ctx, error):
    """Global handler for command errors."""
    if isinstance(error, commands.CommandNotFound):
        return  # Ignore unknown commands
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: {error.param.name}")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Bad argument: {error}")
        return
    logger.exception(f"Command error in {ctx.command}: {error}")
    await ctx.send(f"❌ Command failed: {type(error).__name__}: {str(error)[:200]}")

# =====================================================================
# 💬  РЕГИСТРАЦИЯ КОМАНД
# =====================================================================
# 🆕 Все @bot.command(...) вынесены в discord_commands.py (было ~1600 строк в
# одном файле). Импорт здесь — не "неиспользуемый": discord_commands.py делает
# `import bot as core` и регистрирует команды через `@core.bot.command(...)`
# декораторы, которые срабатывают как побочный эффект ИМЕННО этого импорта.
# Обязательно в самом низу файла: к этому моменту в bot.py уже определено всё,
# что нужно discord_commands.py (bot, state, market_scanner, _exchange_ref и т.д.),
# поэтому обратный `import bot as core` внутри discord_commands.py благополучно
# переиспользует уже почти полностью инициализированный модуль bot.py, а не
# создаёт бесконечную рекурсию импорта.
import discord_commands  # noqa: F401,E402

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in .env file!")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    bot.run(DISCORD_TOKEN)
