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
from state import load_signals_history, load_bot_state, save_bot_state, reconcile_orphaned_signals

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

# 🆕 FIX: раньше state строился ТОЛЬКО через make_state() — свежий, пустой,
# каждый запуск процесса. Signals_history.json (аудиторский журнал) переживал
# рестарт, а вот сам факт "эта позиция ещё реально открыта, вот её TP/SL/tp1_hit"
# жил только в памяти и терялся при каждом перезапуске контейнера (в том числе
# при обычном деплое новой версии) — активные, ещё не закрытые сигналы просто
# исчезали из Status/графика, хотя в signals_history.json так и оставались
# висеть с exit_type="open" навсегда. Теперь восстанавливаем снапшот с диска;
# make_state() используется только для тикеров/tf, которых в снапшоте нет
# (новая пара, либо самый первый запуск).
_saved_state = load_bot_state()
state = {
    ticker: {
        tf: _saved_state.get(ticker, {}).get(tf) or make_state()
        for tf in TIMEFRAMES
    }
    for ticker in TICKERS
}
if _saved_state:
    logger.info("[STATE] Restored state snapshot from disk (bot_state_snapshot.json)")

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
reconcile_orphaned_signals(state)
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

# 🆕 FIX: startup (exchange creation + backtest + scanner.start()) used to run
# ONLY from on_ready — meaning if Discord is disabled/unreachable, on_ready
# never fires and NOTHING starts (not the scanner, not signal generation),
# even though scanning has nothing to do with Discord. Split into its own
# idempotent function so main.py can call it directly when Discord is off,
# while on_ready still calls it the normal way when Discord is on.
_engine_started = False

async def ensure_engine_started():
    global _engine_started, _exchange_ref
    if _engine_started:
        return
    _engine_started = True

    if MARKET_MODE == "futures":
        exchange = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    else:
        exchange = ccxt.gate({"enableRateLimit": True})
    _exchange_ref = exchange
    asyncio.create_task(startup_sequence(exchange))


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
            # 🆕 FIX: раньше исключение здесь (например сетевой сбой при фетче
            # исторических баров для ОДНОЙ пары) прерывало всю функцию — и
            # market_scanner.start() в конце никогда не вызывался. Бот оставался
            # "живым" в Discord, но реально не сканировал НИЧЕГО, без единой
            # ошибки в логах кроме отложенного и легко пропускаемого asyncio
            # warning'а. Теперь сбой одной пары не топит бэктест остальных.
            try:
                count = await asyncio.to_thread(
                    backtest_history, exchange, ticker, tf, 3000
                )
                total += count
            except Exception as e:
                logger.error(f"[STARTUP] Backtest failed for {ticker} {tf}: {e}", exc_info=True)
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
_onchain_cache_loaded = _cfg.load_onchain_bias_cache()
_onchain_bias_cache: Optional[Dict] = _onchain_cache_loaded.get("bias")
_onchain_last_fetch: float = _onchain_cache_loaded.get("last_fetch", 0.0)

@tasks.loop(seconds=_cfg.SCAN_INTERVAL_SECONDS)
async def market_scanner():
    """Основной цикл сканирования."""
    # 🆕 FIX: раньше отсутствие Discord-канала (или сам Discord отключённый —
    # см. _cfg.DISCORD_ENABLED) обрывало ВЕСЬ цикл сканирования через ранний
    # return — то есть без Discord не работало вообще ничего, включая веб.
    # Теперь channel — просто Optional: если недоступен, ниже молча
    # пропускаются только сами отправки в канал, сканирование идёт как обычно.
    channel = discord.utils.get(bot.get_all_channels(), name=CHANNEL_NAME) if _cfg.DISCORD_ENABLED else None
    if _cfg.DISCORD_ENABLED and channel is None:
        logger.warning(f"[WARN] Channel '{CHANNEL_NAME}' not found — scanning continues, Discord messages will be skipped this cycle.")

    exchange = _exchange_ref
    if exchange is None:
        return

    # 🆕 Обновляем on-chain bias с интервалом _cfg.ONCHAIN_CACHE_TTL (15m/30m/1h,
    # настраивается из веб-морды — см. set_onchain_interval())
    global _onchain_bias_cache, _onchain_last_fetch
    now_ts = time.time()
    if ONCHAIN_ENABLED and (now_ts - _onchain_last_fetch) >= _cfg.ONCHAIN_CACHE_TTL:
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
            _cfg.save_onchain_bias_cache(_onchain_bias_cache, _onchain_last_fetch)
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

                # 🆕 FIX: раньше save_bot_state() вызывался один раз за ВЕСЬ цикл
                # сканирования (после всех тикеров/tf, см. конец market_scanner), хотя
                # открытие/закрытие позиции мутирует state прямо здесь, внутри лока
                # выше. Если контейнер перезапускался (docker rebuild/restart) в
                # промежутке между тем как сделка открылась в памяти (и уже попала в
                # signals_history.json через add_signal_record — та запись синхронна)
                # и тем как дошла очередь до save_bot_state() в конце цикла — при
                # рестарте active_trade терялся из bot_state.json, ДАЖЕ с
                # примонтированной data/ как volume: файл просто физически не успел
                # записаться. reconcile_orphaned_signals() затем честно, но обидно
                # помечал такую запись как "cancelled", хотя позиция была реально
                # открыта. Сохраняем сразу же, как только signals непустой (позиция
                # открылась или закрылась в этом цикле) — не дожидаясь остальных пар.
                if signals:
                    try:
                        save_bot_state(state)
                    except Exception as snap_err:
                        logger.warning(f"[STATE] immediate snapshot save failed: {snap_err}")
                if is_new_bar and signals:
                    # 🆕 Fetch df for volume info
                    bars = await safe_fetch_ohlcv(exchange, ticker, tf, limit=100)
                    df = parse_ohlcv(bars) if bars else None
                    for sig_type, price, reg, leverage, bt, conf, sl, tp, tp1, risk, stats, tp_desc in signals:
                        embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf, sl, tp, tp1, risk, stats, tp_desc, df)

                        # 🆕 FIX BUG-LO007: раньше Discord-отправка, WS-бродкаст и push
                        # были в ОДНОМ try — если channel.send() падал (не обязательно
                        # discord.HTTPException, чаще всего действительно им и был, но
                        # ловился только этот один тип), WS и push молча пропускались
                        # тоже, хотя это независимые каналы уведомлений. На новой паре
                        # (например, только что добавленный SUI/USDT) сделка реально
                        # открывалась (state уже мутирован в check_signals ДО этого
                        # места), но ни бот, ни веб, ни пуш не сообщали об этом — юзер
                        # узнавал о позиции только зайдя в Status. Теперь каждый канал
                        # уведомления в своём try: падение одного не блокирует остальные.
                        chart_file = None
                        # 🆕 Skip the (expensive, matplotlib) chart render entirely when
                        # nothing will use it — only the Discord embed attaches it.
                        if _cfg.DISCORD_NOTIFICATIONS_ENABLED and channel is not None:
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

                        # 🆕 discord_notifications toggle (Settings → web) — gateway stays
                        # connected either way, this only skips the channel message itself.
                        if _cfg.DISCORD_NOTIFICATIONS_ENABLED and channel is not None:
                            try:
                                if chart_file:
                                    await channel.send(embed=embed, file=chart_file)
                                else:
                                    await channel.send(embed=embed)
                            except Exception as discord_err:
                                logger.error(f"[DISCORD] Failed to send signal for {ticker} {tf}: {discord_err}", exc_info=True)
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
                        # 🆕 Android push — та же информация, что в Discord/WS, но приходит
                        # даже если приложение закрыто. send_push делает блокирующие HTTP-запросы
                        # (firebase-admin), поэтому через to_thread, не в event loop напрямую.
                        try:
                            import push as _push
                            await asyncio.to_thread(
                                _push.send_push,
                                title=f"{sig_type} {ticker} {tf}",
                                body=f"Entry: {price:.4f} | TP: {tp:.4f} | SL: {sl:.4f}",
                                data={"type": "signal", "ticker": ticker, "tf": tf},
                            )
                        except Exception as push_err:
                            logger.warning(f"[PUSH] send failed: {push_err}")
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
                                    emoji = "🟢" if last["pnl_pct"] > 0 else "🔴"
                                    track_label = "A" if track == "a" else "U"
                                    # 🆕 FIX: раньше st[notified_key] = True выставлялся ПОСЛЕ if age < 35
                                    # блока безусловно — если бот обнаруживал закрытие спустя 35+ секунд
                                    # (например, был недоступен/рестартовал ровно в момент TP/SL), сообщение
                                    # в Discord тихо не отправлялось, но помечалось как "уже уведомлено"
                                    # навсегда. Реальная сделка закрылась, а пользователь никогда об этом
                                    # не узнавал. Теперь отправляем в любом случае, помечая опоздание явно.
                                    late_note = "" if age < 35 else " ⏱️ *(delayed notification)*"
                                    # 🆕 FIX: тот же класс проблемы, что чинили для TP1 —
                                    # channel.send() здесь не был обёрнут в свой try/except,
                                    # только внешний except ValueError (ловит только
                                    # datetime.fromisoformat выше, не Discord-исключения).
                                    # Если отправка падала (rate limit, HTTPException), это
                                    # уходило в общий except сканера и обрывало остаток
                                    # обработки этого ticker/tf на цикле — включая TP1/TP2/SL
                                    # live-проверку ниже. Теперь ошибка Discord-отправки не
                                    # мешает остальному, а notified_ids/флаг всё равно
                                    # выставляются (реотправлять сообщение о старом закрытии
                                    # смысла нет — как и раньше, best-effort уведомление).
                                    if _cfg.DISCORD_NOTIFICATIONS_ENABLED and channel is not None:
                                        try:
                                            await channel.send(
                                                f"{emoji} **Trade Closed [{track_label}-track]** | `{ticker}` `{tf}` | "
                                                f"{last['side'].upper()} | Entry: ${round(last['entry'], 2)} → Exit: ${round(last['exit'], 2)} | "
                                                f"PnL: **{last['pnl_pct']:.2f}%** | Result: **{last['result'].upper()}** | Bars: {last['bars_held']}"
                                                f"{late_note}"
                                            )
                                        except Exception as discord_err:
                                            logger.error(f"[DISCORD] Failed to send trade closed for {ticker} {tf}: {discord_err}", exc_info=True)
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
                    if tp1_price is None:
                        continue

                    # 🆕 FIX BUG-LO009: once SL has already moved (post-TP1), the
                    # closed-bar check in check_signals() deliberately ignores bars
                    # that finished before the move (see check_tp_sl_hit) — otherwise
                    # a bar printed before price ever touched TP1 could trip the new,
                    # tighter stop retroactively. That fix means the position has no
                    # protection at all for the rest of the current forming bar (and
                    # possibly one bar after) unless we also check it here, against
                    # the live ticker — which has no such bar-ordering ambiguity: if
                    # price is really at/through the new SL right now, it's a real hit.
                    if trade.get("tp1_hit"):
                        side = trade.get("side")
                        sl = trade.get("sl")
                        tp = trade.get("tp")
                        try:
                            ticker_data = await asyncio.to_thread(exchange.fetch_ticker, ticker)
                            live_price = ticker_data.get("last")
                        except Exception:
                            live_price = None
                        if live_price is not None:
                            # 🆕 FIX: TP2 раньше проверялся только через check_tp_sl_hit()
                            # по high/low ЗАКРЫТОГО бара (anti-repainting конвеншен) — если
                            # цена внутри формирующегося бара касалась TP2, а потом
                            # разворачивалась обратно к SL ДО закрытия бара, бар закрывался
                            # уже без следа касания TP2, и позиция ошибочно закрывалась как
                            # SL, хотя на реальной бирже лимитный TP2-ордер исполнился бы в
                            # момент касания. Проверяем TP2 живым тикером — симметрично тому,
                            # как уже сделано для SL после TP1 чуть ниже.
                            tp_breached = tp is not None and (
                                (side == "long" and live_price >= tp) or
                                (side == "short" and live_price <= tp)
                            )
                            sl_breached = sl is not None and (
                                (side == "long" and live_price <= sl) or
                                (side == "short" and live_price >= sl)
                            )
                            if tp_breached:
                                from signals import close_trade
                                async with lock:
                                    close_trade(st, tp, "tp", ticker, tf, track)
                                logger.info(f"[TP1-TP2] {ticker} {tf} {track.upper()}-track | live TP2 hit @ {live_price} (tp={tp})")
                            elif sl_breached:
                                from signals import close_trade
                                async with lock:
                                    close_trade(st, sl, "sl", ticker, tf, track)
                                logger.info(f"[TP1-SL] {ticker} {tf} {track.upper()}-track | post-TP1 SL hit @ live={live_price} (sl={sl})")
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
                    # 🆕 FIX: исходный SL (до TP1) раньше проверялся только через
                    # check_tp_sl_hit() по low/high ЗАКРЫТОГО бара — асимметрично с TP1,
                    # который уже проверяется live чуть выше в этом же блоке. Свеча могла
                    # проколоть SL внутри бара и вернуться выше к его закрытию — бот видел
                    # бы это только на следующем скане после закрытия, а не в момент касания.
                    # Симметрично TP2/SL-после-TP1 (см. блок выше при tp1_hit=True) — теперь
                    # тем же current_price, без дополнительного похода к бирже.
                    sl = trade.get("sl")
                    sl_breached = sl is not None and (
                        (side == "long" and current_price <= sl) or
                        (side == "short" and current_price >= sl)
                    )
                    if tp1_reached:
                        trade["tp1_hit"] = True
                        # 🆕 FIX: раньше SL после TP1 либо не двигался вовсе, либо был
                        # жёстко захардкожен на безубыток. Теперь берём режим из
                        # _cfg.TP1_SL_MODE — "breakeven" (SL = entry) или "half_tp1"
                        # (SL = entry + половина пути до TP1, строже безубытка, но
                        # каждое срабатывание фиксирует небольшой гарантированный
                        # профит вместо нуля). check_tp_sl_hit() читает trade["sl"]
                        # заново на каждой проверке, так что дальше это применится
                        # само, без дополнительных изменений в signals.py.
                        entry = trade.get("entry", 0)
                        if _cfg.TP1_SL_MODE == "half_tp1":
                            if side == "long":
                                new_sl = entry + (tp1_price - entry) / 2
                            else:
                                new_sl = entry - (entry - tp1_price) / 2
                            sl_label = f"половина пути до TP1 (${round(new_sl, 2):,.2f})"
                            sl_label_en = f"halfway to TP1 (${round(new_sl, 2):,.2f})"
                        else:
                            new_sl = entry
                            sl_label = f"безубыток (${round(entry, 2):,.2f})"
                            sl_label_en = f"breakeven (${round(entry, 2):,.2f})"
                        trade["sl"] = new_sl
                        # 🆕 FIX BUG-LO009: SL перенесён ВНУТРИ формирующегося бара —
                        # low/high этого и всех предыдущих баров напечатаны ДО того,
                        # как цена вообще коснулась TP1, и check_tp_sl_hit() (signals.py)
                        # больше не сверяет новый стоп против них — только против баров,
                        # закрывшихся ПОСЛЕ этого момента. bar_time берём из state
                        # текущего скана (см. check_signals), не из этого тикер-запроса.
                        # 🆕 FIX (Kimi review): изначально ставили ровно на последний
                        # закрытый бар (X) — но low/high ТЕКУЩЕГО формирующегося бара (Y),
                        # в котором и произошло касание TP1, тоже мог быть напечатан ДО
                        # касания (бар открылся ниже полу-пути, потом вырос до TP1) — тот
                        # же класс ложного срабатывания, просто в более узком окне. Бар Y и так
                        # покрыт live-проверкой по тикеру ниже (при tp1_hit=True), поэтому
                        # безопасно исключить его из барной проверки тоже — сдвигаем порог
                        # на +1 таймфрейм вперёд, так барная проверка начнёт применяться
                        # только с бара Z (первого, целиком закрывшегося ПОСЛЕ переноса).
                        try:
                            tf_ms = int(exchange.parse_timeframe(tf) * 1000)
                        except Exception:
                            tf_ms = 3600_000
                        trade["sl_moved_after_bar"] = (st.get("last_processed_bar_time") or 0) + tf_ms
                        track_label = "A" if track == "a" else "U"
                        # 🆕 FIX: раньше эта отправка не была обёрнута в try/except —
                        # единственная из трёх (Discord/WS/push) в этом блоке. Если
                        # channel.send() кидал исключение (rate limit, отсутствие прав,
                        # discord.HTTPException), это ронял всю итерацию сканера и молча
                        # съедало И WS-broadcast, И push для этого же TP1-события — тот же
                        # класс проблемы, что чинили в BUG-LO007, просто не применённый
                        # к новому коду. Теперь ошибка Discord-отправки не мешает
                        # остальным каналам уведомления сработать.
                        if _cfg.DISCORD_NOTIFICATIONS_ENABLED and channel is not None:
                            try:
                                await channel.send(
                                    f"🎯 **TP1 Hit [{track_label}-track]** | `{ticker}` `{tf}` | "
                                    f"{side.upper()} | Entry: ${round(entry, 2):,.2f} → TP1: ${round(tp1_price, 2):,.2f}\n"
                                    f"⚠️ **Закрой 50% позиции и перенеси SL в {sl_label}**"
                                )
                            except Exception as discord_err:
                                logger.error(f"[DISCORD] Failed to send TP1 hit for {ticker} {tf}: {discord_err}", exc_info=True)
                        logger.info(f"[TP1] {ticker} {tf} {track_label}-track | TP1 hit @ {current_price} | SL mode={_cfg.TP1_SL_MODE} → {new_sl}")
                        try:
                            from web_api import broadcast_event
                            await broadcast_event({
                                "type": "tp1_hit",
                                "ticker": ticker,
                                "tf": tf,
                                "track": track,
                                "side": side,
                                "entry": entry,
                                "tp1": tp1_price,
                                "new_sl": new_sl,
                                "sl_mode": _cfg.TP1_SL_MODE,
                            })
                        except Exception as ws_err:
                            logger.warning(f"[WS] broadcast failed: {ws_err}")
                        try:
                            import push as _push
                            await asyncio.to_thread(
                                _push.send_push,
                                title=f"🎯 TP1 Hit {ticker} {tf}",
                                body=f"Close 50%, move SL to {sl_label_en}",
                                data={"type": "tp1_hit", "ticker": ticker, "tf": tf, "track": track},
                            )
                        except Exception as push_err:
                            logger.warning(f"[PUSH] send failed: {push_err}")
                    elif sl_breached:
                        from signals import close_trade
                        async with lock:
                            close_trade(st, sl, "sl", ticker, tf, track)
                        logger.info(f"[LIVE-SL] {ticker} {tf} {track.upper()}-track | pre-TP1 SL hit @ live={current_price} (sl={sl})")

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

    # 🆕 Снапшот активных позиций на диск — раз в цикл сканирования (не на каждое
    # событие: дёшево при таком размере state, но не нужно писать чаще). Это
    # safety net на случай НЕ-graceful остановки (docker stop -t 0, OOM kill),
    # когда SIGTERM-хендлер в main.py не успевает сработать — при обычном
    # graceful restart снапшот и так пишется там же, дублирование безвредно.
    try:
        save_bot_state(state)
    except Exception as snap_err:
        logger.warning(f"[STATE] snapshot save failed: {snap_err}")

    # 🆕 A scan cycle reached this point without raising — reset the crash
    # counter so sporadic, unrelated failures spread over days/weeks don't
    # eventually accumulate past _MAX_SCANNER_RESTARTS and permanently disable
    # auto-restart (see on_scanner_error below).
    global _scanner_crash_count
    _scanner_crash_count = 0


def set_scan_interval(seconds: int):
    """Live-changes the scanner loop interval (Settings → web) — no restart needed.
    discord.ext.tasks.Loop.change_interval() takes effect after the current
    iteration finishes; it doesn't interrupt an in-progress scan."""
    if seconds not in _cfg.SCAN_INTERVAL_OPTIONS:
        raise ValueError(f"scan interval must be one of {_cfg.SCAN_INTERVAL_OPTIONS}, got {seconds}")
    market_scanner.change_interval(seconds=seconds)
    _cfg.SCAN_INTERVAL_SECONDS = seconds
    _cfg.save_scan_interval(seconds)
    logger.info(f"[SCAN] Interval changed to {seconds}s")


def set_onchain_interval(seconds: int):
    """Live-changes the on-chain refresh interval (Settings → web) — no restart
    needed. Unlike the scanner, on-chain refresh isn't its own tasks.loop — it's
    just a time.time() - _onchain_last_fetch check inside market_scanner each
    cycle, so changing _cfg.ONCHAIN_CACHE_TTL takes effect on the next check."""
    if seconds not in _cfg.ONCHAIN_INTERVAL_OPTIONS:
        raise ValueError(f"onchain interval must be one of {_cfg.ONCHAIN_INTERVAL_OPTIONS}, got {seconds}")
    _cfg.ONCHAIN_CACHE_TTL = seconds
    _cfg.save_onchain_interval(seconds)
    logger.info(f"[ONCHAIN] Interval changed to {seconds}s")


# 🆕 FIX: CRITICAL - Task error handler to prevent silent death
# 🆕 FIX: unbounded restart loop had no ceiling — a persistent error (corrupted
# state file, bad API key, etc.) would crash → sleep 10s → restart → crash again,
# forever, with a fixed 10s delay regardless of how many times it already failed.
# Now backs off exponentially (capped at 5 min) and gives up after too many
# crashes in a row instead of spinning forever.
_scanner_crash_count = 0
_MAX_SCANNER_RESTARTS = 5

@market_scanner.error
async def on_scanner_error(error):
    """Handle scanner loop errors and restart if needed, with backoff + a ceiling."""
    global _scanner_crash_count
    _scanner_crash_count += 1
    logger.exception(f"[CRITICAL] Scanner loop crashed ({_scanner_crash_count}/{_MAX_SCANNER_RESTARTS}): {error}")

    if _scanner_crash_count > _MAX_SCANNER_RESTARTS:
        logger.critical(
            f"[FATAL] Scanner crashed {_scanner_crash_count} times in a row — "
            "giving up on auto-restart. Fix the underlying issue and restart the container."
        )
        return

    wait = min(10 * (2 ** (_scanner_crash_count - 1)), 300)
    await asyncio.sleep(wait)
    if not market_scanner.is_running():
        logger.info(f"[RECOVERY] Restarting scanner loop (after {wait}s backoff)...")
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
    await ensure_engine_started()

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
