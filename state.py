import json
import numpy as np
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone
import logging

import config as _cfg
from config import (
    SIGNALS_HISTORY_FILE,
    SIGNAL_HISTORY_LIMIT,
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

def clear_history_cache():
    """Сбрасывает кэш истории сигналов — вызывать при удалении файла истории."""
    global _history_cache
    _history_cache = None


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

def add_signal_record(ticker: str, tf: str, side: str, entry: float, timestamp, regime: str = "unknown"):
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
        "regime": regime,  # 🆕 Режим рынка при входе
    }

    history[ticker][tf][side].append(record)
    history[ticker][tf][side] = history[ticker][tf][side][-(SIGNAL_HISTORY_LIMIT * 3):]
    save_signals_history(history)
    logger.info(f"[SIGNAL] ADDED {side} signal for {ticker} {tf} @ {entry} | Regime: {regime}")

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
            logger.info(f"[SIGNAL] CLOSED {side} signal for {ticker} {tf} | PnL: {rec['moved_pct']:.2f}% | Regime: {rec.get('regime', 'unknown')}")
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

    update_signal_mae_mfe._counter = getattr(update_signal_mae_mfe, "_counter", 0) + 1

    if updated and update_signal_mae_mfe._counter % save_every == 0:
        save_signals_history(history)

# =====================================================================
# 📊  СТАТИСТИКА ПО СИГНАЛАМ
# =====================================================================

def get_signal_stats(ticker: str, tf: str, side: str, regime: Optional[str] = None) -> Dict:
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

    # 🆕 FIX: Фильтрация по режиму, если указан
    regime_used = False
    if regime:
        regime_closed = [r for r in closed if r.get("regime", "unknown") == regime]
        if len(regime_closed) >= 5:  # Минимум 5 сигналов для режима
            closed = regime_closed
            regime_used = True
        else:
            # Недостаточно данных по режиму — используем все (логируем)
            logger.debug(f"[STATS] Only {len(regime_closed)} {regime} signals for {ticker} {tf} {side}, falling back to all {len(closed)} signals")

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
        "regime_applied": regime_used if regime else None,
        "regime": regime,
    }

# =====================================================================
# 🎯  АДАПТИВНЫЙ ТП (ГИБРИДНЫЙ: РЕЖИМ + ВЗВЕШИВАНИЕ)
# =====================================================================

def _extract_weighted_mfes(records: List[Dict]) -> List[Tuple[float, float]]:
    """
    Извлекает MFE с весами по типу выхода.
    Возвращает список (mfe, weight).
    """
    favorable_pcts = []

    for r in records:
        mfe = r.get("max_favorable_pct", 0)
        if mfe == 0 and r.get("moved_pct", 0) != 0:
            mfe = abs(r["moved_pct"])
        mfe = max(mfe, 0.1)

        exit_type = r.get("exit_type", "unknown")
        if exit_type == "tp":
            weight = 1.0
        elif exit_type == "sl":
            weight = 0.6
        elif exit_type == "cancelled":
            weight = 0.4
        else:
            weight = 0.5

        favorable_pcts.append((mfe, weight))

    return favorable_pcts

def _build_weighted_sample(weighted_mfes: List[Tuple[float, float]]) -> List[float]:
    """
    Строит взвешенную выборку для персентиля.
    Максимум 2 копии на сигнал (tp=2, sl=1, cancelled=1) — не раздувает выборку.
    """
    expanded = []
    for mfe, weight in weighted_mfes:
        # tp → 2 копии, всё остальное → 1 копия
        copies = 2 if weight >= 1.0 else 1
        expanded.extend([mfe] * copies)
    return expanded

def calculate_adaptive_tp(
    ticker: str,
    tf: str,
    side: str,
    entry: float,
    current_sl: float,
    atr14: Optional[float] = None,
    regime: Optional[str] = None
) -> float:
    """
    Адаптивный TP на основе исторических MFE.
    Гибридная логика:
    1. Если по режиму ≥ 10 сигналов — используем только их
    2. Если 5-9 сигналов — используем режим + общие с дисконтом
    3. Если < 5 — используем все с взвешиванием по exit_type
    """
    history = load_signals_history()
    risk = abs(entry - current_sl)
    fallback_tp = entry + (2.0 * risk) if side == "long" else entry - (2.0 * risk)

    if ticker not in history or tf not in history[ticker]:
        return round(fallback_tp, 4)

    records = history[ticker][tf][side]
    closed = [r for r in records if r["exit_type"] in ("tp", "sl", "cancelled")]

    if len(closed) < 3:
        return round(fallback_tp, 4)

    # 🆕 FIX: ГИБРИДНАЯ ЛОГИКА ПО РЕЖИМУ (без дублирования записей)
    use_records = []
    regime_discount = 1.0
    regime_info = ""

    if regime:
        regime_records = [r for r in closed if r.get("regime", "unknown") == regime]

        if len(regime_records) >= 10:
            # ✅ Достаточно данных по режиму — используем только их
            use_records = regime_records
            regime_info = f"regime={regime} ({len(regime_records)} signals)"
        elif len(regime_records) >= 5:
            # ⚠️ Мало данных — смешиваем режим + НЕ-режим с дисконтом
            non_regime = [r for r in closed if r.get("regime", "unknown") != regime]
            use_records = regime_records + non_regime
            regime_discount = 0.85
            regime_info = f"regime={regime} (mixed, {len(regime_records)} regime + {len(non_regime)} other)"
        else:
            # ❌ Недостаточно данных — используем все с дисконтом
            use_records = closed
            regime_discount = 0.75
            regime_info = f"regime={regime} (fallback, {len(regime_records)} regime signals)"
    else:
        use_records = closed
        regime_info = "no regime filter"

    recent = use_records[-SIGNAL_HISTORY_LIMIT:]
    weighted_mfes = _extract_weighted_mfes(recent)

    if not weighted_mfes:
        return round(fallback_tp, 4)

    expanded = _build_weighted_sample(weighted_mfes)

    active_pct = _cfg.SAFE_TP_PERCENTILE if _cfg.USE_SAFE_TP else _cfg.TP_PERCENTILE
    tp_pct = float(np.percentile(expanded, active_pct * 100))

    # Применяем дисконт режима
    tp_pct *= regime_discount

    # 🛡️ ATR-кап
    if atr14 is not None:
        try:
            atr_val = float(atr14.iloc[-1]) if hasattr(atr14, 'iloc') else float(atr14)
        except (TypeError, ValueError, AttributeError):
            atr_val = 0.0

        if atr_val > 0:
            atr_tp_pct = (atr_val * 2 / entry) * 100
            tp_pct = min(tp_pct, atr_tp_pct)
            logger.debug(f"[TP] ATR cap 2x: {tp_pct:.2f}% → capped at {atr_tp_pct:.2f}%")

    tp_pct = max(_cfg.MIN_TP_PCT, min(_cfg.MAX_TP_PCT, tp_pct))

    tp = entry * (1 + tp_pct / 100) if side == "long" else entry * (1 - tp_pct / 100)

    logger.info(f"[TP] {ticker} {tf} {side}: {tp_pct:.2f}% | {regime_info} | expanded: {len(expanded)}")
    return round(tp, 4)

def calculate_combined_tp(
    ticker: str,
    tf: str,
    side: str,
    entry: float,
    sl: float,
    df,
    idx: int,
    atr14,
    regime: Optional[str] = None
) -> Tuple[float, str]:
    """Комбинированный TP: адаптивный (гибридный) + R:R 2.0 как fallback."""
    stats = get_signal_stats(ticker, tf, side, regime)
    tp = calculate_adaptive_tp(ticker, tf, side, entry, sl, atr14, regime)
    risk = abs(entry - sl)
    rr = round(abs(tp - entry) / max(risk, 1e-8), 2)

    mode_label   = "SAFE" if _cfg.USE_SAFE_TP else "AGGR"
    regime_label = f" | Regime: {regime}" if regime else ""

    if stats["count"] >= 5:
        active_pct = _cfg.SAFE_TP_PERCENTILE if _cfg.USE_SAFE_TP else _cfg.TP_PERCENTILE
        desc = f"📚 Adaptive {active_pct:.0%} %ile [{mode_label}] | {stats['count']} signals{regime_label}"
    else:
        desc = f"📐 Fallback R:R 2.0 (only {stats['count']} signals){regime_label}"

    return tp, desc
