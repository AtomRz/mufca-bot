"""
MUFCA v4.0 — Web API
FastAPI backend for the web dashboard. Runs in the SAME process and asyncio
loop as the Discord bot (see main.py) — reuses bot.state, bot._exchange_ref,
and config.py as-is, no separate indicator recomputation by a second scanner.

Endpoints:
  GET  /api/status                      — summary across all pairs/TFs (like !status)
  GET  /api/pairs                       — list of tickers
  POST /api/pairs                       — add a ticker {"ticker": "DOGE/USDT"}
  DELETE /api/pairs/{ticker}            — remove a ticker
  GET  /api/chart?ticker=...&tf=...     — JSON for the chart (candles+FRAMA+BB+S/R+MFI)
  GET  /api/config                      — all editable settings as one object
  POST /api/config/mode                 — {"mode": "spot"|"futures"}
  POST /api/config/htf                  — {"htf": "4h"}
  POST /api/config/utha                 — {"enabled": true}
  POST /api/config/chop                 — {"tf": "1h", "value": 55.0}
  POST /api/config/tpconfig             — {"param": "mode"|"limit"|"percentile"|"safe", "value": ...}
  WS   /ws/live                         — broadcasts new bars/signals/trade closures
"""

import asyncio
import logging
import re
import secrets
import time
from collections import defaultdict
from typing import Optional, List, Set
from urllib.parse import unquote

import ccxt
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from starlette.responses import Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="MUFCA Web API")

# 🆕 Circular import, same pattern as discord_commands.py: bot.py imports
# web_api at the very bottom, once bot/state/config are already set up.
import bot as core
import config as _cfg
from config import TIMEFRAMES, CHOP_THRESHOLD, save_mode, save_htf, save_tp_config, save_filter_toggles, save_tp1_sl_mode, save_discord_notifications_enabled
from signals import make_state, clear_htf_cache
from chart_data import get_chart_data, get_market_pulse
from state import load_signals_history, save_signals_history
import push as _push


# =====================================================================
# 🔒  HTTP BASIC AUTH — protects both /api/* and the frontend's static assets.
# If WEB_USERNAME/WEB_PASSWORD aren't set in .env, the dashboard stays open
# (for local development), but we loudly warn in the logs on every startup so
# this can't go unnoticed.
# =====================================================================
if not _cfg.WEB_USERNAME or not _cfg.WEB_PASSWORD:
    logger.warning(
        "[AUTH] ⚠️  WEB_USERNAME/WEB_PASSWORD not set in .env — the web dashboard "
        "IS OPEN WITHOUT AUTHENTICATION. Anyone with access to port 8585 can "
        "change the bot's settings. Set both values in .env to enable protection."
    )


# =====================================================================
# 🚫  RATE LIMITING — locks out an IP after a run of failed login attempts.
# Without this, Basic Auth exposed to the internet can be brute-forced an
# unlimited number of times. In-memory (resets on container restart) — for a
# personal dashboard that's enough; a distributed brute-force isn't a
# realistic scenario here.
# =====================================================================
_LOCKOUT_THRESHOLD = 5             # this many failed attempts in a row...
_LOCKOUT_WINDOW_SECONDS = 300      # ...within this window...
_LOCKOUT_DURATION_SECONDS = 900    # ...triggers a 15-minute lockout

_failed_attempts: dict = defaultdict(list)
_locked_until: dict = {}


def _get_client_ip(scope) -> str:
    """The client's real IP behind a Cloudflare Tunnel/reverse proxy — not
    scope['client'], which would show the tunnel/nginx's own address, not the
    visitor's."""
    headers = dict(scope.get("headers") or [])
    for header_name in (b"cf-connecting-ip", b"x-forwarded-for"):
        value = headers.get(header_name)
        if value:
            return value.decode("utf-8", errors="ignore").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


def _is_locked_out(ip: str) -> bool:
    until = _locked_until.get(ip)
    return until is not None and time.time() < until


def _record_failed_attempt(ip: str):
    now = time.time()
    attempts = [t for t in _failed_attempts[ip] if now - t < _LOCKOUT_WINDOW_SECONDS]
    attempts.append(now)
    _failed_attempts[ip] = attempts
    if len(attempts) >= _LOCKOUT_THRESHOLD:
        _locked_until[ip] = now + _LOCKOUT_DURATION_SECONDS
        logger.warning(
            f"[AUTH] IP {ip} locked out for {_LOCKOUT_DURATION_SECONDS}s "
            f"after {len(attempts)} failed login attempts"
        )


def _record_success(ip: str):
    _failed_attempts.pop(ip, None)
    _locked_until.pop(ip, None)


# =====================================================================
# 🎫  WS TICKETS — short-lived, single-use tokens for the WebSocket handshake.
# The browser WebSocket API can't set custom headers, so something has to go
# in a URL query parameter instead — and anything in a query parameter ends
# up in access logs (uvicorn/nginx/Cloudflare) in plain text. This used to be
# the raw base64(login:password) — a permanent, real password. Now it's a
# ticket: lives 30 seconds, single-use, worthless after half a minute, and
# reveals nothing about the real credentials even if a greedy CI/CD system or
# Cloudflare keeps it in logs forever.
# =====================================================================
_WS_TICKET_TTL_SECONDS = 30
_ws_tickets: dict = {}


def _issue_ws_ticket() -> str:
    ticket = secrets.token_urlsafe(32)
    _ws_tickets[ticket] = time.time() + _WS_TICKET_TTL_SECONDS
    return ticket


def _consume_ws_ticket(ticket: str) -> bool:
    expiry = _ws_tickets.pop(ticket, None)  # pop — single-use, won't work a second time
    return expiry is not None and time.time() < expiry


class BasicAuthASGIMiddleware:
    """A pure ASGI middleware (not BaseHTTPMiddleware!) — that one can't see
    the websocket scope, and /ws/live also needs to require auth, otherwise
    the signal feed stays open even when the rest of the API is protected."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _check_basic_value(raw_b64: str) -> bool:
        import base64
        try:
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            user, _, pwd = decoded.partition(":")
        except Exception:
            return False
        # constant-time comparison — don't let response timing reveal correctness
        return (
            secrets.compare_digest(user, _cfg.WEB_USERNAME)
            and secrets.compare_digest(pwd, _cfg.WEB_PASSWORD)
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        # Only protect /api/* and /ws/* — serve the frontend's static assets
        # (index.html, JS/CSS bundle) freely, otherwise the browser shows ITS
        # OWN native Basic Auth popup over our custom login screen before
        # React even gets a chance to render. /api/health is also exempt —
        # it's the liveness probe for the Docker HEALTHCHECK, which has no
        # (and shouldn't have) credentials, and it returns nothing sensitive.
        path = scope.get("path", "")
        if not (path.startswith("/api/") or path.startswith("/ws/")) or path == "/api/health":
            return await self.app(scope, receive, send)

        if not _cfg.WEB_USERNAME or not _cfg.WEB_PASSWORD:
            return await self.app(scope, receive, send)  # auth disabled — no credentials set

        ip = _get_client_ip(scope)
        if _is_locked_out(ip):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4429})
                return
            response = Response(content="Too many failed attempts, try again later", status_code=429)
            await response(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")

        authorized = False
        if auth_header.startswith("Basic "):
            authorized = self._check_basic_value(auth_header[6:])

        if not authorized and scope["type"] == "websocket":
            from urllib.parse import parse_qs
            qs = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="ignore"))
            ticket = (qs.get("ticket") or [None])[0]
            if ticket and _consume_ws_ticket(ticket):
                authorized = True

        if authorized:
            _record_success(ip)
            return await self.app(scope, receive, send)

        _record_failed_attempt(ip)

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return

        response = Response(
            content="Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MUFCA"'},
        )
        await response(scope, receive, send)


app.add_middleware(BasicAuthASGIMiddleware)

# =====================================================================
# 📡  WEBSOCKET BROADCAST
# =====================================================================
_ws_clients: Set[WebSocket] = set()


async def broadcast_event(event: dict):
    """Broadcasts an event to all connected clients. Called from bot.py.

    Each send is bounded by a timeout — a slow/stuck client shouldn't be able
    to stall delivery to every other connected client."""
    dead = []
    for ws in _ws_clients:
        try:
            await asyncio.wait_for(ws.send_json(event), timeout=2.0)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            # The client doesn't send anything — just keep the connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


# =====================================================================
# 📊  STATUS / PAIRS
# =====================================================================
@app.post("/api/ws-ticket")
async def issue_ws_ticket():
    """Short-lived (30 sec) single-use ticket for the WS handshake — see the
    comment on _issue_ws_ticket() above. The call itself is protected by the
    same Basic Auth middleware as the rest of /api/*, so only someone who
    already passed normal header-based auth can get a ticket."""
    return {"ticket": _issue_ws_ticket()}


@app.get("/api/onchain")
async def get_onchain():
    """On-chain bias snapshot (ETH exchange flow, Fear & Greed, BTC dominance,
    resulting TP/SL/leverage multipliers) — same data the Discord embeds show,
    now also available to the web dashboard. Reads bot.py's in-memory cache
    (refreshed hourly, persisted to disk — see config.load_onchain_bias_cache),
    doesn't trigger a fetch itself."""
    if not core.ONCHAIN_ENABLED:
        return {"enabled": False, "bias": None, "last_fetch": None}
    return {
        "enabled": True,
        "bias": core._onchain_bias_cache,
        "last_fetch": core._onchain_last_fetch or None,
    }


@app.get("/api/health")
async def health():
    """Lightweight liveness/readiness probe for Docker/orchestrator healthchecks —
    intentionally has no auth and does no heavy work (no exchange calls, no file I/O)."""
    return {
        "status": "ok",
        "exchange_connected": core._exchange_ref is not None,
        "scanner_running": core.market_scanner.is_running(),
        "last_scan": core.scan_stats.get("last_scan_time"),
    }


@app.get("/api/status")
async def get_status():
    result = {
        "scan_stats": core.scan_stats,
        "market_mode": _cfg.MARKET_MODE,
        "htf_bias": _cfg.HTF_BIAS,
        "pairs": {},
    }
    for ticker in _cfg.TICKERS:
        result["pairs"][ticker] = {}
        for tf in TIMEFRAMES:
            st = core.state.get(ticker, {}).get(tf, {})
            result["pairs"][ticker][tf] = {
                "a_active_trade": st.get("a_active_trade"),
                "u_active_trade": st.get("u_active_trade"),
                "last_bar_time": st.get("last_bar_time"),
            }
    return result


# =====================================================================
# 📱  ANDROID PUSH — device registration (FCM tokens)
# =====================================================================
class DeviceRegisterIn(BaseModel):
    token: str
    device_name: Optional[str] = None


@app.post("/api/devices/register")
async def register_device(body: DeviceRegisterIn):
    if not body.token or len(body.token) < 20:
        raise HTTPException(400, "Invalid FCM token")
    info = _push.register_device(body.token, body.device_name)
    return {"registered": True, **info}


@app.delete("/api/devices/{token}")
async def unregister_device(token: str):
    ok = _push.unregister_device(unquote(token))
    if not ok:
        raise HTTPException(404, "Device not found")
    return {"unregistered": True}


@app.get("/api/devices")
async def list_devices():
    return {"devices": _cfg.load_devices()}


@app.post("/api/devices/test-push")
async def test_push():
    """Sends a test push to all registered devices — to verify the whole
    pipeline (Firebase credentials on the server → FCM → device) without
    waiting for a real signal. If firebase-credentials.json isn't configured
    on the server, returns skipped='firebase_not_configured' rather than a
    silent "success"."""
    result = await asyncio.to_thread(
        _push.send_push,
        title="MUFCA test push",
        body="If you see this — the full pipeline works: backend → Firebase → your phone.",
        data={"type": "test"},
    )
    return result


@app.get("/api/pairs")
async def get_pairs():
    return {"tickers": _cfg.TICKERS}


class PairIn(BaseModel):
    ticker: str


@app.post("/api/pairs")
async def add_pair(body: PairIn):
    ticker = body.ticker.upper().strip()
    if "/" not in ticker:
        raise HTTPException(400, "Ticker format: BASE/QUOTE, e.g. DOGE/USDT")
    if ticker in _cfg.TICKERS:
        raise HTTPException(409, f"{ticker} is already tracked")

    exchange = core._exchange_ref
    if exchange is None:
        raise HTTPException(503, "The bot hasn't connected to the exchange yet, try again in a few seconds")

    _cfg.TICKERS.append(ticker)
    _cfg.save_tickers(_cfg.TICKERS)
    core.state[ticker] = {tf: make_state() for tf in TIMEFRAMES}
    async with core._tickers_lock:
        core._ensure_locks()

    # 🆕 FIX: a pair added via the web used to be left with an empty history —
    # unlike Discord's !add, which immediately runs backtest_history. An
    # empty history means calculate_combined_tp runs in fallback mode (fixed
    # R:R) until real signals accumulate, which is noticeably worse than
    # adaptive TP/SL.
    from signals import backtest_history
    total = 0
    for tf in TIMEFRAMES:
        try:
            count = await asyncio.to_thread(backtest_history, exchange, ticker, tf, 3000)
            total += count
        except Exception as e:
            logger.error(f"[API] Backtest failed for {ticker} {tf}: {e}", exc_info=True)

    return {"tickers": _cfg.TICKERS, "backtest_signals": total}


@app.delete("/api/pairs/{ticker:path}")
async def remove_pair(ticker: str, purge_history: bool = False):
    """purge_history=true — equivalent to Discord `!delsignals {ticker} yes`
    (all timeframes), executed immediately with no preview/confirmation,
    since this is already a deliberate user action from the web UI (the
    frontend handles the checkbox/confirm before calling this). Default
    (false) — the same behavior as the Discord `!remove` command:
    signals_history.json is left untouched, to preserve the accumulated
    adaptive TP/SL statistics in case the pair comes back."""
    ticker = unquote(ticker).upper().strip()
    if ticker not in _cfg.TICKERS:
        raise HTTPException(404, f"{ticker} is not tracked")
    _cfg.TICKERS.remove(ticker)
    _cfg.save_tickers(_cfg.TICKERS)
    # 🆕 FIX: the ticker used to be removed from _cfg.TICKERS but left in
    # core.state and core._state_locks — doesn't crash (the scanner iterates
    # over TICKERS, not over state), but the orphaned entry hangs in memory
    # forever and bloats bot_state_snapshot.json. Clean it up right away.
    core.state.pop(ticker, None)
    core._state_locks.pop(ticker, None)

    purged = 0
    if purge_history:
        history = load_signals_history()
        if ticker in history:
            purged = sum(
                len(v.get("long", [])) + len(v.get("short", []))
                for v in history[ticker].values()
            )
            del history[ticker]
            save_signals_history(history)

    return {"tickers": _cfg.TICKERS, "purged_signals": purged}


# =====================================================================
# 📈  CHART DATA
# =====================================================================
@app.get("/api/pulse")
async def pulse(ticker: Optional[str] = None, tf: str = "1h"):
    """Summary for the top bar: CHOP/trend/suggested leverage for one
    reference pair (defaults to the first tracked pair). Not tied to the
    selection on the Chart tab."""
    ticker = unquote(ticker).upper().strip() if ticker else (_cfg.TICKERS[0] if _cfg.TICKERS else None)
    if not ticker:
        raise HTTPException(404, "No tracked pairs")
    if ticker not in _cfg.TICKERS:
        raise HTTPException(404, f"{ticker} is not tracked")
    if tf not in TIMEFRAMES:
        raise HTTPException(400, f"tf must be one of {TIMEFRAMES}")

    exchange = core._exchange_ref
    if exchange is None:
        raise HTTPException(503, "The bot hasn't connected to the exchange yet, try again in a few seconds")

    try:
        return await get_market_pulse(exchange, ticker, tf, onchain_bias=core._onchain_bias_cache)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/chart")
async def chart(ticker: str, tf: str, limit: int = 200, track: str = "a"):
    ticker = unquote(ticker).upper().strip()
    if ticker not in _cfg.TICKERS:
        raise HTTPException(404, f"{ticker} is not tracked")
    if tf not in TIMEFRAMES:
        raise HTTPException(400, f"tf must be one of {TIMEFRAMES}")
    if not (20 <= limit <= 1000):
        raise HTTPException(400, "limit must be between 20 and 1000")

    exchange = core._exchange_ref
    if exchange is None:
        raise HTTPException(503, "The bot hasn't connected to the exchange yet, try again in a few seconds")

    st = core.state.get(ticker, {}).get(tf, {})
    trade_key = "a_active_trade" if track == "a" else "u_active_trade"
    active_trade = st.get(trade_key)

    try:
        data = await get_chart_data(exchange, ticker, tf, limit=limit, state_snapshot=active_trade)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return data


# =====================================================================
# 📚  SIGNAL HISTORY / WIN RATE
# Same data as Discord's !signals, but as JSON, with a per-track (A/U)
# breakdown that !signals doesn't do (there a+u+sim are all mixed together).
# =====================================================================
def _aggregate_records(records: list) -> Optional[dict]:
    """Aggregates closed signals: win rate, average MFE/MAE/PnL, TP/SL/cancelled
    breakdown. Synthetic (!sim) records are excluded — same as in
    get_signal_stats/calculate_adaptive_tp, they don't reflect real market behavior.
    🆕 FIX BUG-LO008: sl_after_tp1 (TP1 gave profit, the remainder closed at
    the moved SL) is a real closed outcome and is included in the sample and
    in win_rate (win_rate is already computed from the sign of moved_pct, not
    from exit_type, so it was never actually dropped from the sample here);
    shown as a separate counter instead of being lumped in with plain "sl"."""
    closed = [r for r in records if r.get("exit_type") in ("tp", "sl", "sl_after_tp1", "cancelled") and not r.get("synthetic", False)]
    if not closed:
        return None
    wins = sum(1 for r in closed if r.get("moved_pct", 0) > 0)
    tp_hits = sum(1 for r in closed if r["exit_type"] == "tp")
    sl_hits = sum(1 for r in closed if r["exit_type"] == "sl")
    sl_after_tp1_hits = sum(1 for r in closed if r["exit_type"] == "sl_after_tp1")
    cancelled = sum(1 for r in closed if r["exit_type"] == "cancelled")
    mfes = [r.get("max_favorable_pct", 0) for r in closed]
    maes = [r.get("max_adverse_pct", 0) for r in closed]
    pnls = [r.get("moved_pct", 0) for r in closed]
    n = len(closed)
    return {
        "count": n,
        "wins": wins,
        "win_rate": round(wins / n, 3),
        "avg_pnl": round(sum(pnls) / n, 2),
        "avg_mfe": round(sum(mfes) / n, 2),
        "avg_mae": round(sum(maes) / n, 2),
        "tp_hits": tp_hits,
        "sl_hits": sl_hits,
        "sl_after_tp1_hits": sl_after_tp1_hits,
        "cancelled": cancelled,
    }


@app.get("/api/history/summary")
async def history_summary():
    """Win rate/statistics for every ticker/tf/side/track combination that has
    closed signals, plus a grand-total row across all of them."""
    history = load_signals_history()
    rows = []
    all_closed_for_total: list = []

    for ticker, tfs in history.items():
        for tf, sides in tfs.items():
            for side in ("long", "short"):
                records = sides.get(side, [])
                by_track: dict = {}
                for r in records:
                    by_track.setdefault(r.get("track", "a"), []).append(r)
                for track, recs in by_track.items():
                    agg = _aggregate_records(recs)
                    if agg:
                        rows.append({"ticker": ticker, "tf": tf, "side": side, "track": track, **agg})
                        all_closed_for_total.extend(
                            r for r in recs
                            if r.get("exit_type") in ("tp", "sl", "sl_after_tp1", "cancelled") and not r.get("synthetic", False)
                        )

    rows.sort(key=lambda r: (r["ticker"], r["tf"], r["side"], r["track"]))
    total = _aggregate_records(all_closed_for_total)
    return {"rows": rows, "total": total}


@app.get("/api/history/records")
async def history_records(ticker: str, tf: str, side: str, track: str = "a", limit: int = 30):
    ticker = unquote(ticker).upper().strip()
    side = side.lower()
    if side not in ("long", "short"):
        raise HTTPException(400, "side must be 'long' or 'short'")

    history = load_signals_history()
    records = history.get(ticker, {}).get(tf, {}).get(side, [])
    closed = [r for r in records if r.get("track") == track and r.get("exit_type") != "open"]
    closed = list(reversed(closed[-limit:]))  # newest first
    return {"ticker": ticker, "tf": tf, "side": side, "track": track, "records": closed}


# =====================================================================
# ⚙️  CONFIG — get all settings + set one at a time, logic 1-to-1 with the
# Discord commands.
# =====================================================================
@app.get("/api/config")
async def get_config():
    return {
        "mode": _cfg.MARKET_MODE,
        "htf_bias": _cfg.HTF_BIAS,
        "ut_heikin_ashi": _cfg.UT_HEIKIN_ASHI,
        "tp1_sl_mode": _cfg.TP1_SL_MODE,
        "discord_notifications_enabled": _cfg.DISCORD_NOTIFICATIONS_ENABLED,
        "scan_interval_seconds": _cfg.SCAN_INTERVAL_SECONDS,
        "scan_interval_options": list(_cfg.SCAN_INTERVAL_OPTIONS),
        "onchain_interval_seconds": _cfg.ONCHAIN_CACHE_TTL,
        "onchain_interval_options": list(_cfg.ONCHAIN_INTERVAL_OPTIONS),
        "chop_threshold": CHOP_THRESHOLD,
        "filter_toggles": {
            "frama": _cfg.ENABLE_FRAMA_FILTER,
            "chop": _cfg.ENABLE_CHOP_FILTER,
            "atr": _cfg.ENABLE_ATR_FILTER,
            "htf": _cfg.ENABLE_MTF_BIAS,
            "fake_break": _cfg.ENABLE_FAKE_BREAK_FILTER,
            "liq_sweep": _cfg.ENABLE_LIQ_SWEEP_FILTER,
        },
        "tp_config": {
            "use_safe_tp": _cfg.USE_SAFE_TP,
            "tp_percentile": _cfg.TP_PERCENTILE,
            "safe_tp_percentile": _cfg.SAFE_TP_PERCENTILE,
            "signal_history_limit": _cfg.SIGNAL_HISTORY_LIMIT,
            "min_tp_pct": _cfg.MIN_TP_PCT,
            "max_tp_pct": _cfg.MAX_TP_PCT,
            "max_hold_bars": _cfg.MAX_HOLD_BARS,
        },
        "indicators": {
            "frama_len": _cfg.FRAMA_LEN,
            "frama_mult": _cfg.FRAMA_MULT,
            "mfi_len": _cfg.MFI_LEN,
            "mfi_training": _cfg.MFI_TRAINING,
            "and_len": _cfg.AND_LEN,
            "and_sig_len": _cfg.AND_SIG_LEN,
            "lookback": _cfg.LOOKBACK,
            "ut_sensitivity": _cfg.UT_SENSITIVITY,
            "ut_period": _cfg.UT_PERIOD,
            "bb_period": _cfg.BB_PERIOD,
            "bb_stddev": _cfg.BB_STDDEV,
            "sr_pivot_window": _cfg.SR_PIVOT_WINDOW,
            "sr_max_levels": _cfg.SR_MAX_LEVELS,
        },
        "volume_profile": {
            "enabled": _cfg.VP_ENABLED,
            "bins": _cfg.VP_BINS,
            "lookback": _cfg.VP_LOOKBACK,
            "value_area_pct": _cfg.VP_VALUE_AREA_PCT,
            "show_histogram": _cfg.VP_SHOW_HISTOGRAM,
        },
        "colors": _cfg.CHART_COLORS,
        "timeframes": TIMEFRAMES,
        "pairs": _cfg.TICKERS,
    }


async def _reset_states_after_regime_change():
    """Same as what mode_cmd/htf_cmd do after a regime change — otherwise
    state would end up desynced."""
    exchange = core._exchange_ref
    for ticker in _cfg.TICKERS:
        for tf in TIMEFRAMES:
            new_st = make_state()
            try:
                if exchange:
                    bars = await asyncio.to_thread(exchange.fetch_ohlcv, ticker, tf, limit=3)
                    if bars and len(bars) >= 2:
                        new_st["last_bar_time"] = int(bars[-2][0])
                        new_st["last_processed_bar_time"] = int(bars[-2][0])
            except Exception:
                pass
            async with core._state_locks[ticker][tf]:
                core.state[ticker][tf] = new_st


class ModeIn(BaseModel):
    mode: str


@app.post("/api/config/mode")
async def set_mode(body: ModeIn):
    new_mode = body.mode.lower()
    if new_mode not in ("spot", "futures"):
        raise HTTPException(400, "mode must be 'spot' or 'futures'")
    if new_mode == _cfg.MARKET_MODE:
        return {"mode": _cfg.MARKET_MODE, "changed": False}

    _cfg.MARKET_MODE = new_mode
    save_mode(_cfg.MARKET_MODE)

    if _cfg.MARKET_MODE == "futures":
        core._exchange_ref = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    else:
        core._exchange_ref = ccxt.gate({"enableRateLimit": True})

    await _reset_states_after_regime_change()
    await broadcast_event({"type": "config_changed", "key": "mode", "value": _cfg.MARKET_MODE})
    return {"mode": _cfg.MARKET_MODE, "changed": True}


class HtfIn(BaseModel):
    htf: str


@app.post("/api/config/htf")
async def set_htf(body: HtfIn):
    valid_htfs = ("1d", "4h", "2h", "6h", "12h", "1w", "3d")
    new_htf = body.htf.lower()
    if new_htf not in valid_htfs:
        raise HTTPException(400, f"htf must be one of {valid_htfs}")
    if new_htf == _cfg.HTF_BIAS:
        return {"htf_bias": _cfg.HTF_BIAS, "changed": False}

    _cfg.HTF_BIAS = new_htf
    save_htf(_cfg.HTF_BIAS)
    clear_htf_cache()
    await _reset_states_after_regime_change()
    await broadcast_event({"type": "config_changed", "key": "htf", "value": _cfg.HTF_BIAS})
    return {"htf_bias": _cfg.HTF_BIAS, "changed": True}


class Tp1SlModeIn(BaseModel):
    tp1_sl_mode: str


@app.post("/api/config/tp1-sl-mode")
async def set_tp1_sl_mode(body: Tp1SlModeIn):
    """SL-move mode after TP1: 'breakeven' (SL = entry) or 'half_tp1' (SL =
    entry + halfway to TP1, tighter than breakeven). Applied immediately, no
    container restart needed — bot.py reads _cfg.TP1_SL_MODE on every TP1 hit."""
    new_mode = body.tp1_sl_mode.lower()
    if new_mode not in ("breakeven", "half_tp1"):
        raise HTTPException(400, "tp1_sl_mode must be 'breakeven' or 'half_tp1'")
    if new_mode == _cfg.TP1_SL_MODE:
        return {"tp1_sl_mode": _cfg.TP1_SL_MODE, "changed": False}

    _cfg.TP1_SL_MODE = new_mode
    save_tp1_sl_mode(_cfg.TP1_SL_MODE)
    await broadcast_event({"type": "config_changed", "key": "tp1_sl_mode", "value": _cfg.TP1_SL_MODE})
    return {"tp1_sl_mode": _cfg.TP1_SL_MODE, "changed": True}


class DiscordNotificationsIn(BaseModel):
    enabled: bool


@app.post("/api/config/discord-notifications")
async def set_discord_notifications(body: DiscordNotificationsIn):
    """Turns sending signal/TP1 notifications to the Discord channel on/off.
    The Discord gateway stays connected (commands like !status keep working) —
    only the channel message sends themselves are disabled. The scanner, web
    dashboard, WebSocket, and Android push are completely unaffected."""
    if body.enabled == _cfg.DISCORD_NOTIFICATIONS_ENABLED:
        return {"discord_notifications_enabled": _cfg.DISCORD_NOTIFICATIONS_ENABLED, "changed": False}

    _cfg.DISCORD_NOTIFICATIONS_ENABLED = body.enabled
    save_discord_notifications_enabled(_cfg.DISCORD_NOTIFICATIONS_ENABLED)
    await broadcast_event({
        "type": "config_changed",
        "key": "discord_notifications_enabled",
        "value": _cfg.DISCORD_NOTIFICATIONS_ENABLED,
    })
    return {"discord_notifications_enabled": _cfg.DISCORD_NOTIFICATIONS_ENABLED, "changed": True}


class ScanIntervalIn(BaseModel):
    seconds: int


@app.post("/api/config/scan-interval")
async def set_scan_interval(body: ScanIntervalIn):
    """Changes the scanner interval live, without a container restart — see
    bot.set_scan_interval() / discord.ext.tasks.Loop.change_interval()."""
    if body.seconds not in _cfg.SCAN_INTERVAL_OPTIONS:
        raise HTTPException(400, f"seconds must be one of {_cfg.SCAN_INTERVAL_OPTIONS}")
    if body.seconds == _cfg.SCAN_INTERVAL_SECONDS:
        return {"scan_interval_seconds": _cfg.SCAN_INTERVAL_SECONDS, "changed": False}

    core.set_scan_interval(body.seconds)
    await broadcast_event({"type": "config_changed", "key": "scan_interval_seconds", "value": _cfg.SCAN_INTERVAL_SECONDS})
    return {"scan_interval_seconds": _cfg.SCAN_INTERVAL_SECONDS, "changed": True}


class OnchainIntervalIn(BaseModel):
    seconds: int


@app.post("/api/config/onchain-interval")
async def set_onchain_interval(body: OnchainIntervalIn):
    """Changes the on-chain data refresh interval (Etherscan/CoinGecko) live,
    without a container restart — see bot.set_onchain_interval()."""
    if body.seconds not in _cfg.ONCHAIN_INTERVAL_OPTIONS:
        raise HTTPException(400, f"seconds must be one of {_cfg.ONCHAIN_INTERVAL_OPTIONS}")
    if body.seconds == _cfg.ONCHAIN_CACHE_TTL:
        return {"onchain_interval_seconds": _cfg.ONCHAIN_CACHE_TTL, "changed": False}

    core.set_onchain_interval(body.seconds)
    await broadcast_event({"type": "config_changed", "key": "onchain_interval_seconds", "value": _cfg.ONCHAIN_CACHE_TTL})
    return {"onchain_interval_seconds": _cfg.ONCHAIN_CACHE_TTL, "changed": True}


class UthaIn(BaseModel):
    enabled: bool


@app.post("/api/config/utha")
async def set_utha(body: UthaIn):
    if body.enabled == _cfg.UT_HEIKIN_ASHI:
        return {"ut_heikin_ashi": _cfg.UT_HEIKIN_ASHI, "changed": False}
    _cfg.UT_HEIKIN_ASHI = body.enabled
    _cfg.save_ut_ha(_cfg.UT_HEIKIN_ASHI)
    return {"ut_heikin_ashi": _cfg.UT_HEIKIN_ASHI, "changed": True}


# 🆕 API/UI key ("frama"/"chop"/"atr"/"htf"/"fake_break"/"liq_sweep") -> the
# actual attribute name in config.py. Changed strictly via _cfg.X = ..., NOT
# via a local variable — otherwise signals.py and chart_data.py (which also
# specifically read _cfg.X/config.X) wouldn't see the runtime change and
# would keep running on the old value.
_FILTER_ATTR = {
    "frama": "ENABLE_FRAMA_FILTER",
    "chop": "ENABLE_CHOP_FILTER",
    "atr": "ENABLE_ATR_FILTER",
    "htf": "ENABLE_MTF_BIAS",
    "fake_break": "ENABLE_FAKE_BREAK_FILTER",
    "liq_sweep": "ENABLE_LIQ_SWEEP_FILTER",
}


class FilterToggleIn(BaseModel):
    filter: str  # frama | chop | atr | htf | fake_break | liq_sweep
    enabled: bool


@app.post("/api/config/filters")
async def set_filter_toggle(body: FilterToggleIn):
    key = body.filter.lower()
    if key not in _FILTER_ATTR:
        raise HTTPException(400, f"filter must be one of {list(_FILTER_ATTR)}")

    attr = _FILTER_ATTR[key]
    if body.enabled == getattr(_cfg, attr):
        return {"filter_toggles": _current_filter_toggles(), "changed": False}

    setattr(_cfg, attr, body.enabled)
    save_filter_toggles(_current_filter_toggles())
    await broadcast_event({"type": "config_changed", "key": f"filter_{key}", "value": body.enabled})
    return {"filter_toggles": _current_filter_toggles(), "changed": True}


def _current_filter_toggles() -> dict:
    return {
        "frama": _cfg.ENABLE_FRAMA_FILTER,
        "chop": _cfg.ENABLE_CHOP_FILTER,
        "atr": _cfg.ENABLE_ATR_FILTER,
        "htf": _cfg.ENABLE_MTF_BIAS,
        "fake_break": _cfg.ENABLE_FAKE_BREAK_FILTER,
        "liq_sweep": _cfg.ENABLE_LIQ_SWEEP_FILTER,
    }


class ChopIn(BaseModel):
    tf: str
    value: float


@app.post("/api/config/chop")
async def set_chop(body: ChopIn):
    tf = body.tf.lower()
    if tf not in TIMEFRAMES:
        raise HTTPException(400, f"tf must be one of {TIMEFRAMES}")
    if not (20.0 <= body.value <= 90.0):
        raise HTTPException(400, "value must be between 20 and 90")
    CHOP_THRESHOLD[tf] = body.value
    _cfg.save_chop(CHOP_THRESHOLD)
    return {"chop_threshold": CHOP_THRESHOLD}


class TpConfigIn(BaseModel):
    param: str  # mode | limit | percentile | safe
    value: str


@app.post("/api/config/tpconfig")
async def set_tpconfig(body: TpConfigIn):
    param = body.param.lower()
    if param == "mode":
        if body.value.lower() == "safe":
            _cfg.USE_SAFE_TP = True
        elif body.value.lower() in ("aggressive", "aggr"):
            _cfg.USE_SAFE_TP = False
        else:
            raise HTTPException(400, "value must be 'safe' or 'aggressive'")
        save_tp_config()
    elif param == "limit":
        try:
            new_limit = int(body.value)
        except ValueError:
            raise HTTPException(400, "value must be a number")
        if not (5 <= new_limit <= 200):
            raise HTTPException(400, "limit must be between 5 and 200")
        _cfg.SIGNAL_HISTORY_LIMIT = new_limit
        save_tp_config()
    elif param in ("percentile", "safe"):
        try:
            new_pct = float(body.value)
        except ValueError:
            raise HTTPException(400, "value must be a number")
        if not (10 <= new_pct <= 99):
            raise HTTPException(400, "percentile must be between 10 and 99")
        if param == "percentile":
            _cfg.TP_PERCENTILE = new_pct / 100
        else:
            _cfg.SAFE_TP_PERCENTILE = new_pct / 100
        save_tp_config()
    else:
        raise HTTPException(400, "param must be mode|limit|percentile|safe")

    return {
        "use_safe_tp": _cfg.USE_SAFE_TP,
        "tp_percentile": _cfg.TP_PERCENTILE,
        "safe_tp_percentile": _cfg.SAFE_TP_PERCENTILE,
        "signal_history_limit": _cfg.SIGNAL_HISTORY_LIMIT,
    }


# =====================================================================
# 📐  INDICATOR PARAMETERS (FRAMA / MFI / Andean / UT Bot)
# Change signal logic live — like mode/htf, they require a state reset so
# warmed_up/bars_since aren't computed against a mixed old/new window.
# =====================================================================
_INDICATOR_BOUNDS = {
    "frama_len": ("FRAMA_LEN", int, 5, 100),
    "frama_mult": ("FRAMA_MULT", float, 0.5, 5.0),
    "mfi_len": ("MFI_LEN", int, 2, 50),
    "mfi_training": ("MFI_TRAINING", int, 100, 3000),
    "and_len": ("AND_LEN", int, 5, 100),
    "and_sig_len": ("AND_SIG_LEN", int, 2, 50),
    "lookback": ("LOOKBACK", int, 1, 20),
    "ut_sensitivity": ("UT_SENSITIVITY", float, 0.1, 10.0),
    "ut_period": ("UT_PERIOD", int, 2, 50),
    "bb_period": ("BB_PERIOD", int, 5, 100),
    "bb_stddev": ("BB_STDDEV", float, 0.5, 5.0),
    "sr_pivot_window": ("SR_PIVOT_WINDOW", int, 3, 50),
    "sr_max_levels": ("SR_MAX_LEVELS", int, 1, 10),
}


class IndicatorsIn(BaseModel):
    # Any subset of fields — a partial PATCH. See _INDICATOR_BOUNDS for the keys.
    frama_len: Optional[int] = None
    frama_mult: Optional[float] = None
    mfi_len: Optional[int] = None
    mfi_training: Optional[int] = None
    and_len: Optional[int] = None
    and_sig_len: Optional[int] = None
    lookback: Optional[int] = None
    ut_sensitivity: Optional[float] = None
    ut_period: Optional[int] = None
    bb_period: Optional[int] = None
    bb_stddev: Optional[float] = None
    sr_pivot_window: Optional[int] = None
    sr_max_levels: Optional[int] = None


@app.post("/api/config/indicators")
async def set_indicators(body: IndicatorsIn):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "At least one field is required")

    # 🆕 FIX: validation and application used to happen in the same loop — if
    # a PATCH updated several fields and one further down the list failed
    # bounds validation, the earlier fields had already been applied to
    # _cfg via setattr() before the exception aborted the request. The
    # response was an error, but _cfg was left partially mutated (and never
    # persisted via save_indicators(), which only ran after the loop) —
    # a silent, inconsistent half-applied state until the next successful
    # call or a restart. Now every field is validated first; setattr() only
    # runs once everything has passed.
    casted = {}
    for key, value in updates.items():
        attr, caster, lo, hi = _INDICATOR_BOUNDS[key]
        if not (lo <= value <= hi):
            raise HTTPException(400, f"{key} must be between {lo} and {hi}")
        casted[attr] = caster(value)

    for attr, value in casted.items():
        setattr(_cfg, attr, value)

    _cfg.save_indicators({
        "FRAMA_LEN": _cfg.FRAMA_LEN,
        "FRAMA_MULT": _cfg.FRAMA_MULT,
        "MFI_LEN": _cfg.MFI_LEN,
        "MFI_TRAINING": _cfg.MFI_TRAINING,
        "AND_LEN": _cfg.AND_LEN,
        "AND_SIG_LEN": _cfg.AND_SIG_LEN,
        "LOOKBACK": _cfg.LOOKBACK,
        "UT_SENSITIVITY": _cfg.UT_SENSITIVITY,
        "UT_PERIOD": _cfg.UT_PERIOD,
        "BB_PERIOD": _cfg.BB_PERIOD,
        "BB_STDDEV": _cfg.BB_STDDEV,
        "SR_PIVOT_WINDOW": _cfg.SR_PIVOT_WINDOW,
        "SR_MAX_LEVELS": _cfg.SR_MAX_LEVELS,
    })
    await _reset_states_after_regime_change()
    await broadcast_event({"type": "config_changed", "key": "indicators"})
    return {
        "frama_len": _cfg.FRAMA_LEN,
        "frama_mult": _cfg.FRAMA_MULT,
        "mfi_len": _cfg.MFI_LEN,
        "mfi_training": _cfg.MFI_TRAINING,
        "and_len": _cfg.AND_LEN,
        "and_sig_len": _cfg.AND_SIG_LEN,
        "lookback": _cfg.LOOKBACK,
        "ut_sensitivity": _cfg.UT_SENSITIVITY,
        "ut_period": _cfg.UT_PERIOD,
        "bb_period": _cfg.BB_PERIOD,
        "bb_stddev": _cfg.BB_STDDEV,
        "sr_pivot_window": _cfg.SR_PIVOT_WINDOW,
        "sr_max_levels": _cfg.SR_MAX_LEVELS,
    }


class VolumeProfileIn(BaseModel):
    # Any subset of fields — a partial PATCH, same as IndicatorsIn.
    enabled: Optional[bool] = None
    bins: Optional[int] = None
    lookback: Optional[int] = None
    value_area_pct: Optional[float] = None
    show_histogram: Optional[bool] = None


@app.post("/api/config/volume-profile")
async def set_volume_profile(body: VolumeProfileIn):
    """Volume Profile (POC / Value Area) settings. Purely a chart overlay —
    unlike indicator params, it doesn't affect signal generation, so no
    state reset is needed here."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "At least one field is required")

    # Validate everything first, apply only once everything passes — same
    # reasoning as set_indicators()/set_colors() above.
    if "bins" in updates and not (10 <= updates["bins"] <= 200):
        raise HTTPException(400, "bins must be between 10 and 200")
    if "lookback" in updates and not (50 <= updates["lookback"] <= 2000):
        raise HTTPException(400, "lookback must be between 50 and 2000")
    if "value_area_pct" in updates and not (0.5 <= updates["value_area_pct"] <= 0.95):
        raise HTTPException(400, "value_area_pct must be between 0.5 and 0.95")

    if "enabled" in updates:
        _cfg.VP_ENABLED = updates["enabled"]
    if "bins" in updates:
        _cfg.VP_BINS = updates["bins"]
    if "lookback" in updates:
        _cfg.VP_LOOKBACK = updates["lookback"]
    if "value_area_pct" in updates:
        _cfg.VP_VALUE_AREA_PCT = updates["value_area_pct"]
    if "show_histogram" in updates:
        _cfg.VP_SHOW_HISTOGRAM = updates["show_histogram"]

    _cfg.save_vp_config({
        "enabled": _cfg.VP_ENABLED,
        "bins": _cfg.VP_BINS,
        "lookback": _cfg.VP_LOOKBACK,
        "value_area_pct": _cfg.VP_VALUE_AREA_PCT,
        "show_histogram": _cfg.VP_SHOW_HISTOGRAM,
    })
    await broadcast_event({"type": "config_changed", "key": "volume_profile"})
    return {
        "enabled": _cfg.VP_ENABLED,
        "bins": _cfg.VP_BINS,
        "lookback": _cfg.VP_LOOKBACK,
        "value_area_pct": _cfg.VP_VALUE_AREA_PCT,
        "show_histogram": _cfg.VP_SHOW_HISTOGRAM,
    }


# =====================================================================
# 🎨  CHART COLORS — purely visual, no need to reset state
# =====================================================================
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ColorsIn(BaseModel):
    frama: Optional[str] = None
    bb: Optional[str] = None
    support: Optional[str] = None
    resistance: Optional[str] = None
    poc: Optional[str] = None
    mfi_line: Optional[str] = None
    mfi_overbought: Optional[str] = None
    mfi_oversold: Optional[str] = None
    candle_up: Optional[str] = None
    candle_down: Optional[str] = None
    tp_line: Optional[str] = None
    sl_line: Optional[str] = None
    signal_long: Optional[str] = None
    signal_short: Optional[str] = None


@app.post("/api/config/colors")
async def set_colors(body: ColorsIn):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "At least one field is required")

    # 🆕 FIX: same class of bug as set_indicators() above — validation and
    # application used to be a single loop, so a later invalid hex color in
    # a multi-field request left the earlier, already-validated colors
    # applied to the shared _cfg.CHART_COLORS dict in place, with the request
    # still failing overall and nothing saved to disk. Validate every color
    # first, apply only once everything passes.
    for key, value in updates.items():
        if not _HEX_RE.match(value):
            raise HTTPException(400, f"{key}: color must be in #RRGGBB format")
    for key, value in updates.items():
        _cfg.CHART_COLORS[key] = value

    _cfg.save_colors(_cfg.CHART_COLORS)
    await broadcast_event({"type": "config_changed", "key": "colors"})
    return _cfg.CHART_COLORS


# =====================================================================
# 🌐  FRONTEND STATIC ASSETS (built web/dist, see the root Dockerfile)
# Registered LAST: /api/*, /ws/*, /docs, etc. earlier in the file match
# first, and this mount catches everything else — index.html and assets.
# =====================================================================
import os
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")
else:
    logger.warning(f"[WEB] Static dir not found at {_STATIC_DIR} — the frontend won't be served "
                    f"(normal for a local run without building web/, see the README)")
