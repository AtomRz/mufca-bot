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
from utils import safe_fetch_ohlcv, parse_ohlcv, format_price
from signals import check_signals, backtest_history, make_state
from onchain import get_onchain_bias
import derivatives
import spread
from state import load_signals_history, load_bot_state, save_bot_state, reconcile_orphaned_signals

logger = logging.getLogger(__name__)

# 🆕 NOTE: the remaining imports (validate_dataframe, calculate_atr/frama,
# volume_*, calculate_sl/calculate_adaptive_sl, format_onchain_report,
# clear_*_cache, calculate_combined_tp, add/update_signal_*, get_signal_stats,
# save_tickers/save_tp_config/save_mode, MIN_TP_PCT/MAX_TP_PCT/MAX_HOLD_BARS/
# CHOP_THRESHOLD/PAIRS_FILE/ATR_PERIOD/FRAMA_LEN/FRAMA_MULT/
# SIGNALS_HISTORY_FILE) were only used inside @bot.command handlers and now
# live in discord_commands.py alongside the commands themselves.

from embeds import build_embed  # 🆕 moved to embeds.py (build_embed is used in market_scanner)

# =====================================================================
# 🤖  DISCORD BOT
# =====================================================================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# 🆕 FIX: state used to be built ONLY via make_state() — fresh, empty, on
# every process start. signals_history.json (the audit log) survived a
# restart, but the fact that "this position is still genuinely open, here's
# its TP/SL/tp1_hit" only lived in memory and was lost on every container
# restart (including a routine deploy of a new version) — active, not-yet-
# closed signals just disappeared from Status/the chart, while still hanging
# in signals_history.json with exit_type="open" forever. Now we restore the
# snapshot from disk; make_state() is only used for ticker/tf pairs that
# aren't in the snapshot (a new pair, or the very first run).
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
    """Fixes desynced track flags on startup."""
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            st = state[ticker][tf]
            # If the flag is set but there's no active_trade — clear the flag
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
_CLOSURE_NOTIFIED_MAX = 2000  # 🆕 FIX: the set used to grow unbounded (1 ID per closed trade, forever)

def _load_closure_notified() -> set:
    """Load set of already-notified trade IDs."""
    try:
        with open(_closure_notified_file, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def _save_closure_notified(notified: set):
    """Save notified trade IDs. Trimmed to the last _CLOSURE_NOTIFIED_MAX so the file doesn't grow forever."""
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
# 🚀  STARTUP
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
    """Startup: backtest first, then the scanner."""
    logger.info("=" * 60)
    logger.info("[STARTUP] Running historical backtest to populate signal history...")
    logger.info("=" * 60)

    # 🆕 FIX: backtest_history() doesn't check anything, it just APPENDS
    # signals on top of the existing history. startup_sequence used to run
    # the backtest unconditionally on EVERY container restart — and the
    # backtest is deterministic (same past candles → same signals), so every
    # restart duplicated the history (visible in !signals as pairs of
    # identical records). Now the backtest for a ticker/tf is skipped if its
    # history is already non-empty — we assume it was already accumulated
    # (by the first run or a backtest, or live trading). To force a rebuild
    # of the history — `!reset yes`.
    history = load_signals_history()
    total = 0
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            existing = history.get(ticker, {}).get(tf, {})
            already_populated = any(len(existing.get(side, [])) > 0 for side in ("long", "short"))
            if already_populated:
                logger.info(f"[STARTUP] {ticker} {tf} already has signal history — skipping backtest (use `!reset yes` to force re-run).")
                continue
            # 🆕 FIX: an exception here (e.g. a network glitch fetching
            # historical bars for ONE pair) used to abort the whole function —
            # and market_scanner.start() at the end was never called. The bot
            # stayed "alive" in Discord, but wasn't actually scanning
            # ANYTHING, with no error in the logs beyond a deferred, easily
            # missed asyncio warning. Now a single pair's failure doesn't sink
            # the backtest for the rest.
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

# On-chain bias cache (refreshed once an hour in market_scanner)
_onchain_cache_loaded = _cfg.load_onchain_bias_cache()
_onchain_bias_cache: Optional[Dict] = _onchain_cache_loaded.get("bias")
_onchain_last_fetch: float = _onchain_cache_loaded.get("last_fetch", 0.0)

@tasks.loop(seconds=_cfg.SCAN_INTERVAL_SECONDS)
async def market_scanner():
    """Main scanning loop."""
    # 🆕 FIX: a missing Discord channel (or Discord itself being disabled —
    # see _cfg.DISCORD_ENABLED) used to abort the WHOLE scan cycle via an
    # early return — meaning nothing worked at all without Discord, including
    # the web dashboard. Now channel is just Optional: if unavailable, only
    # the channel sends below are silently skipped, scanning proceeds as normal.
    channel = discord.utils.get(bot.get_all_channels(), name=CHANNEL_NAME) if _cfg.DISCORD_ENABLED else None
    if _cfg.DISCORD_ENABLED and channel is None:
        logger.warning(f"[WARN] Channel '{CHANNEL_NAME}' not found — scanning continues, Discord messages will be skipped this cycle.")

    exchange = _exchange_ref
    if exchange is None:
        return

    # 🆕 Refresh on-chain bias at an interval of _cfg.ONCHAIN_CACHE_TTL
    # (15m/30m/1h, configurable from the web UI — see set_onchain_interval())
    global _onchain_bias_cache, _onchain_last_fetch
    now_ts = time.time()
    if ONCHAIN_ENABLED and (now_ts - _onchain_last_fetch) >= _cfg.ONCHAIN_CACHE_TTL:
        try:
            _onchain_bias_cache = await get_onchain_bias()
            _onchain_last_fetch = now_ts
            # On the first run — clear the onchain_bias cache so the next
            # hourly cycle recomputes the real delta (the baseline is already
            # saved in _prev_balances)
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

    for ticker in list(TICKERS):  # copy the list — guards against mutation via !add/!remove
        # 🆕 Derivatives bias (funding rate + OI) — per-ticker, unlike
        # on-chain, which is one global reading shared across every pair.
        # get_derivatives_bias() does its own TTL caching internally
        # (config.DERIVATIVES_CACHE_TTL), so calling it once per ticker per
        # scan cycle is cheap on a cache hit — no separate module-level
        # cache needed here. combine_biases() merges it with the global
        # on-chain bias into one dict of the same shape everything
        # downstream already expects (see derivatives.py for why).
        derivatives_bias = None
        if _cfg.DERIVATIVES_ENABLED and _cfg.MARKET_MODE == "futures":
            try:
                derivatives_bias = await derivatives.get_derivatives_bias(exchange, ticker)
            except Exception as e:
                logger.warning(f"[DERIVATIVES] Failed for {ticker}: {e}")
        combined_bias = derivatives.combine_biases(_onchain_bias_cache, derivatives_bias)

        # 🆕 Order book spread — fetched every cycle regardless of
        # ENABLE_SPREAD_FILTER (see spread.py's module docstring): this is
        # the "warm-up" collection Atom asked for, so the rolling per-pair
        # median is already populated by the time the toggle gets switched
        # on, instead of starting cold. spread_ok gating itself only takes
        # effect inside check_signals() when the toggle is actually on.
        spread_snapshot = await spread.get_spread_snapshot(exchange, ticker)

        for tf in TIMEFRAMES:
            try:
                # 🆕 FIX: Use lock to prevent race with commands
                lock = _state_locks.get(ticker, {}).get(tf)
                if lock is None:
                    lock = asyncio.Lock()
                    if ticker not in _state_locks:
                        _state_locks[ticker] = {}
                    _state_locks[ticker][tf] = lock

                # 🆕 FIX: the lock used to be held for the whole cycle —
                # check_signals, fetching OHLCV for the chart, generating the
                # chart (another fetch), and all Discord messages. Because of
                # this, any command (!forcerun/!sim/!tp) on the SAME pair/TF
                # would block for several seconds until the scanner finished
                # all of its network/Discord work. Now only check_signals
                # stays under the lock (that's where positions actually open/
                # close and state gets mutated — that needs to be atomic
                # relative to commands). All network calls and Discord sends
                # are moved OUTSIDE the lock.
                async with lock:
                    st = state[ticker][tf]
                    signals, bar_time, regime, lev = await check_signals(
                        exchange, ticker, tf, st,
                        onchain_bias=combined_bias,
                        spread_info=spread_snapshot,
                    )

                    scan_stats["total_scans"] += 1
                    scan_stats["last_scan_time"] = datetime.now(timezone.utc).isoformat()

                    is_new_bar = bool(bar_time and bar_time != st["last_bar_time"])
                    if is_new_bar:
                        st["last_bar_time"] = bar_time

                # ── lock released — from here on it's only reads/notifications, no critical mutations ──

                # 🆕 FIX: save_bot_state() used to be called once for the
                # ENTIRE scan cycle (after all tickers/tfs, see the end of
                # market_scanner), even though opening/closing a position
                # mutates state right here, inside the lock above. If the
                # container restarted (docker rebuild/restart) in the window
                # between a trade opening in memory (already recorded in
                # signals_history.json via add_signal_record — that write is
                # synchronous) and the moment save_bot_state() ran at the end
                # of the cycle — on restart, active_trade was lost from
                # bot_state.json, EVEN with data/ mounted as a volume: the
                # file simply hadn't been written yet. reconcile_orphaned_
                # signals() would then honestly, but unfortunately, mark such
                # a record as "cancelled", even though the position was
                # really open. Now we save immediately as soon as signals is
                # non-empty (a position opened or closed this cycle) — without
                # waiting for the rest of the pairs.
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

                        # 🆕 FIX BUG-LO007: the Discord send, the WS broadcast,
                        # and push used to be in ONE try block — if
                        # channel.send() failed (not necessarily a
                        # discord.HTTPException, though that was the usual
                        # cause, but only that one type was even caught), WS
                        # and push were silently skipped too, even though
                        # they're independent notification channels. On a new
                        # pair (e.g. a freshly added SUI/USDT), a trade would
                        # genuinely open (state was already mutated in
                        # check_signals before this point), but neither the
                        # bot, nor the web UI, nor push reported it — the
                        # user only found out about the position by checking
                        # Status. Now every notification channel has its own
                        # try: one failing doesn't block the others.
                        chart_file = None
                        # 🆕 Skip the (expensive, matplotlib) chart render entirely when
                        # nothing will use it — only the Discord embed attaches it.
                        if _cfg.DISCORD_NOTIFICATIONS_ENABLED and channel is not None:
                            try:
                                from chart import generate_chart
                                is_long = "BUY" in sig_type or "LONG" in sig_type
                                # 🆕 FIX: this used to compute an absolute index
                                # into the df fetched HERE (limit=100), while
                                # generate_chart does its own separate fetch
                                # internally (limit≈300+) — the indices didn't
                                # line up, and the marker almost always ended
                                # up near the start of the chart. A signal is
                                # always formed on the last confirmed closed
                                # bar (iloc[-2]) per the bot's rules, which
                                # holds for a df of ANY length — use an offset
                                # from the end instead.
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

                        # 🆕 web UI: push the signal to all connected WS clients
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
                        # 🆕 Android push — same information as Discord/WS, but
                        # arrives even if the app is closed. send_push makes
                        # blocking HTTP requests (firebase-admin), so it's
                        # dispatched via to_thread, not directly in the event loop.
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

                # 🆕 FIX: Check ALL tracks for closures independently
                for track in _cfg.TRACKS:
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
                                    track_label = _cfg.TRACK_LABELS[track]
                                    # 🆕 FIX: st[notified_key] = True used to be
                                    # set unconditionally AFTER the if age < 35
                                    # block — if the bot only discovered the
                                    # closure 35+ seconds later (e.g. it was
                                    # unreachable/restarting right at the
                                    # moment of TP/SL), the Discord message
                                    # silently never got sent, but was still
                                    # marked "already notified" forever. The
                                    # trade genuinely closed, and the user
                                    # never found out. Now we always send it,
                                    # explicitly flagging the delay.
                                    late_note = "" if age < 35 else " ⏱️ *(delayed notification)*"
                                    # 🆕 FIX: same class of problem fixed for
                                    # TP1 — channel.send() here wasn't wrapped
                                    # in its own try/except, only the outer
                                    # except ValueError (which only catches
                                    # datetime.fromisoformat above, not Discord
                                    # exceptions). If the send failed (rate
                                    # limit, HTTPException), it fell through to
                                    # the scanner's general except and cut off
                                    # the rest of processing for this ticker/tf
                                    # this cycle — including the TP1/TP2/SL
                                    # live check below. Now a Discord-send
                                    # failure doesn't affect the rest, and
                                    # notified_ids/the flag are still set
                                    # either way (there's no point re-sending a
                                    # message about an old closure — as
                                    # before, this is a best-effort notification).
                                    if _cfg.DISCORD_NOTIFICATIONS_ENABLED and channel is not None:
                                        try:
                                            await channel.send(
                                                f"{emoji} **Trade Closed [{track_label}-track]** | `{ticker}` `{tf}` | "
                                                f"{last['side'].upper()} | Entry: ${format_price(last['entry'])} → Exit: ${format_price(last['exit'])} | "
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
                for track in _cfg.TRACKS:
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
                            # 🆕 FIX: TP2 used to only be checked via
                            # check_tp_sl_hit() against the CLOSED bar's
                            # high/low (anti-repainting convention) — if price
                            # touched TP2 within the forming bar and then
                            # reversed back toward SL before the bar closed,
                            # the bar would close with no trace of the TP2
                            # touch, and the position would be incorrectly
                            # closed as SL, even though on a real exchange the
                            # limit TP2 order would have filled at the moment
                            # of the touch. Check TP2 against the live ticker —
                            # symmetric to how it's already done for SL after
                            # TP1 just below.
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
                    # 🆕 FIX: the original SL (pre-TP1) used to only be
                    # checked via check_tp_sl_hit() against the CLOSED bar's
                    # low/high — asymmetric with TP1, which is already
                    # checked live just above in this same block. A candle
                    # could pierce SL intrabar and recover above it by close —
                    # the bot would only see this on the next scan after the
                    # bar closed, not at the moment of the touch. Symmetric to
                    # TP2/SL-after-TP1 (see the block above when tp1_hit=True) —
                    # now using the same current_price, with no extra exchange
                    # round trip.
                    sl = trade.get("sl")
                    sl_breached = sl is not None and (
                        (side == "long" and current_price <= sl) or
                        (side == "short" and current_price >= sl)
                    )
                    if tp1_reached:
                        trade["tp1_hit"] = True
                        # 🆕 FIX: SL after TP1 used to either not move at all,
                        # or be hardcoded to breakeven. Now the mode is read
                        # from _cfg.TP1_SL_MODE — "breakeven" (SL = entry) or
                        # "half_tp1" (SL = entry + halfway to TP1, tighter
                        # than breakeven, but every trigger locks in a small
                        # guaranteed profit instead of zero). check_tp_sl_hit()
                        # re-reads trade["sl"] on every check, so this applies
                        # itself automatically, with no further changes needed
                        # in signals.py.
                        entry = trade.get("entry", 0)
                        if _cfg.TP1_SL_MODE == "half_tp1":
                            if side == "long":
                                new_sl = entry + (tp1_price - entry) / 2
                            else:
                                new_sl = entry - (entry - tp1_price) / 2
                            sl_label = f"halfway to TP1 (${format_price(new_sl)})"
                        else:
                            new_sl = entry
                            sl_label = f"breakeven (${format_price(entry)})"
                        trade["sl"] = new_sl
                        # 🆕 FIX BUG-LO009: SL moved WITHIN the still-forming
                        # bar — the low/high of this and all preceding bars
                        # were printed BEFORE price ever touched TP1, and
                        # check_tp_sl_hit() (signals.py) no longer checks the
                        # new stop against them — only against bars that
                        # closed AFTER this moment. bar_time comes from the
                        # current scan's state (see check_signals), not from
                        # this ticker request.
                        # 🆕 FIX (Kimi review): originally this was set to
                        # exactly the last closed bar (X) — but the low/high
                        # of the CURRENT forming bar (Y), the one where TP1
                        # was actually touched, could also have been printed
                        # BEFORE the touch (the bar opened below the halfway
                        # point, then rallied to TP1) — the same class of
                        # false trigger, just in a narrower window. Bar Y is
                        # already covered by the live ticker check below
                        # (when tp1_hit=True), so it's safe to exclude it from
                        # the bar-based check too — push the threshold forward
                        # by +1 timeframe, so the bar-based check only starts
                        # applying from bar Z (the first one to close entirely
                        # AFTER the move).
                        try:
                            tf_ms = int(exchange.parse_timeframe(tf) * 1000)
                        except Exception:
                            tf_ms = 3600_000
                        trade["sl_moved_after_bar"] = (st.get("last_processed_bar_time") or 0) + tf_ms
                        track_label = _cfg.TRACK_LABELS[track]
                        # 🆕 FIX: this send used to not be wrapped in
                        # try/except at all — the only one of the three
                        # (Discord/WS/push) in this block. If channel.send()
                        # raised (rate limit, missing permissions,
                        # discord.HTTPException), it would abort the whole
                        # scanner iteration and silently eat BOTH the WS
                        # broadcast AND push for this same TP1 event — the
                        # same class of problem fixed in BUG-LO007, just not
                        # applied to this newer code. Now a Discord-send
                        # failure doesn't prevent the other notification
                        # channels from firing.
                        if _cfg.DISCORD_NOTIFICATIONS_ENABLED and channel is not None:
                            try:
                                await channel.send(
                                    f"🎯 **TP1 Hit [{track_label}-track]** | `{ticker}` `{tf}` | "
                                    f"{side.upper()} | Entry: ${format_price(entry)} → TP1: ${format_price(tp1_price)}\n"
                                    f"⚠️ **Close 50% of the position and move SL to {sl_label}**"
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
                                body=f"Close 50%, move SL to {sl_label}",
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

    # 🆕 web UI: scanner tick — the frontend can refresh its status panel
    try:
        from web_api import broadcast_event
        await broadcast_event({"type": "scan_tick", "scan_stats": scan_stats})
    except Exception as ws_err:
        logger.warning(f"[WS] broadcast failed: {ws_err}")

    # 🆕 Snapshot active positions to disk — once per scan cycle (not on
    # every event: cheap at this state size, but no need to write more
    # often). This is a safety net for a NON-graceful stop (docker stop -t 0,
    # OOM kill), when the SIGTERM handler in main.py doesn't get a chance to
    # fire — on a normal graceful restart the snapshot gets written there too
    # anyway, and the duplication is harmless.
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

# 🆕 FIX BUG-LO002: Guard flag against multiple on_ready calls
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
# 💬  COMMAND REGISTRATION
# =====================================================================
# 🆕 All @bot.command(...) handlers now live in discord_commands.py (used to
# be ~1600 lines in one file). This import is not "unused": discord_commands.py
# does `import bot as core` and registers commands via `@core.bot.command(...)`
# decorators, which fire as a side effect of THIS exact import. It must stay
# at the very bottom of the file: by this point everything discord_commands.py
# needs (bot, state, market_scanner, _exchange_ref, etc.) is already defined
# in bot.py, so the reverse `import bot as core` inside discord_commands.py
# safely reuses the already nearly-fully-initialized bot.py module instead of
# creating an infinite import recursion.
import discord_commands  # noqa: F401,E402

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in .env file!")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    bot.run(DISCORD_TOKEN)
