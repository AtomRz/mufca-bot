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
        "regime": regime,
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
        "tp_hit_rate": 0.0,
        "sl_hit_rate": 0.0,
        "avg_bars_held": 0,
    }

    if ticker not in history or tf not in history[ticker]:
        return empty

    records = history[ticker][tf][side]
    closed = [r for r in records if r["exit_type"] in ("tp", "sl", "cancelled")]
    if not closed:
        return empty

    # 🆕 FIX: Фильтрация по режиму, если указан
    regime_used = False
    if regime:
        regime_closed = [r for r in closed if r.get("regime", "unknown") == regime]
        if len(regime_closed) >= 5:
            closed = regime_closed
            regime_used = True
        else:
            logger.debug(f"[STATS] Only {len(regime_closed)} {regime} signals for {ticker} {tf} {side}, falling back to all {len(closed)} signals")

    recent = closed[-SIGNAL_HISTORY_LIMIT:]
    favorable_pcts = []
    tp_hits = 0
    sl_hits = 0
    bars_held_list = []

    for r in recent:
        mfe = r.get("max_favorable_pct", 0)
        if mfe == 0 and r.get("moved_pct", 0) != 0:
            mfe = abs(r["moved_pct"])
        favorable_pcts.append(max(mfe, 0.1))

        if r["exit_type"] == "tp":
            tp_hits += 1
        elif r["exit_type"] == "sl":
            sl_hits += 1

        bars = r.get("bars_held", 0)
        if bars > 0:
            bars_held_list.append(bars)

    total_exits = tp_hits + sl_hits

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
        "tp_hit_rate": round(tp_hits / total_exits, 3) if total_exits > 0 else 0.0,
        "sl_hit_rate": round(sl_hits / total_exits, 3) if total_exits > 0 else 0.0,
        "avg_bars_held": round(float(np.mean(bars_held_list)), 1) if bars_held_list else 0,
        "regime_applied": regime_used if regime else None,
        "regime": regime,
    }


# =====================================================================
# 🎯  АДАПТИВНЫЙ ТП (ГИБРИДНЫЙ: РЕЖИМ + ВЗВЕШИВАНИЕ + HIT RATE FEEDBACK)
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
    Максимум 2 копии на сигнал (tp=2, всё остальное=1) — не раздувает выборку.
    """
    expanded = []
    for mfe, weight in weighted_mfes:
        copies = 2 if weight >= 1.0 else 1
        expanded.extend([mfe] * copies)
    return expanded


def _calculate_hit_rate(records: List[Dict]) -> Tuple[float, int, int]:
    """
    Возвращает (tp_hit_rate, tp_count, total_exits) по записям.
    """
    tp_hits = sum(1 for r in records if r["exit_type"] == "tp")
    sl_hits = sum(1 for r in records if r["exit_type"] == "sl")
    total = tp_hits + sl_hits
    if total == 0:
        return 0.0, 0, 0
    return tp_hits / total, tp_hits, total


def _adjust_percentile_by_hit_rate(
    base_percentile: float,
    tp_hit_rate: float,
    target_hit_rate: float = 0.35,
    min_pct: float = 0.30,
    max_pct: float = 0.85,
) -> Tuple[float, str]:
    """
    Автоподстройка перцентиля на основе реального hit rate.

    Если hit rate ниже целевого — снижаем перцентиль (TP ближе, достижимее).
    Если hit rate выше — можно поднять (TP дальше, больше прибыли).

    Returns: (adjusted_percentile, reason)
    """
    if tp_hit_rate < target_hit_rate * 0.7:
        # Слишком мало TP hits — TP слишком агрессивный
        adjustment = -0.12
        reason = f"hit_rate={tp_hit_rate:.1%} < target, lowering pct {base_percentile:.0%} → {max(min_pct, base_percentile + adjustment):.0%}"
    elif tp_hit_rate < target_hit_rate * 0.9:
        # Немного ниже цели — небольшая корректировка
        adjustment = -0.06
        reason = f"hit_rate={tp_hit_rate:.1%} slightly low, adjusting pct {base_percentile:.0%} → {max(min_pct, base_percentile + adjustment):.0%}"
    elif tp_hit_rate > target_hit_rate * 1.5:
        # TP достигается слишком часто — можно быть агрессивнее
        adjustment = +0.08
        reason = f"hit_rate={tp_hit_rate:.1%} high, raising pct {base_percentile:.0%} → {min(max_pct, base_percentile + adjustment):.0%}"
    elif tp_hit_rate > target_hit_rate * 1.2:
        # Немного выше цели — небольшой буст
        adjustment = +0.04
        reason = f"hit_rate={tp_hit_rate:.1%} good, slight boost {base_percentile:.0%} → {min(max_pct, base_percentile + adjustment):.0%}"
    else:
        # В целевом диапазоне — не трогаем
        adjustment = 0.0
        reason = f"hit_rate={tp_hit_rate:.1%} in target zone, pct unchanged {base_percentile:.0%}"

    adjusted = max(min_pct, min(max_pct, base_percentile + adjustment))
    return adjusted, reason


def _apply_realistic_capture(mfe_pct: float, capture_rate: float = 0.70) -> float:
    """
    Применяет "realistic capture rate" к MFE.
    Идеальный MFE невозможно поймать — корректируем вниз.
    """
    return mfe_pct * capture_rate


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
    Адаптивный TP на основе исторических MFE с feedback loop по hit rate.

    Гибридная логика:
    1. Если по режиму ≥ 10 сигналов — используем только их
    2. Если 5-9 сигналов — используем режим + общие с дисконтом
    3. Если < 5 — используем все с взвешиванием по exit_type
    4. 🆕 Автоподстройка перцентиля на основе реального hit rate
    5. 🆕 Realistic capture rate (70% от идеального MFE)
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

    # 🆕 FIX: ГИБРИДНАЯ ЛОГИКА ПО РЕЖИМУ
    use_records = []
    regime_discount = 1.0
    regime_info = ""

    if regime:
        regime_records = [r for r in closed if r.get("regime", "unknown") == regime]

        if len(regime_records) >= 10:
            use_records = regime_records
            regime_info = f"regime={regime} ({len(regime_records)} signals)"
        elif len(regime_records) >= 5:
            non_regime = [r for r in closed if r.get("regime", "unknown") != regime]
            use_records = regime_records + non_regime
            regime_discount = 0.85
            regime_info = f"regime={regime} (mixed, {len(regime_records)} regime + {len(non_regime)} other)"
        else:
            use_records = closed
            regime_discount = 0.75
            regime_info = f"regime={regime} (fallback, {len(regime_records)} regime signals)"
    else:
        use_records = closed
        regime_info = "no regime filter"

    recent = use_records[-SIGNAL_HISTORY_LIMIT:]

    # 🆕 FIX: АВТОПОДСТРОЙКА ПЕРЦЕНТИЛЯ ПО HIT RATE
    base_percentile = _cfg.SAFE_TP_PERCENTILE if _cfg.USE_SAFE_TP else _cfg.TP_PERCENTILE
    adjusted_percentile = base_percentile
    hit_rate_info = ""

    # Только если достаточно данных для статистически значимой оценки
    if len(recent) >= 15 and _cfg.TP_AUTO_ADJUST:
        tp_hit_rate, tp_count, total_exits = _calculate_hit_rate(recent)
        adjusted_percentile, hit_rate_info = _adjust_percentile_by_hit_rate(
            base_percentile,
            tp_hit_rate,
            target_hit_rate=_cfg.TP_HIT_RATE_TARGET,
            min_pct=_cfg.TP_ADJUST_MIN_PCT,
            max_pct=_cfg.TP_ADJUST_MAX_PCT,
        )
        logger.info(f"[TP-HIT-RATE] {ticker} {tf} {side}: {hit_rate_info}")

    weighted_mfes = _extract_weighted_mfes(recent)

    if not weighted_mfes:
        return round(fallback_tp, 4)

    expanded = _build_weighted_sample(weighted_mfes)

    tp_pct = float(np.percentile(expanded, adjusted_percentile * 100))

    # Применяем дисконт режима
    tp_pct *= regime_discount

    # 🛡️ Realistic capture rate
    # Применяем capture rate ТОЛЬКО если regime_discount не срабатывал (нет дисконта)
    # иначе тройной дисконт (weighted + regime + capture) делает TP слишком близким
    if regime_discount >= 1.0:
        tp_pct = _apply_realistic_capture(tp_pct, capture_rate=_cfg.TP_CAPTURE_RATE)
    else:
        # При наличии regime_discount capture_rate смягчаем до среднего между 1.0 и capture_rate
        soft_capture = (_cfg.TP_CAPTURE_RATE + 1.0) / 2
        tp_pct = _apply_realistic_capture(tp_pct, capture_rate=soft_capture)
        logger.debug(f"[TP] Soft capture {soft_capture:.2f} (regime_discount={regime_discount})")

    # 🛡️ ATR-кап
    if atr14 is not None:
        try:
            atr_val = float(atr14.iloc[-1]) if hasattr(atr14, 'iloc') else float(atr14)
        except (TypeError, ValueError, AttributeError):
            atr_val = 0.0

        if atr_val > 0:
            atr_tp_pct = (atr_val * 2 / entry) * 100
            if tp_pct > atr_tp_pct:
                logger.debug(f"[TP] ATR cap 2x: {tp_pct:.2f}% → capped at {atr_tp_pct:.2f}%")
                tp_pct = atr_tp_pct

    tp_pct = max(_cfg.MIN_TP_PCT, min(_cfg.MAX_TP_PCT, tp_pct))

    tp = entry * (1 + tp_pct / 100) if side == "long" else entry * (1 - tp_pct / 100)

    logger.info(f"[TP] {ticker} {tf} {side}: {tp_pct:.2f}% | pct={adjusted_percentile:.0%} (base={base_percentile:.0%}) | {regime_info} | {hit_rate_info} | capture={'full' if regime_discount >= 1.0 else 'soft'}")
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

    mode_label = "SAFE" if _cfg.USE_SAFE_TP else "AGGR"
    regime_label = f" | Regime: {regime}" if regime else ""

    if stats["count"] >= 5:
        active_pct = _cfg.SAFE_TP_PERCENTILE if _cfg.USE_SAFE_TP else _cfg.TP_PERCENTILE
        hit_info = f" | Hit rate: {stats.get('tp_hit_rate', 0):.1%}" if stats.get('tp_hit_rate', 0) > 0 else ""
        desc = f"📚 Adaptive {active_pct:.0%} %ile [{mode_label}] | {stats['count']} signals{hit_info}{regime_label}"
    else:
        desc = f"📐 Fallback R:R 2.0 (only {stats['count']} signals){regime_label}"

    return tp, desc
