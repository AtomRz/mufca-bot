import json
import numpy as np
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone
import logging

import config as _cfg
from config import (
    SIGNALS_HISTORY_FILE,
    SIGNAL_HISTORY_LIMIT,
    BOT_STATE_FILE,
    safe_json_load,
    safe_json_save,
)

logger = logging.getLogger(__name__)

# =====================================================================
# 💾  СНАПШОТ ЖИВОГО СОСТОЯНИЯ (bot.state) — активные позиции переживают рестарт
# =====================================================================
def save_bot_state(state: Dict[str, Dict[str, dict]]):
    """Пишет снапшот всего bot.state (ticker -> tf -> state dict) на диск.
    Вызывается периодически (каждый скан) и на graceful shutdown (SIGTERM/SIGINT,
    см. main.py), чтобы активные позиции (a_active_trade/u_active_trade — entry,
    TP1/TP2, SL, tp1_hit) переживали рестарт контейнера, а не терялись как раньше.
    signals_history.json — это отдельный аудиторский журнал, он и без этого
    переживал рестарт; данный файл — именно "что бот сейчас реально отслеживает
    как открытое"."""
    try:
        safe_json_save(BOT_STATE_FILE, state)
    except Exception as e:
        logger.error(f"[STATE] Failed to save state snapshot: {e}", exc_info=True)


def load_bot_state() -> Dict[str, Dict[str, dict]]:
    """Читает снапшот с диска. Пустой dict, если файла нет (первый запуск) или
    он повреждён — в этом случае вызывающий код (bot.py) достроит state заново
    через make_state() для отсутствующих ticker/tf, как и раньше."""
    try:
        return safe_json_load(BOT_STATE_FILE, {})
    except Exception as e:
        logger.error(f"[STATE] Failed to load state snapshot, starting fresh: {e}", exc_info=True)
        return {}


def reconcile_orphaned_signals(state: Dict[str, Dict[str, dict]]):
    """Помечает как 'cancelled' любые exit_type='open' записи в signals_history.json,
    для которых нет соответствующего active_trade в переданном (восстановленном) state.

    Зачем: до появления save_bot_state()/load_bot_state() каждый рестарт контейнера
    полностью стирал bot.state (make_state() с нуля), но записи в signals_history.json
    оставались висеть с exit_type="open" навсегда — бот больше их не отслеживал, TP/SL
    для них никогда не проверялся, и они просто зависали "открытыми" без надежды на
    честный exit_type. Тот же сценарий в принципе возможен и сейчас в узком окне (если
    контейнер упадёт не-gracefully ровно между открытием сигнала и первым сохранением
    снапшота) — поэтому это не разовая миграция, а постоянная проверка при каждом
    старте. cancelled — честная маркировка "не знаем как закрылось", а не притворство,
    что знаем реальный tp/sl-исход."""
    history = load_signals_history()
    changed = False

    for ticker, tfs in history.items():
        for tf, sides in tfs.items():
            for side, records in sides.items():
                for rec in records:
                    if rec.get("exit_type") != "open":
                        continue
                    track = rec.get("track", "a")
                    if track in ("sim",):
                        continue  # синтетика !sim к живому bot.state не относится

                    active = state.get(ticker, {}).get(tf, {}).get(f"{track}_active_trade")
                    matches_active = (
                        active is not None
                        and active.get("side") == side
                        and abs(float(active.get("entry", -1)) - float(rec.get("entry", -2))) < 1e-9
                    )
                    if not matches_active:
                        rec["exit_type"] = "cancelled"
                        rec["exit"] = None
                        changed = True
                        logger.warning(
                            f"[RECONCILE] Orphaned open signal marked cancelled: "
                            f"{ticker} {tf} {side} track={track} entry={rec.get('entry')}"
                        )

    if changed:
        save_signals_history(history)

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


def normalize_timestamp(timestamp) -> str:
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


def add_signal_record(
    ticker: str,
    tf: str,
    side: str,
    entry: float,
    timestamp,
    regime: str = "unknown",
    track: str = "a",
    synthetic: bool = False,
):
    """Добавляет запись о новом сигнале.

    🆕 FIX: записи теперь помечаются `track` ("a" | "u" | "sim" | ...).
    Раньше история хранилась только по ключу ticker/tf/side, и если A- и U-трек
    одновременно держали позицию в одну сторону на одном ticker/tf, в истории
    оказывалось две записи exit_type="open" без возможности их различить —
    update_signal_record/update_signal_mae_mfe закрывали/обновляли не ту сделку.
    `synthetic` — метка для записей, созданных не реальным сканером (например !sim),
    чтобы они не участвовали в калибровке adaptive TP/SL.
    """
    history = load_signals_history()
    _ensure_history_slot(history, ticker, tf)

    record = {
        "entry": round(entry, 4),
        "exit": None,
        "exit_type": "open",
        "bars_held": 0,
        "moved_pct": 0.0,
        "timestamp": normalize_timestamp(timestamp),
        "max_favorable_pct": 0.0,
        "max_adverse_pct": 0.0,
        "regime": regime,
        "track": track,
        "synthetic": synthetic,
    }

    history[ticker][tf][side].append(record)
    history[ticker][tf][side] = history[ticker][tf][side][-(SIGNAL_HISTORY_LIMIT * 3):]
    save_signals_history(history)
    logger.info(f"[SIGNAL] ADDED {side} signal for {ticker} {tf} @ {entry} | Track: {track} | Regime: {regime}")


def _find_open_record(records: List[Dict], track: str) -> Optional[Dict]:
    """
    Находит открытую запись для данного трека.

    🆕 FIX: сначала ищем запись с точным совпадением track (новые данные).
    Если не нашли — fallback на запись без поля track вообще (старые данные,
    записанные до этого фикса), чтобы не ломать существующую историю.
    Берём самую последнюю подходящую запись (reversed).
    """
    for rec in reversed(records):
        if rec.get("exit_type") == "open" and rec.get("track") == track:
            return rec
    for rec in reversed(records):
        if rec.get("exit_type") == "open" and "track" not in rec:
            return rec
    return None


def _pct_move(side: str, entry: float, price: float) -> float:
    """% движения цены в пользу позиции — общая формула для long/short."""
    return (price - entry) / entry * 100 if side == "long" else (entry - price) / entry * 100


def update_signal_record(
    ticker: str, tf: str, side: str, exit_price: float, exit_type: str, bars_held: int,
    track: str = "a", tp1_hit: bool = False, tp1_price: Optional[float] = None,
):
    """Закрывает открытый сигнал (для указанного трека).

    🆕 FIX: раньше PnL всегда считался наивно entry→exit_price по ВСЕЙ позиции.
    Но если TP1 уже был достигнут, по факту закрыто 50% с прибылью TP1, а SL
    на оставшиеся 50% Атом переносит в безубыток вручную (бот это делает только
    как уведомление, реальную позицию на бирже не трогает — см. bot.py). Значит
    итоговый result "sl" по СТАРОМУ SL для такой сделки в реальности никогда бы
    не наступил: цена сначала должна была откатиться через безубыток. Поэтому
    когда tp1_hit=True, PnL считаем как среднее между зафиксированной на TP1
    половиной и результатом второй половины — это и есть реальная экономика
    сделки, а не искажённая "закрыли всё по старому SL"."""
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        logger.warning(f"Cannot update signal: no history for {ticker} {tf}")
        return

    records = history[ticker][tf][side]
    rec = _find_open_record(records, track)
    if rec is None:
        logger.warning(f"No open signal found to close for {ticker} {tf} {side} (track={track})")
        return

    rec["exit"] = round(exit_price, 4)
    rec["exit_type"] = exit_type
    rec["bars_held"] = bars_held
    rec["tp1_hit"] = bool(tp1_hit)
    entry = rec["entry"]

    if tp1_hit and tp1_price is not None:
        tp1_leg_pct = _pct_move(side, entry, tp1_price)      # первые 50%, зафиксировано на TP1
        remainder_leg_pct = _pct_move(side, entry, exit_price)  # вторые 50%, закрыты позже (обычно безубыток или TP2)
        rec["moved_pct"] = round((tp1_leg_pct + remainder_leg_pct) / 2, 4)
    else:
        rec["moved_pct"] = round(_pct_move(side, entry, exit_price), 4)

    save_signals_history(history)
    logger.info(f"[SIGNAL] CLOSED {side} signal for {ticker} {tf} | Track: {track} | PnL: {rec['moved_pct']:.2f}% | "
                f"TP1 hit: {tp1_hit} | Regime: {rec.get('regime', 'unknown')}")


def update_signal_mae_mfe(ticker: str, tf: str, side: str, current_price: float, track: str = "a"):
    """
    Обновляет MFE/MAE для открытого сигнала указанного трека.

    🆕 FIX: раньше сохраняли на диск не чаще чем раз в save_every вызовов через
    функцию-атрибут-счётчик, общий на ВСЕ пары/tf/сторону сразу. history в памяти
    кэширован (_history_cache) и не терялся между вызовами, но при неожиданном
    завершении процесса (crash/OOM/`docker stop` без graceful shutdown, см. main.py)
    несохранённый прогресс по MFE/MAE между сохранениями пропадал. Вызывается редко
    (раз в скан на открытую позицию), atomic-write в safe_json_save дешёвый —
    троттлинг не нужен, сохраняем при каждом реальном изменении.
    """
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        return

    records = history[ticker][tf][side]
    rec = _find_open_record(records, track)

    if rec is not None:
        entry = rec["entry"]
        if side == "long":
            favorable = (current_price - entry) / entry * 100
            adverse = (entry - current_price) / entry * 100
        else:
            favorable = (entry - current_price) / entry * 100
            adverse = (current_price - entry) / entry * 100

        new_favorable = round(max(float(rec.get("max_favorable_pct", 0)), favorable), 4)
        new_adverse = round(max(float(rec.get("max_adverse_pct", 0)), adverse), 4)

        if new_favorable != rec.get("max_favorable_pct") or new_adverse != rec.get("max_adverse_pct"):
            rec["max_favorable_pct"] = new_favorable
            rec["max_adverse_pct"] = new_adverse
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
    # 🆕 FIX: синтетические записи (!sim) исключаются из статистики/калибровки —
    # они не отражают реальное поведение рынка и искажали перцентили.
    closed = [r for r in records if r["exit_type"] in ("tp", "sl", "cancelled") and not r.get("synthetic", False)]
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
    # 🆕 FIX: синтетические записи (!sim) исключаются — см. get_signal_stats.
    closed = [r for r in records if r["exit_type"] in ("tp", "sl", "cancelled") and not r.get("synthetic", False)]

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
) -> Tuple[float, float, str]:
    """
    Комбинированный TP с двумя уровнями:
      TP1 — статистический (перцентиль MFE без RR cap), цель для 50% позиции
      TP2 — с RR cap (минимум R:R 1.5), цель для оставшихся 50%

    Returns: (tp1, tp2, desc)
    """
    stats = get_signal_stats(ticker, tf, side, regime)
    risk = abs(entry - sl)
    mode_label = "SAFE" if _cfg.USE_SAFE_TP else "AGGR"
    regime_label = f" | Regime: {regime}" if regime else ""

    # ── TP1: чистый статистический, без RR cap ───────────────────────
    tp1 = calculate_adaptive_tp(ticker, tf, side, entry, sl, atr14, regime)

    # ── TP2: с RR cap (минимум 1.5) ──────────────────────────────────
    min_rr_tp = entry + 1.5 * risk if side == "long" else entry - 1.5 * risk
    if side == "long":
        tp2 = max(tp1, min_rr_tp)
    else:
        tp2 = min(tp1, min_rr_tp)
    tp2 = round(tp2, 4)

    if stats["count"] >= 5:
        active_pct = _cfg.SAFE_TP_PERCENTILE if _cfg.USE_SAFE_TP else _cfg.TP_PERCENTILE
        hit_info = f" | Hit rate: {stats.get('tp_hit_rate', 0):.1%}" if stats.get('tp_hit_rate', 0) > 0 else ""
        desc = f"📚 Adaptive {active_pct:.0%} %ile [{mode_label}] | {stats['count']} signals{hit_info}{regime_label}"
    else:
        desc = f"📐 Fallback R:R 2.0 (only {stats['count']} signals){regime_label}"

    return tp1, tp2, desc
