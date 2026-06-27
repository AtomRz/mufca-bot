import asyncio
import math
import json
import os
import time
import re
import numpy as np
import pandas as pd
import discord
from discord.ext import tasks, commands
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Dict, List
import logging

import ccxt

import config as _cfg
from config import (
    DISCORD_TOKEN,
    CHANNEL_NAME,
    TICKERS,
    TIMEFRAMES,
    DATA_DIR,
    MIN_TP_PCT,
    MAX_TP_PCT,
    MAX_HOLD_BARS,
    MARKET_MODE,
    CHOP_THRESHOLD,
    PAIRS_FILE,
    SIGNALS_HISTORY_FILE,
    ATR_PERIOD,
    FRAMA_LEN,
    FRAMA_MULT,
    save_tickers,
    save_tp_config,
    ONCHAIN_ENABLED,
    GATE_API_KEY,
    GATE_SECRET,
)
from utils import safe_fetch_ohlcv, parse_ohlcv, validate_dataframe
from indicators import calculate_atr, calculate_frama
from volume_indicators import volume_flow_signal_v3, volume_score_for_side
from signals import check_signals, backtest_history, make_state, calculate_sl, clear_htf_cache
from onchain import get_onchain_bias, format_onchain_report, clear_onchain_cache, clear_onchain_cache_full
from state import load_signals_history, calculate_combined_tp, add_signal_record, update_signal_record, clear_history_cache, get_signal_stats
from execution import GateExecutor

logger = logging.getLogger(__name__)

def _flow_label(flow: str) -> str:
    return {"inflow": "BUY PRESSURE", "outflow": "SELL PRESSURE"}.get(flow, "NEUTRAL")

# =====================================================================
# 🤖  DISCORD BOT
# =====================================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

state = {ticker: {tf: make_state() for tf in TIMEFRAMES} for ticker in TICKERS}

def _heal_state():
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st = state[ticker][tf]
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

_state_locks: Dict[str, Dict[str, asyncio.Lock]] = {}
_tickers_lock = asyncio.Lock()

def _ensure_locks():
    for ticker in TICKERS:
        if ticker not in _state_locks:
            _state_locks[ticker] = {}
        for tf in TIMEFRAMES:
            if tf not in _state_locks[ticker]:
                _state_locks[ticker][tf] = asyncio.Lock()

_ensure_locks()

_exchange_ref: Optional[ccxt.Exchange] = None
_executor: Optional[GateExecutor] = None

_closure_notified_file = os.path.join(DATA_DIR, "closure_notified.json")

def _load_closure_notified() -> set:
    try:
        with open(_closure_notified_file, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_closure_notified(notified: set):
    try:
        temp = _closure_notified_file + ".tmp"
        with open(temp, "w") as f:
            json.dump(list(notified), f)
        os.replace(temp, _closure_notified_file)
    except Exception as e:
        logger.warning(f"Failed to save closure notifications: {e}")

# =====================================================================
# 🆕 SIGNAL REGISTRY
# =====================================================================

@dataclass
class SignalEntry:
    id: str
    ticker: str
    tf: str
    side: str
    track: str
    entry_price: float
    sl: float
    tp: float
    lev: int
    confidence: int
    regime: str
    timestamp: float
    message_id: Optional[int] = None

class SignalRegistry:
    TTL = 1800

    def __init__(self):
        self._signals: Dict[str, SignalEntry] = {}
        self._counter = 0

    def register(self, ticker, tf, side, track, entry, sl, tp, lev, conf, regime) -> str:
        self._counter += 1
        sig_id = f"sig{self._counter:03d}"
        self._signals[sig_id] = SignalEntry(
            id=sig_id, ticker=ticker.upper(), tf=tf, side=side, track=track,
            entry_price=entry, sl=sl, tp=tp, lev=lev,
            confidence=conf, regime=regime, timestamp=time.time(),
        )
        self._cleanup()
        return sig_id

    def _cleanup(self):
        now = time.time()
        expired = [k for k, v in self._signals.items() if now - v.timestamp > self.TTL]
        for k in expired:
            del self._signals[k]

    def list_active(self) -> List[SignalEntry]:
        self._cleanup()
        return sorted(self._signals.values(), key=lambda x: x.timestamp)

    def get(self, sig_id: str) -> Optional[SignalEntry]:
        self._cleanup()
        return self._signals.get(sig_id.lower())

    def find(self, ticker: str, side: str) -> Optional[SignalEntry]:
        self._cleanup()
        ticker = ticker.upper()
        side = side.lower()
        matches = [v for v in self._signals.values() if v.ticker == ticker and v.side == side]
        return matches[-1] if matches else None

_signal_registry = SignalRegistry()

# =====================================================================
# 🚀  ЗАПУСК
# =====================================================================

async def startup_sequence(exchange: ccxt.Exchange):
    global _exchange_ref, _executor
    _exchange_ref = exchange
    _executor = GateExecutor(exchange)
    
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
                sl, tp, risk, stats, tp_desc: str = "", df=None) -> discord.Embed:
    is_long = "BUY" in signal_type or "LONG" in signal_type
    is_a_track = "Andean" in signal_type or "A " in signal_type
    is_u_track = "UT Bot" in signal_type or "U " in signal_type
    coin_emoji = "🟡" if "BTC" in ticker else "🔷" if "ETH" in ticker else "🟣"
    track_emoji = "🔵" if is_a_track else "🟢" if is_u_track else "⚪"
    conf_color = "🟢" if confidence >= 80 else "🟡" if confidence >= 60 else "🔴"
    mode_label = "Spot" if MARKET_MODE == "spot" else "Futures"
    ha_label = "HA" if _cfg.UT_HEIKIN_ASHI else "Normal"
    rr = round(abs(tp - price) / max(risk, 1e-8), 2)
    tp_pct = abs(tp - price) / price * 100

    tp_source = (
        f"📚 Adaptive (last {stats['count']} signals, {_cfg.TP_PERCENTILE*100:.0f}th %ile)"
        if stats["count"] >= 5 else "📐 Fixed R:R = 2.0"
    )

    embed = discord.Embed(
        title=f"🚨 MUFCA v3.1 {coin_emoji} {'📈 LONG' if is_long else '📉 SHORT'}",
        color=discord.Color.green() if is_long else discord.Color.red(),
    )
    embed.add_field(name="📈 Pair", value=f"**{ticker}**", inline=True)
    embed.add_field(name="⏱ TF", value=tf.upper(), inline=True)
    embed.add_field(name=f"{track_emoji} Track", value=signal_type.strip(), inline=True)
    embed.add_field(name="🧬 HTF Bias", value=f"✅ {_cfg.HTF_BIAS.upper()} FRAMA confirmed", inline=True)
    embed.add_field(name="💵 Entry", value=f"${round(price, 2):,.2f}", inline=True)
    embed.add_field(name="🛑 Stop Loss", value=f"${round(sl, 2):,.2f}", inline=True)
    embed.add_field(name="🎯 Take Profit", value=f"${round(tp, 2):,.2f} (+{tp_pct:.2f}%)", inline=True)
    embed.add_field(name="📊 Risk/Reward", value=f"1:{rr}", inline=True)
    embed.add_field(name="⚙️ Regime", value=regime, inline=True)
    embed.add_field(name="⚠️ Leverage", value=f"x{leverage}", inline=True)
    embed.add_field(name=f"{conf_color} AI Conf", value=f"{confidence}%", inline=True)
    embed.add_field(name="🕯️ UT Bot", value=f"Heikin Ashi: {'✅' if _cfg.UT_HEIKIN_ASHI else '❌'}", inline=True)
    if df is not None:
        try:
            vol_info = volume_flow_signal_v3(df)
            vol_flow = vol_info["flow"]
            vol_emoji = "🟢" if vol_flow == "inflow" else "🔴" if vol_flow == "outflow" else "⚪"
            rel_vol = vol_info["rel_vol"]
            dir_score = volume_score_for_side(vol_info, "long" if is_long else "short")
            lev_adj = "+" if dir_score > 0.3 else "-" if dir_score < -0.3 else "="
            vol_text = f"{_flow_label(vol_flow)} RV:{rel_vol:.1f}x [{lev_adj}lev]"
            embed.add_field(name=f"{vol_emoji} Volume", value=vol_text, inline=True)
        except Exception as e:
            logger.debug(f"Volume info error in build_embed: {e}")

    embed.add_field(name="📚 TP Source", value=tp_source, inline=False)
    if stats["count"] >= 5:
        embed.add_field(name="📈 Signal Stats",
                        value=f"Avg MFE: {stats['avg_mfe']:.2f}% | Best: {stats['best']:.2f}% | Signals: {stats['count']}",
                        inline=False)
    if tp_desc:
        embed.add_field(name="🧠 TP Logic", value=tp_desc, inline=False)
    embed.set_footer(text=f"MUFCA [AtomDC] v3.1 • Gate.io {mode_label} • HTF:{_cfg.HTF_BIAS.upper()} • UT:{ha_label}")
    return embed

# =====================================================================
# 📡  SCANNER LOOP
# =====================================================================

_onchain_bias_cache: Optional[Dict] = None
_onchain_last_fetch: float = 0.0

@tasks.loop(seconds=20)
async def market_scanner():
    channel = discord.utils.get(bot.get_all_channels(), name=CHANNEL_NAME)
    if channel is None:
        logger.warning(f"[WARN] Channel '{CHANNEL_NAME}' not found!")
        return

    exchange = _exchange_ref
    if exchange is None:
        return

    global _onchain_bias_cache, _onchain_last_fetch
    now_ts = time.time()
    if ONCHAIN_ENABLED and (now_ts - _onchain_last_fetch) >= 3600:
        try:
            _onchain_bias_cache = await get_onchain_bias()
            _onchain_last_fetch = now_ts
            if _onchain_bias_cache.get("flow_data", {}).get("note") == "first_run":
                from onchain import _cache
                _cache.pop("onchain_bias", None)
                logger.info("[ONCHAIN] First run detected — bias cache cleared for next cycle")
            else:
                logger.info(f"[ONCHAIN] Bias refreshed: long={_onchain_bias_cache.get('bias_long',0):+d} short={_onchain_bias_cache.get('bias_short',0):+d}")
        except Exception as e:
            logger.warning(f"[ONCHAIN] Refresh failed: {e}")

    notified_ids = _load_closure_notified()

    for ticker in list(TICKERS):
        for tf in TIMEFRAMES:
            try:
                lock = _state_locks.get(ticker, {}).get(tf)
                if lock is None:
                    lock = asyncio.Lock()
                    if ticker not in _state_locks:
                        _state_locks[ticker] = {}
                    _state_locks[ticker][tf] = lock

                async with lock:
                    st = state[ticker][tf]
                    signals, bar_time, regime, lev = await check_signals(
                        exchange, ticker, tf, st,
                        onchain_bias=_onchain_bias_cache,
                    )

                    scan_stats["total_scans"] += 1
                    scan_stats["last_scan_time"] = datetime.now(timezone.utc).isoformat()

                    if bar_time and bar_time != st["last_bar_time"]:
                        st["last_bar_time"] = bar_time
                        bars = await safe_fetch_ohlcv(exchange, ticker, tf, limit=100)
                        df = parse_ohlcv(bars) if bars else None
                        for sig_type, price, reg, leverage, bt, conf, sl, tp, risk, stats, tp_desc in signals:
                            embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf, sl, tp, risk, stats, tp_desc, df)
                            
                            is_long = "BUY" in sig_type or "LONG" in sig_type
                            side_clean = "long" if is_long else "short"
                            track_clean = "a" if ("Andean" in sig_type or "A " in sig_type) else "u"
                            sig_id = _signal_registry.register(
                                ticker, tf, side_clean, track_clean,
                                price, sl, tp, leverage, conf, reg
                            )
                            embed.add_field(name="🆔 Signal ID", value=f"`{sig_id}`", inline=True)
                            
                            try:
                                msg = await channel.send(embed=embed)
                                _signal_registry._signals[sig_id].message_id = msg.id
                                scan_stats["signals_generated"] += 1
                            except discord.HTTPException as e:
                                logger.error(f"Failed to send signal: {e}")
                            logger.info(f"[SIGNAL] {ticker} {tf} | {sig_type} @ {price:.4f} | ID={sig_id}")

                    for track in ("a", "u"):
                        trade_key = f"{track}_active_trade"
                        history_key = f"{track}_trade_history"
                        notified_key = f"{track}_last_closure_notified"

                        trade = st.get(trade_key)
                        if not trade and st.get(history_key) and not st.get(notified_key, False):
                            last = st[history_key][-1]
                            if last.get("exit_time"):
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

                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Scanner error for {ticker} {tf}: {e}", exc_info=True)
                await asyncio.sleep(0.5)

@market_scanner.error
async def on_scanner_error(error):
    logger.exception(f"[CRITICAL] Scanner loop crashed: {error}")
    await asyncio.sleep(10)
    if not market_scanner.is_running():
        logger.info("[RECOVERY] Restarting scanner loop...")
        market_scanner.restart()

# =====================================================================
# 🤖  DISCORD COMMANDS
# =====================================================================

_startup_completed = False

@bot.event
async def on_ready():
    global _startup_completed
    if _startup_completed:
        logger.info("🔄 Reconnect detected — skipping startup sequence.")
        return
    _startup_completed = True

    logger.info(f"✅ {bot.user.name} started! Mode: {MARKET_MODE.upper()} | HTF: {_cfg.HTF_BIAS.upper()} | Pairs: {' | '.join(TICKERS)}")

    ex_config = {"enableRateLimit": True}
    if GATE_API_KEY and GATE_SECRET:
        ex_config["apiKey"] = GATE_API_KEY
        ex_config["secret"] = GATE_SECRET
        logger.info("[GATE] API keys loaded — trading enabled")
    else:
        logger.info("[GATE] No API keys — read-only mode")

    if MARKET_MODE == "futures":
        ex_config["options"] = {"defaultType": "swap"}
    
    exchange = ccxt.gate(ex_config)

    global _exchange_ref
    _exchange_ref = exchange
    asyncio.create_task(startup_sequence(exchange))
    await asyncio.sleep(0)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: {error.param.name}")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Bad argument: {error}")
        return
    logger.exception(f"Command error in {ctx.command}: {error}")
    await ctx.send(f"❌ Command failed: {type(error).__name__}: {str(error)[:200]}")

@bot.command(name="help", aliases=["?"])
async def help_cmd(ctx):
    lines = [
        "**📖 MUFCA v3.1 — Команды**\n",

        "**📊 Мониторинг**",
        "`!status`       — состояние сканера: пары, треки A/U, volume",
        "`!scan <pair> <tf>` — ручной скан",
        "`!history <pair> <tf>` — история сделок",
        "`!signals <pair> <tf>` — статистика сигналов по паре",
        "`!tp <pair> <tf>` — текущий адаптивный TP",
        "`!debug`        — расширенная отладочная информация",
        "`!onchain`      — on-chain анализ",
        "",
        
        "**💰 Торговля**",
        "`!trade`        — список активных сигналов",
        "`!trade sig001` — открыть по ID сигнала",
        "`!trade 1`      — открыть по номеру из списка",
        "`!trade ETH/USDT long` — по паре (ищет сигнал, иначе ручной)",
        "`!trade BTC/USDT long qty=50 lev=5 tp=4% sl=2%` — с переопределениями",
        "`!positions`    — открытые позиции",
        "`!close <pair>` — закрыть позицию по рынку",
        "",

        "**⚙️ Настройки**",
        "`!mode spot|futures` — переключить режим торговли",
        "`!htf <tf>`     — HTF Bias таймфрейм",
        "`!utha on|off`  — Heikin Ashi для UT Bot",
        "`!chop <tf> <val>` — порог CHOP",
        "",

        "**📚 Адаптивный TP**",
        "`!tpconfig`           — показать текущий конфиг TP",
        "`!tpconfig mode safe` — безопасный режим",
        "`!tpconfig mode aggressive` — агрессивный",
        "`!tpconfig limit 30`  — кол-во сигналов для обучения",
        "",

        "**📋 Пары**",
        "`!pairs`        — список активных пар",
        "`!add <pair>`   — добавить пару",
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

# =====================================================================
# 🆕 TRADE VIEW & HELPERS
# =====================================================================

class TradeConfirmView(discord.ui.View):
    def __init__(self, ticker, side, amount, leverage, tp, sl, order_type, limit_price, use_cost):
        super().__init__(timeout=120)
        self.ticker = ticker
        self.side = side
        self.amount = amount
        self.leverage = leverage
        self.tp = tp
        self.sl = sl
        self.order_type = order_type
        self.limit_price = limit_price
        self.use_cost = use_cost

    @discord.ui.button(label="🚀 Открыть позицию", style=discord.ButtonStyle.green, custom_id="confirm_trade")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg, order = await _executor.open_position(
            symbol=self.ticker,
            side=self.side,
            amount=self.amount,
            leverage=self.leverage,
            tp_price=self.tp,
            sl_price=self.sl,
            order_type=self.order_type,
            limit_price=self.limit_price,
            use_cost=self.use_cost,
        )
        if ok:
            embed = discord.Embed(title="✅ Позиция открыта", color=discord.Color.green())
            embed.add_field(name="Order ID", value=order.get("id", "N/A"), inline=True)
            embed.add_field(name="Status", value=order.get("status", "open"), inline=True)
            embed.add_field(name="Filled", value=f"{order.get('filled', 0)} / {order.get('amount', 0)}", inline=True)
            await interaction.followup.send(embed=embed, ephemeral=False)
        else:
            await interaction.followup.send(msg, ephemeral=True)
        self.stop()

    @discord.ui.button(label="❌ Отмена", style=discord.ButtonStyle.red, custom_id="cancel_trade")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Отменено", ephemeral=True)
        self.stop()


def _parse_trade_args(raw: str) -> dict:
    return {m.group(1).lower(): m.group(2) for m in re.finditer(r'(\w+)=([^\s]+)', raw or "")}


@bot.command(name="trade")
async def trade_cmd(ctx, arg1: str = "", arg2: str = "", *, raw_args: str = ""):
    exchange = _exchange_ref
    if exchange is None or _executor is None:
        await ctx.send("❌ Executor не готов. Проверьте API-ключи Gate.io.")
        return

    if not arg1:
        active = _signal_registry.list_active()
        if not active:
            await ctx.send(
                "📭 Нет активных сигналов (истекли 30 мин).\n"
                "Для ручной торговли: `!trade <pair> <long|short>`"
            )
            return

        lines = ["**🎯 Активные сигналы (последние 30 мин):**\n"]
        for i, sig in enumerate(active, 1):
            emoji = "🟢" if sig.side == "long" else "🔴"
            lines.append(
                f"{emoji} **#{i}** `{sig.id}` | `{sig.ticker}` `{sig.tf}` | "
                f"{sig.side.upper()} | Entry: ${sig.entry_price:,.2f} | "
                f"TP: ${sig.tp:,.2f} | SL: ${sig.sl:,.2f} | Lev: {sig.lev}x"
            )
        lines.append("\n**Открыть:** `!trade <ID>` или `!trade <#номер>` или `!trade <pair> <side>`")
        await ctx.send("\n".join(lines))
        return

    sig = None
    is_pair = "/" in arg1
    is_id = arg1.lower().startswith("sig")
    is_number = arg1.isdigit() and not is_pair

    if is_id:
        sig = _signal_registry.get(arg1)
    elif is_number:
        active = _signal_registry.list_active()
        idx = int(arg1) - 1
        if 0 <= idx < len(active):
            sig = active[idx]

    manual = False
    if sig is None and is_pair:
        ticker = arg1.upper()
        side = arg2.lower()
        if side not in ("long", "short"):
            await ctx.send("❌ Сторона должна быть `long` или `short`")
            return
        sig = _signal_registry.find(ticker, side)
        if sig is None:
            manual = True
    elif sig is None and not is_pair:
        await ctx.send(
            f"❌ Не понял аргумент `{arg1}`.\n"
            f"Используйте: `!trade <ID>`, `!trade <#номер>`, или `!trade <pair> <long|short>`"
        )
        return

    ticker = sig.ticker if sig else arg1.upper()
    side = sig.side if sig else arg2.lower()
    if side not in ("long", "short"):
        await ctx.send("❌ Укажите сторону: `long` или `short`")
        return

    try:
        tick = await asyncio.to_thread(exchange.fetch_ticker, ticker)
        current_price = float(tick.get("last") or tick.get("close"))
    except Exception as e:
        await ctx.send(f"❌ Не получить цену: {e}")
        return

    args = _parse_trade_args(raw_args)

    qty_raw = args.get("qty", "auto")
    if qty_raw.endswith("%"):
        pct = float(qty_raw[:-1]) / 100
        balance = await _executor.fetch_balance_usdt()
        qty_usdt = balance * pct
    elif qty_raw.lower() == "auto":
        balance = await _executor.fetch_balance_usdt()
        qty_usdt = balance * 0.05
    else:
        qty_usdt = float(qty_raw) if qty_raw else 0
    use_cost = True

    lev = int(args.get("lev", sig.lev if sig else 1))

    def parse_price(raw, default):
        if not raw or raw.lower() == "auto":
            return default
        if raw.endswith("%"):
            pct = float(raw[:-1]) / 100
            return current_price * (1 + pct) if side == "long" else current_price * (1 - pct)
        return float(raw)

    default_tp = sig.tp if sig else (current_price * 1.02 if side == "long" else current_price * 0.98)
    default_sl = sig.sl if sig else (current_price * 0.98 if side == "long" else current_price * 1.02)
    tp = parse_price(args.get("tp"), default_tp)
    sl = parse_price(args.get("sl"), default_sl)

    order_type = args.get("type", "market").lower()
    limit_price = float(args["price"]) if "price" in args else None

    if _cfg.MARKET_MODE != "futures" and lev > 1:
        await ctx.send("⚠️ Плечо доступно только в futures. `!mode futures`")
        lev = 1

    source = "📡 Сигнал" if sig else "✋ Ручной режим"
    embed = discord.Embed(
        title=f"{'🟢' if side=='long' else '🔴'} Подтверждение | {source}",
        description=f"**{ticker}** | {_cfg.MARKET_MODE.upper()}",
        color=discord.Color.green() if side == "long" else discord.Color.red(),
    )
    embed.add_field(name="Side", value=side.upper(), inline=True)
    embed.add_field(name="Type", value=order_type.upper(), inline=True)
    embed.add_field(name="Leverage", value=f"{lev}x", inline=True)
    embed.add_field(name="Entry (est.)", value=f"${current_price:,.2f}", inline=True)
    embed.add_field(name="Qty", value=f"{qty_usdt:,.2f} USDT", inline=True)
    balance = await _executor.fetch_balance_usdt()
    embed.add_field(name="Balance", value=f"${balance:,.2f} USDT", inline=True)
    embed.add_field(name="🎯 TP", value=f"${tp:,.2f} ({((tp/current_price-1)*100 if side=='long' else (1-tp/current_price)*100):+.2f}%)", inline=True)
    embed.add_field(name="🛑 SL", value=f"${sl:,.2f} ({((sl/current_price-1)*100 if side=='long' else (1-sl/current_price)*100):+.2f}%)", inline=True)
    if limit_price:
        embed.add_field(name="Limit", value=f"${limit_price:,.2f}", inline=True)
    if sig:
        embed.add_field(name="🆔 Signal", value=f"`{sig.id}` | Conf: {sig.confidence}%", inline=True)
        embed.set_footer(text=f"Track: {sig.track.upper()} | Regime: {sig.regime} | ID: {sig.id}")

    view = TradeConfirmView(
        ticker=ticker,
        side="buy" if side == "long" else "sell",
        amount=qty_usdt,
        leverage=lev,
        tp=tp,
        sl=sl,
        order_type=order_type,
        limit_price=limit_price,
        use_cost=use_cost,
    )
    await ctx.send(embed=embed, view=view)


@bot.command(name="positions")
async def positions_cmd(ctx):
    pos = _executor.get_positions() if _executor else {}
    if not pos:
        await ctx.send("📭 Нет открытых позиций в памяти бота")
        return
    lines = ["**📊 Открытые позиции:**\n"]
    for sym, p in pos.items():
        lines.append(
            f"• `{sym}` {p['side'].upper()} | Entry: ${p['entry']:,.2f} | "
            f"Lev: {p['leverage']}x | TP: ${p['tp']:,.2f} | SL: ${p['sl']:,.2f}"
        )
    await ctx.send("\n".join(lines))


@bot.command(name="close")
async def close_cmd(ctx, ticker: str = ""):
    if not ticker:
        await ctx.send("❌ Укажите пару: `!close BTC/USDT`")
        return
    ticker = ticker.upper()
    if _executor is None:
        await ctx.send("❌ Executor не инициализирован")
        return
    success, msg = await _executor.close_position(ticker)
    await ctx.send(msg)

@bot.command(name="status")
async def status_cmd(ctx):
    ha_status = "✅ ON" if _cfg.UT_HEIKIN_ASHI else "❌ OFF"
    lines = [
        f"**MUFCA v3.1 — Scanner Status**\n",
        f"🧬 HTF Bias: **{_cfg.HTF_BIAS.upper()}**\n",
        f"🕯️ UT Bot Heikin Ashi: **{ha_status}**\n",
        f"📚 Adaptive TP: last **{_cfg.SIGNAL_HISTORY_LIMIT}** signals | **{(_cfg.SAFE_TP_PERCENTILE if _cfg.USE_SAFE_TP else _cfg.TP_PERCENTILE)*100:.0f}th** percentile ({'SAFE 🛡️' if _cfg.USE_SAFE_TP else 'AGGRESSIVE ⚡'})\n",
    ]
    exchange = _exchange_ref
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st = state[ticker][tf]
            last = st["last_bar_time"]
            ts = "no data"
            if last is not None:
                try:
                    ts_val = int(last)
                    ts = f"<t:{ts_val // 1000}:R>"
                except (ValueError, TypeError):
                    ts = str(last)
            a_trade = st.get("a_active_trade")
            u_trade = st.get("u_active_trade")

            a_flag = "LONG" if st["a_in_long"] else "SHORT" if st["a_in_short"] else None
            u_flag = "LONG" if st["u_in_long"] else "SHORT" if st["u_in_short"] else None

            async with _state_locks[ticker][tf]:
                if a_flag and not a_trade:
                    logger.warning(f"[STATE] A-track desync fixed for {ticker} {tf}")
                    state[ticker][tf]["a_in_long"] = False
                    state[ticker][tf]["a_in_short"] = False
                    a_pos = f"⚠️{a_flag} (fixed)"
                else:
                    a_pos = a_flag or "—"

                if u_flag and not u_trade:
                    logger.warning(f"[STATE] U-track desync fixed for {ticker} {tf}")
                    state[ticker][tf]["u_in_long"] = False
                    state[ticker][tf]["u_in_short"] = False
                    u_pos = f"⚠️{u_flag} (fixed)"
                else:
                    u_pos = u_flag or "—"
            trade_info = ""
            if a_trade:
                trade_info += f" | 🎯[A] {a_trade['side'].upper()} @ ${round(a_trade['entry'], 2)}"
            if u_trade:
                trade_info += f" | 🎯[U] {u_trade['side'].upper()} @ ${round(u_trade['entry'], 2)}"

            vol_info = ""
            if exchange:
                try:
                    bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=50)
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

@bot.command(name="scan")
async def scan_cmd(ctx, ticker: str = "BTC/USDT", tf: str = "1h"):
    ticker = ticker.upper()
    tf = tf.lower()

    if tf not in TIMEFRAMES:
        await ctx.send(f"❌ Unknown timeframe. Available: {', '.join(TIMEFRAMES)}")
        return

    await ctx.send(f"🔍 Scanning `{ticker}` `{tf}`…")

    exchange = _exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    if ticker not in _state_locks:
        _state_locks[ticker] = {}
    if tf not in _state_locks.get(ticker, {}):
        _state_locks[ticker][tf] = asyncio.Lock()

    async with _state_locks[ticker][tf]:
        temp_state = make_state()
        try:
            signals, bar_time, regime, lev = await check_signals(exchange, ticker, tf, temp_state, dry_run=True)
        except Exception as e:
            logger.error(f"Manual scan error: {e}", exc_info=True)
            await ctx.send(f"❌ Scan error: {e}")
            return

    bars = await safe_fetch_ohlcv(exchange, ticker, tf, limit=100)
    df = parse_ohlcv(bars) if bars else None

    if signals:
        for sig_type, price, reg, leverage, bt, conf, sl, tp, risk, stats, tp_desc in signals:
            embed = build_embed(ticker, tf, sig_type, price, reg, leverage, conf, sl, tp, risk, stats, tp_desc, df)
            await ctx.send(embed=embed)
    else:
        vol_info = ""
        try:
            vol_data = volume_flow_signal_v3(df)
            vol_flow = vol_data["flow"]
            vol_emoji = "🟢" if vol_flow == "inflow" else "🔴" if vol_flow == "outflow" else "⚪"
            vol_info = f" | {vol_emoji} Vol:{_flow_label(vol_flow)} RV:{vol_data['rel_vol']:.1f}x"
        except Exception as e:
            logger.debug(f"Volume info error in build_embed: {e}")
        await ctx.send(f"⏳ No signals for `{ticker}` `{tf}`. Regime: **{regime}**{vol_info}")

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

    exchange = _exchange_ref
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

    async with _tickers_lock:
        TICKERS.append(ticker)
        state[ticker] = {tf: make_state() for tf in TIMEFRAMES}

    if ticker not in _state_locks:
        _state_locks[ticker] = {}
    for tf in TIMEFRAMES:
        if tf not in _state_locks[ticker]:
            _state_locks[ticker][tf] = asyncio.Lock()

    save_tickers(TICKERS)

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

    async with _tickers_lock:
        TICKERS.remove(ticker)
        if ticker in state:
            del state[ticker]
        if ticker in _state_locks:
            del _state_locks[ticker]

    save_tickers(TICKERS)

    await ctx.send(f"🗑️ `{ticker}` removed. Remaining: {' | '.join(TICKERS)}")

@bot.command(name="mode")
async def mode_cmd(ctx, new_mode: str = ""):
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

    _cfg.MARKET_MODE = new_mode
    _cfg.save_mode(_cfg.MARKET_MODE)

    ex_config = {"enableRateLimit": True}
    if GATE_API_KEY and GATE_SECRET:
        ex_config["apiKey"] = GATE_API_KEY
        ex_config["secret"] = GATE_SECRET
    if _cfg.MARKET_MODE == "futures":
        ex_config["options"] = {"defaultType": "swap"}
    
    exchange = ccxt.gate(ex_config)
    global _exchange_ref
    _exchange_ref = exchange

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
            async with _state_locks[ticker][tf]:
                state[ticker][tf] = st

    label = "🔵 Spot (Gate.io Spot)" if _cfg.MARKET_MODE == "spot" else "🟠 Futures (Gate.io Perpetual)"
    await ctx.send(f"✅ Switched to **{label}**\n⚠️ Position states have been reset.")

@bot.command(name="utha")
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

@bot.command(name="htf")
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

    exchange = _exchange_ref
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
                async with _state_locks[ticker][tf]:
                    state[ticker][tf] = st

    await ctx.send(f"🧬 HTF Bias changed: **{old_htf.upper()}** → **{_cfg.HTF_BIAS.upper()}**\n"
                   f"⚠️ Position states have been reset.")

@bot.command(name="tpconfig")
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

@bot.command(name="chop")
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
            await ctx.send(f"✅ CHOP threshold for `{tf}` changed: **{old}** → **{new_val}**")
        except ValueError:
            await ctx.send("❌ Invalid number")
    except Exception as e:
        logger.error(f"Chop command error: {e}", exc_info=True)
        await ctx.send(f"❌ Error: {e}")

@bot.command(name="history")
async def history_cmd(ctx, ticker: str = "", tf: str = ""):
    try:
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
        while msg:
            chunk = msg[:1900]
            if len(msg) > 1900:
                chunk = chunk[:chunk.rfind("\n")] if "\n" in chunk else chunk
            await ctx.send(chunk)
            msg = msg[len(chunk):].lstrip("\n")
    except Exception as e:
        logger.error(f"History command error: {e}", exc_info=True)
        await ctx.send(f"❌ Error: {e}")

@bot.command(name="signals")
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

    exchange = _exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    if ticker not in _state_locks:
        _state_locks[ticker] = {}
    if tf not in _state_locks.get(ticker, {}):
        _state_locks[ticker][tf] = asyncio.Lock()

    async with _state_locks[ticker][tf]:
        try:
            bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=100)
            df = parse_ohlcv(bars)
            if not validate_dataframe(df, 50):
                await ctx.send("❌ Not enough data")
                return

            try:
                ticker_data = await asyncio.to_thread(exchange.fetch_ticker, ticker)
                last_close = float(
                    ticker_data.get("last") or
                    ticker_data.get("close") or
                    ticker_data.get("bid") or
                    df["close"].iloc[-1]
                )
            except Exception:
                last_close = float(df["close"].iloc[-1])

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

    exchange = _exchange_ref
    lines.append("\n**📊 Volume Overview:**")
    if exchange is None:
        lines.append("• Exchange not initialized")
    else:
        for ticker in list(TICKERS):
            for tf in TIMEFRAMES:
                try:
                    bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=50)
                    if bars and len(bars) >= 25:
                        df_d = parse_ohlcv(bars)
                        vol_data = volume_flow_signal_v3(df_d)
                        vol_flow = vol_data["flow"]
                        vol_emoji = "🟢" if vol_flow == "inflow" else "🔴" if vol_flow == "outflow" else "⚪"
                        lines.append(f"• `{ticker}` `{tf}`: {vol_emoji} {_flow_label(vol_flow)} RV:{vol_data['rel_vol']:.1f}x")
                except Exception:
                    pass

    await ctx.send("\n".join(lines))

@bot.command(name="reset")
async def reset_cmd(ctx, confirm: str = ""):
    if confirm.lower() != "yes":
        await ctx.send(
            "⚠️ This will DELETE all signal history and trade data!\n"
            "To confirm, type: `!reset yes`"
        )
        return

    scanner_was_running = market_scanner.is_running()
    if scanner_was_running:
        market_scanner.stop()
        await asyncio.sleep(1)

    if os.path.exists(SIGNALS_HISTORY_FILE):
        os.remove(SIGNALS_HISTORY_FILE)
    clear_history_cache()

    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            state[ticker][tf] = make_state()

    global _onchain_bias_cache, _onchain_last_fetch
    _onchain_bias_cache = None
    _onchain_last_fetch = 0.0

    await ctx.send("🗑️ History cleared. Running fresh backtest…")

    exchange = _exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        if scanner_was_running:
            market_scanner.start()
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

    if scanner_was_running and not market_scanner.is_running():
        market_scanner.start()

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

    exchange = _exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    if ticker not in _state_locks:
        _state_locks[ticker] = {}
    if tf not in _state_locks.get(ticker, {}):
        _state_locks[ticker][tf] = asyncio.Lock()

    async with _state_locks[ticker][tf]:
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

            update_signal_mae_mfe(ticker, tf, side, tp)
            update_signal_mae_mfe(ticker, tf, side, sl)

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

    exchange = _exchange_ref
    if exchange is None:
        await ctx.send("❌ Exchange not initialized")
        return

    if ticker not in _state_locks:
        _state_locks[ticker] = {}
    if tf not in _state_locks.get(ticker, {}):
        _state_locks[ticker][tf] = asyncio.Lock()

    async with _state_locks[ticker][tf]:
        try:
            bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=100)
            df = parse_ohlcv(bars)

            if not validate_dataframe(df, 50):
                await ctx.send("❌ Not enough data from exchange")
                return

            last_close = float(df["close"].iloc[-2])

            atr14 = calculate_atr(df, ATR_PERIOD)
            fs, fu, fl, fdir = calculate_frama(df, FRAMA_LEN, FRAMA_MULT)
            idx = len(df) - 2

            sl = calculate_sl(last_close, side, fs, fu, fl, atr14, idx)
            tp, tp_desc = calculate_combined_tp(ticker, tf, side, last_close, sl, df, idx, atr14)
            risk = abs(last_close - sl)

            if ticker not in state or tf not in state.get(ticker, {}):
                state.setdefault(ticker, {})[tf] = make_state()

            st = state[ticker][tf]
            track_key = "a_active_trade"
            st[track_key] = {"side": side, "entry": last_close, "sl": sl, "tp": tp, "lev": 3, "bar_opened": idx}
            st["a_bars_in_trade"] = 0
            st["a_in_long"] = (side == "long")
            st["a_in_short"] = (side == "short")
            st["active_trade"] = st[track_key]
            st["bars_in_trade"] = 0

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

@bot.command(name="onchain")
async def onchain_cmd(ctx):
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

        if bias.get("flow_data", {}).get("note") == "first_run":
            report = format_onchain_report(bias)
            await msg.edit(content=report + "\n\n⏳ _ETH flow дельта будет доступна через ~1 час (первый запуск)._")
            return

        global _onchain_bias_cache, _onchain_last_fetch
        _onchain_bias_cache = bias
        _onchain_last_fetch = time.time()
        report = format_onchain_report(bias)
        await msg.edit(content=report)
    except Exception as e:
        await msg.edit(content=f"❌ Ошибка on-chain: `{e}`")

@bot.command(name="reset_cache")
async def reset_cache_cmd(ctx):
    clear_htf_cache()
    clear_onchain_cache_full()
    global _onchain_bias_cache, _onchain_last_fetch
    _onchain_bias_cache = None
    _onchain_last_fetch = 0.0
    await ctx.send("✅ HTF bias cache и On-Chain cache сброшены. Следующий скан обновит данные.")

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