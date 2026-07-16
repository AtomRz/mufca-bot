"""
MUFCA v4.0 — Web API
FastAPI-бэкенд для веб-морды. Работает В ТОМ ЖЕ процессе и asyncio-loop,
что и discord-бот (см. main.py) — переиспользует bot.state, bot._exchange_ref
и config.py как есть, никакого отдельного пересчёта индикаторов сканером.

Эндпоинты:
  GET  /api/status                      — сводка по всем парам/TF (как !status)
  GET  /api/pairs                       — список тикеров
  POST /api/pairs                       — добавить тикер {"ticker": "DOGE/USDT"}
  DELETE /api/pairs/{ticker}            — убрать тикер
  GET  /api/chart?ticker=...&tf=...     — JSON для графика (свечи+FRAMA+BB+S/R+MFI)
  GET  /api/config                      — все редактируемые настройки одним объектом
  POST /api/config/mode                 — {"mode": "spot"|"futures"}
  POST /api/config/htf                  — {"htf": "4h"}
  POST /api/config/utha                 — {"enabled": true}
  POST /api/config/chop                 — {"tf": "1h", "value": 55.0}
  POST /api/config/tpconfig             — {"param": "mode"|"limit"|"percentile"|"safe", "value": ...}
  WS   /ws/live                         — broadcast новых баров/сигналов/закрытий сделок
"""

import asyncio
import logging
import re
import secrets
from typing import Optional, List, Set
from urllib.parse import unquote

import ccxt
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="MUFCA Web API")

# Разрешаем локальный dev-фронт (vite и т.п.); в проде фронт и так за тем же nginx/tunnel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 🆕 Циклический импорт по тому же паттерну, что discord_commands.py:
# bot.py импортирует web_api в самом низу, когда bot/state/config уже готовы.
import bot as core
import config as _cfg
from config import TIMEFRAMES, CHOP_THRESHOLD, save_mode, save_htf, save_tp_config
from signals import make_state, clear_htf_cache
from chart_data import get_chart_data, get_market_pulse
from state import load_signals_history


# =====================================================================
# 🔒  HTTP BASIC AUTH — защищает и /api/*, и раздачу статики фронта.
# Если WEB_USERNAME/WEB_PASSWORD не заданы в .env — дашборд остаётся
# открытым (для локальной разработки), но громко предупреждаем в логах
# при каждом старте, чтобы это нельзя было не заметить.
# =====================================================================
if not _cfg.WEB_USERNAME or not _cfg.WEB_PASSWORD:
    logger.warning(
        "[AUTH] ⚠️  WEB_USERNAME/WEB_PASSWORD не заданы в .env — веб-дашборд "
        "ОТКРЫТ БЕЗ АВТОРИЗАЦИИ. Любой с доступом к порту 8585 может менять "
        "настройки бота. Задай оба значения в .env, чтобы включить защиту."
    )


class BasicAuthASGIMiddleware:
    """Чистый ASGI middleware (не BaseHTTPMiddleware!) — тот не видит websocket-scope,
    а /ws/live тоже должен требовать авторизации, иначе фид сигналов остаётся открытым
    даже когда весь остальной API защищён.

    🆕 FIX: нативный браузерный WebSocket API НЕ умеет выставлять кастомные заголовки —
    Authorization на handshake он не отправляет, сколько бы креды ни были закэшированы
    браузером для fetch(). Поэтому для websocket-scope дополнительно принимаем токен как
    query-параметр (?auth=base64(user:pass), то же значение что было бы в Basic-заголовке) —
    единственный канал, который реально контролируется JS на WS-соединении."""

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
        # constant-time сравнение — не даём определить правильность по времени ответа
        return (
            secrets.compare_digest(user, _cfg.WEB_USERNAME)
            and secrets.compare_digest(pwd, _cfg.WEB_PASSWORD)
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        # 🆕 Защищаем только /api/* и /ws/* — статику фронта (index.html, JS/CSS-бандл)
        # отдаём свободно, иначе браузер покажет СВОЙ нативный Basic Auth попап поверх
        # нашего кастомного логин-экрана ещё до того, как React успеет отрендериться.
        # В самой статике нет ничего чувствительного, только код — секьюрити boundary
        # именно на API/WS, где реально читаются/меняются данные бота.
        path = scope.get("path", "")
        if not (path.startswith("/api/") or path.startswith("/ws/")):
            return await self.app(scope, receive, send)

        if not _cfg.WEB_USERNAME or not _cfg.WEB_PASSWORD:
            return await self.app(scope, receive, send)  # auth выключен — креды не заданы

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")

        authorized = False
        if auth_header.startswith("Basic "):
            authorized = self._check_basic_value(auth_header[6:])

        if not authorized and scope["type"] == "websocket":
            from urllib.parse import parse_qs
            qs = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="ignore"))
            token = (qs.get("auth") or [None])[0]
            if token:
                authorized = self._check_basic_value(token)

        if authorized:
            return await self.app(scope, receive, send)

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
    """Рассылает событие всем подключенным клиентам. Вызывается из bot.py."""
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(event)
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
            # Клиент ничего не шлёт — просто держим соединение живым
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


# =====================================================================
# 📊  STATUS / PAIRS
# =====================================================================
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


@app.get("/api/pairs")
async def get_pairs():
    return {"tickers": _cfg.TICKERS}


class PairIn(BaseModel):
    ticker: str


@app.post("/api/pairs")
async def add_pair(body: PairIn):
    ticker = body.ticker.upper().strip()
    if "/" not in ticker:
        raise HTTPException(400, "Формат тикера: BASE/QUOTE, например DOGE/USDT")
    if ticker in _cfg.TICKERS:
        raise HTTPException(409, f"{ticker} уже отслеживается")
    _cfg.TICKERS.append(ticker)
    _cfg.save_tickers(_cfg.TICKERS)
    core.state[ticker] = {tf: make_state() for tf in TIMEFRAMES}
    async with core._tickers_lock:
        core._ensure_locks()
    return {"tickers": _cfg.TICKERS}


@app.delete("/api/pairs/{ticker:path}")
async def remove_pair(ticker: str):
    ticker = unquote(ticker).upper().strip()
    if ticker not in _cfg.TICKERS:
        raise HTTPException(404, f"{ticker} не отслеживается")
    _cfg.TICKERS.remove(ticker)
    _cfg.save_tickers(_cfg.TICKERS)
    return {"tickers": _cfg.TICKERS}


# =====================================================================
# 📈  CHART DATA
# =====================================================================
@app.get("/api/pulse")
async def pulse(ticker: Optional[str] = None, tf: str = "1h"):
    """Сводка для верхней плашки: CHOP/тренд/suggested leverage по одной referens-паре
    (по умолчанию — первая отслеживаемая пара). Не привязан к выбору на вкладке Chart."""
    ticker = unquote(ticker).upper().strip() if ticker else (_cfg.TICKERS[0] if _cfg.TICKERS else None)
    if not ticker:
        raise HTTPException(404, "Нет отслеживаемых пар")
    if ticker not in _cfg.TICKERS:
        raise HTTPException(404, f"{ticker} не отслеживается")
    if tf not in TIMEFRAMES:
        raise HTTPException(400, f"tf должен быть одним из {TIMEFRAMES}")

    exchange = core._exchange_ref
    if exchange is None:
        raise HTTPException(503, "Бот ещё не подключился к бирже, попробуй через пару секунд")

    try:
        return await get_market_pulse(exchange, ticker, tf)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/chart")
async def chart(ticker: str, tf: str, limit: int = 150, track: str = "a"):
    ticker = unquote(ticker).upper().strip()
    if ticker not in _cfg.TICKERS:
        raise HTTPException(404, f"{ticker} не отслеживается")
    if tf not in TIMEFRAMES:
        raise HTTPException(400, f"tf должен быть одним из {TIMEFRAMES}")

    exchange = core._exchange_ref
    if exchange is None:
        raise HTTPException(503, "Бот ещё не подключился к бирже, попробуй через пару секунд")

    st = core.state.get(ticker, {}).get(tf, {})
    trade_key = "a_active_trade" if track == "a" else "u_active_trade"
    active_trade = st.get(trade_key)

    try:
        data = await get_chart_data(exchange, ticker, tf, limit=limit, state_snapshot=active_trade)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return data


# =====================================================================
# 📚  ИСТОРИЯ СИГНАЛОВ / ВИНРЕЙТ
# То же, что !signals в Discord, но JSON + разбивка по track (A/U), которую
# !signals не делает (там a+u+sim смешаны в одну кучу).
# =====================================================================
def _aggregate_records(records: list) -> Optional[dict]:
    """Агрегирует закрытые сигналы: винрейт, средние MFE/MAE/PnL, разбивка TP/SL/cancelled.
    Синтетические (!sim) записи исключены — как и в get_signal_stats/calculate_adaptive_tp,
    они не отражают реальное поведение рынка."""
    closed = [r for r in records if r.get("exit_type") in ("tp", "sl", "cancelled") and not r.get("synthetic", False)]
    if not closed:
        return None
    wins = sum(1 for r in closed if r.get("moved_pct", 0) > 0)
    tp_hits = sum(1 for r in closed if r["exit_type"] == "tp")
    sl_hits = sum(1 for r in closed if r["exit_type"] == "sl")
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
        "cancelled": cancelled,
    }


@app.get("/api/history/summary")
async def history_summary():
    """Винрейт/статистика по каждой комбинации ticker/tf/side/track, у которой есть закрытые сигналы,
    плюс итоговая строка по всем вместе."""
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
                            if r.get("exit_type") in ("tp", "sl", "cancelled") and not r.get("synthetic", False)
                        )

    rows.sort(key=lambda r: (r["ticker"], r["tf"], r["side"], r["track"]))
    total = _aggregate_records(all_closed_for_total)
    return {"rows": rows, "total": total}


@app.get("/api/history/records")
async def history_records(ticker: str, tf: str, side: str, track: str = "a", limit: int = 30):
    ticker = unquote(ticker).upper().strip()
    side = side.lower()
    if side not in ("long", "short"):
        raise HTTPException(400, "side должен быть 'long' или 'short'")

    history = load_signals_history()
    records = history.get(ticker, {}).get(tf, {}).get(side, [])
    closed = [r for r in records if r.get("track") == track and r.get("exit_type") != "open"]
    closed = list(reversed(closed[-limit:]))  # свежие первыми
    return {"ticker": ticker, "tf": tf, "side": side, "track": track, "records": closed}


# =====================================================================
# ⚙️  CONFIG — get всех настроек + set по одной, 1-в-1 логика discord-команд
# =====================================================================
@app.get("/api/config")
async def get_config():
    return {
        "mode": _cfg.MARKET_MODE,
        "htf_bias": _cfg.HTF_BIAS,
        "ut_heikin_ashi": _cfg.UT_HEIKIN_ASHI,
        "chop_threshold": CHOP_THRESHOLD,
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
        "colors": _cfg.CHART_COLORS,
        "timeframes": TIMEFRAMES,
        "pairs": _cfg.TICKERS,
    }


async def _reset_states_after_regime_change():
    """То же, что делают mode_cmd/htf_cmd после смены режима — иначе стейт рассинхронится."""
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
        raise HTTPException(400, "mode должен быть 'spot' или 'futures'")
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
        raise HTTPException(400, f"htf должен быть одним из {valid_htfs}")
    if new_htf == _cfg.HTF_BIAS:
        return {"htf_bias": _cfg.HTF_BIAS, "changed": False}

    _cfg.HTF_BIAS = new_htf
    save_htf(_cfg.HTF_BIAS)
    clear_htf_cache()
    await _reset_states_after_regime_change()
    await broadcast_event({"type": "config_changed", "key": "htf", "value": _cfg.HTF_BIAS})
    return {"htf_bias": _cfg.HTF_BIAS, "changed": True}


class UthaIn(BaseModel):
    enabled: bool


@app.post("/api/config/utha")
async def set_utha(body: UthaIn):
    if body.enabled == _cfg.UT_HEIKIN_ASHI:
        return {"ut_heikin_ashi": _cfg.UT_HEIKIN_ASHI, "changed": False}
    _cfg.UT_HEIKIN_ASHI = body.enabled
    _cfg.save_ut_ha(_cfg.UT_HEIKIN_ASHI)
    return {"ut_heikin_ashi": _cfg.UT_HEIKIN_ASHI, "changed": True}


class ChopIn(BaseModel):
    tf: str
    value: float


@app.post("/api/config/chop")
async def set_chop(body: ChopIn):
    tf = body.tf.lower()
    if tf not in TIMEFRAMES:
        raise HTTPException(400, f"tf должен быть одним из {TIMEFRAMES}")
    if not (20.0 <= body.value <= 90.0):
        raise HTTPException(400, "value должен быть между 20 и 90")
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
            raise HTTPException(400, "value должен быть 'safe' или 'aggressive'")
        save_tp_config()
    elif param == "limit":
        try:
            new_limit = int(body.value)
        except ValueError:
            raise HTTPException(400, "value должен быть числом")
        if not (5 <= new_limit <= 200):
            raise HTTPException(400, "limit должен быть между 5 и 200")
        _cfg.SIGNAL_HISTORY_LIMIT = new_limit
        save_tp_config()
    elif param in ("percentile", "safe"):
        try:
            new_pct = float(body.value)
        except ValueError:
            raise HTTPException(400, "value должен быть числом")
        if not (10 <= new_pct <= 99):
            raise HTTPException(400, "percentile должен быть между 10 и 99")
        if param == "percentile":
            _cfg.TP_PERCENTILE = new_pct / 100
        else:
            _cfg.SAFE_TP_PERCENTILE = new_pct / 100
        save_tp_config()
    else:
        raise HTTPException(400, "param должен быть mode|limit|percentile|safe")

    return {
        "use_safe_tp": _cfg.USE_SAFE_TP,
        "tp_percentile": _cfg.TP_PERCENTILE,
        "safe_tp_percentile": _cfg.SAFE_TP_PERCENTILE,
        "signal_history_limit": _cfg.SIGNAL_HISTORY_LIMIT,
    }


# =====================================================================
# 📐  ПАРАМЕТРЫ ИНДИКАТОРОВ (FRAMA / MFI / Andean / UT Bot)
# Меняют логику сигналов на лету — как и mode/htf, требуют сброса стейта,
# чтобы warmed_up/bars_since не считались по вперемешку старым/новым окном.
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
    # Любое подмножество полей — как partial PATCH. Ключи см. _INDICATOR_BOUNDS.
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
        raise HTTPException(400, "Нужно передать хотя бы одно поле")

    for key, value in updates.items():
        attr, caster, lo, hi = _INDICATOR_BOUNDS[key]
        if not (lo <= value <= hi):
            raise HTTPException(400, f"{key} должен быть между {lo} и {hi}")
        setattr(_cfg, attr, caster(value))

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


# =====================================================================
# 🎨  ЦВЕТА ГРАФИКА — чисто визуальные, стейт сбрасывать не нужно
# =====================================================================
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ColorsIn(BaseModel):
    frama: Optional[str] = None
    bb: Optional[str] = None
    support: Optional[str] = None
    resistance: Optional[str] = None
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
        raise HTTPException(400, "Нужно передать хотя бы одно поле")
    for key, value in updates.items():
        if not _HEX_RE.match(value):
            raise HTTPException(400, f"{key}: цвет должен быть в формате #RRGGBB")
        _cfg.CHART_COLORS[key] = value
    _cfg.save_colors(_cfg.CHART_COLORS)
    await broadcast_event({"type": "config_changed", "key": "colors"})
    return _cfg.CHART_COLORS


# =====================================================================
# 🌐  СТАТИКА ФРОНТА (собранный web/dist, см. корневой Dockerfile)
# Регистрируется ПОСЛЕДНЕЙ: /api/*, /ws/*, /docs и т.д. выше по файлу
# матчатся первыми, а этот mount ловит всё остальное — index.html и ассеты.
# =====================================================================
import os
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")
else:
    logger.warning(f"[WEB] Static dir not found at {_STATIC_DIR} — фронт не будет отдаваться "
                    f"(нормально при локальном запуске без сборки web/, посмотри README)")
