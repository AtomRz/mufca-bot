import json
import numpy as np
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone
import logging

from config import (
    SIGNALS_HISTORY_FILE,
    SIGNAL_HISTORY_LIMIT,
    TP_PERCENTILE,
    SAFE_TP_PERCENTILE,
    USE_SAFE_TP,
    MIN_TP_PCT,
    MAX_TP_PCT,
    safe_json_load,
    safe_json_save,
)

logger = logging.getLogger(__name__)

# =====================================================================
# 💾  УПРАВЛЕНИЕ ИСТОРИЕЙ СИГНАЛОВ
# =====================================================================

_history_cache: Optional[Dict] = None

# =====================================================================
# 💾  УПРАВЛЕНИЕ ИСТОРИЕЙ СИГНАЛОВ
# =====================================================================

def load_signals_history() -> Dict:
    """Загружает историю сигналов из файла."""
    global _history_cache
    if _history_cache is not None:
        return _history_cache

    _history_cache = safe_json_load(SIGNALS_HISTORY_FILE, {})
    return _history_cache

def save_signals_history(history: Dict):
    """Сохраняет историю сигналов в файл."""
    global _history_cache
    _history_cache = history
    safe_json_save(SIGNALS_HISTORY_FILE, history)

def _ensure_history_slot(history: Dict, ticker: str, tf: str):
    """Создает слот для пары/таймфрейма если его нет."""
    if ticker not in history:
        history[ticker] = {}
    if tf not in history[ticker]:
        history[ticker][tf] = {"long": [], "short": []}

def _normalize_timestamp(timestamp) -> str:
    """Унифицирует timestamp в ISO формат."""
    if isinstance(timestamp, datetime):
        return timestamp.isoformat()
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()
    if isinstance(timestamp, str):
        if timestamp.isdigit():
            return datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc).isoformat()
        return timestamp
    return datetime.now(timezone.utc).isoformat()

def add_signal_record(ticker: str, tf: str, side: str, entry: float, timestamp):
    """Добавляет запись о новом сигнале."""
    history = load_signals_history()
    _ensure_history_slot(history, ticker, tf)

    record = {
        "entry": round(entry, 4),
        "exit": None,
        "exit_type": "open",
        "bars_held": 0,
        "moved_pct": 0.0,
        "timestamp": _normalize_timestamp(timestamp),
        "max_favorable_pct": 0.0,
        "max_adverse_pct": 0.0,
    }

    history[ticker][tf][side].append(record)
    history[ticker][tf][side] = history[ticker][tf][side][-(SIGNAL_HISTORY_LIMIT * 3):]
    save_signals_history(history)
    logger.info(f"[SIGNAL] ADDED {side} signal for {ticker} {tf} @ {entry}")

def update_signal_record(ticker: str, tf: str, side: str, exit_price: float, exit_type: str, bars_held: int):
    """Закрывает открытый сигнал."""
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        logger.warning(f"Cannot update signal: no history for {ticker} {tf}")
        return

    records = history[ticker][tf][side]
    for rec in reversed(records):
        if rec["exit_type"] == "open":
            rec["exit"] = round(exit_price, 4)
            rec["exit_type"] = exit_type
            rec["bars_held"] = bars_held
            entry = rec["entry"]
            if side == "long":
                rec["moved_pct"] = round((exit_price - entry) / entry * 100, 4)
            else:
                rec["moved_pct"] = round((entry - exit_price) / entry * 100, 4)
            save_signals_history(history)
            logger.info(f"[SIGNAL] CLOSED {side} signal for {ticker} {tf} | PnL: {rec['moved_pct']:.2f}%")
            return

    logger.warning(f"No open signal found to close for {ticker} {tf} {side}")

def update_signal_mae_mfe(ticker: str, tf: str, side: str, current_price: float, save_every: int = 5):
    """
    Обновляет MFE/MAE для открытого сигнала.
    Сохраняет на диск не чаще, чем каждые `save_every` вызовов.
    """
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        return

    records = history[ticker][tf][side]
    updated = False

    for rec in reversed(records):
        if rec["exit_type"] == "open":
            entry = rec["entry"]
            if side == "long":
                favorable = (current_price - entry) / entry * 100
                adverse = (entry - current_price) / entry * 100
            else:
                favorable = (entry - current_price) / entry * 100
                adverse = (current_price - entry) / entry * 100

            rec["max_favorable_pct"] = round(max(float(rec.get("max_favorable_pct", 0)), favorable), 4)
            rec["max_adverse_pct"] = round(max(float(rec.get("max_adverse_pct", 0)), adverse), 4)
            updated = True
            break

    if updated and getattr(update_signal_mae_mfe, "_counter", 0) % save_every == 0:
        save_signals_history(history)

    update_signal_mae_mfe._counter = getattr(update_signal_mae_mfe, "_counter", 0) + 1

# =====================================================================
# 📊  СТАТИСТИКА ПО СИГНАЛАМ
# =====================================================================

def get_signal_stats(ticker: str, tf: str, side: str) -> Dict:
    """Возвращает статистику по сигналам."""
    history = load_signals_history()
    empty = {
        "count": 0,
        "avg_mfe": 0,
        "median_mfe": 0,
        "tp_pct": 0,
        "best": 0,
        "worst": 0,
        "mean_mfe": 0,
        "std_mfe": 0,
    }

    if ticker not in history or tf not in history[ticker]:
        return empty

    records = history[ticker][tf][side]
    # ✅ ИСПРАВЛЕНО: включаем cancelled в статистику для более полной картины
    closed = [r for r in records if r["exit_type"] in ("tp", "sl", "cancelled")]
    if not closed:
        return empty

    recent = closed[-SIGNAL_HISTORY_LIMIT:]
    favorable_pcts = []
    for r in recent:
        mfe = r.get("max_favorable_pct", 0)
        if mfe == 0 and r.get("moved_pct", 0) != 0:
            mfe = abs(r["moved_pct"])
        favorable_pcts.append(max(mfe, 0.1))

    if not favorable_pcts:
        return empty

    return {
        "count": len(recent),
        "avg_mfe": round(float(np.mean(favorable_pcts)), 2),
        "median_mfe": round(float(np.median(favorable_pcts)), 2),
        "tp_pct": round(float(np.mean(favorable_pcts) + 0.5 * np.std(favorable_pcts)), 2),
        "mean_mfe": round(float(np.mean(favorable_pcts)), 2),
        "std_mfe": round(float(np.std(favorable_pcts)), 2),
        "best": round(float(max(favorable_pcts)), 2),
        "worst": round(float(min(favorable_pcts)), 2),
    }

# =====================================================================
# 🎯  АДАПТИВНЫЙ ТП
# =====================================================================

def calculate_adaptive_tp(ticker: str, tf: str, side: str, entry: float, current_sl: float) -> float:
    """Адаптивный TP на основе исторических MFE."""
    history = load_signals_history()
    risk = abs(entry - current_sl)
    fallback_tp = entry + (2.0 * risk) if side == "long" else entry - (2.0 * risk)

    if ticker not in history or tf not in history[ticker]:
        return round(fallback_tp, 4)

    records = history[ticker][tf][side]
    # ✅ ИСПРАВЛЕНО: включаем cancelled
    closed = [r for r in records if r["exit_type"] in ("tp", "sl", "cancelled")]

    if len(closed) < 3:
        return round(fallback_tp, 4)

    recent = closed[-SIGNAL_HISTORY_LIMIT:]
    favorable_pcts = []
    for r in recent:
        mfe = r.get("max_favorable_pct", 0)
        if mfe == 0 and r.get("moved_pct", 0) != 0:
            mfe = abs(r["moved_pct"])
        favorable_pcts.append(max(mfe, 0.1))

    if not favorable_pcts:
        return round(fallback_tp, 4)

    active_pct = SAFE_TP_PERCENTILE if USE_SAFE_TP else TP_PERCENTILE
    tp_pct = float(np.percentile(favorable_pcts, active_pct * 100))
    tp_pct = max(MIN_TP_PCT, min(MAX_TP_PCT, tp_pct))

    tp = entry * (1 + tp_pct / 100) if side == "long" else entry * (1 - tp_pct / 100)
    return round(tp, 4)

def calculate_combined_tp(
    ticker: str,
    tf: str,
    side: str,
    entry: float,
    sl: float,
    df,
    idx: int,
    atr14
) -> Tuple[float, str]:
    """Комбинированный TP: адаптивный + R:R 2.0 как fallback."""
    stats = get_signal_stats(ticker, tf, side)
    tp = calculate_adaptive_tp(ticker, tf, side, entry, sl)
    risk = abs(entry - sl)
    rr = round(abs(tp - entry) / max(risk, 1e-8), 2)

    mode_label = "SAFE" if USE_SAFE_TP else "AGGR"
    if stats["count"] >= 5:
        desc = f"📚 Adaptive {SAFE_TP_PERCENTILE if USE_SAFE_TP else TP_PERCENTILE:.0%} %ile [{mode_label}] | {stats['count']} signals"
    else:
        desc = f"📐 Fallback R:R 2.0 (only {stats['count']} signals)"

    return tp, desc
