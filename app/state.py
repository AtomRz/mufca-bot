import json
import threading
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
from utils import round_price

logger = logging.getLogger(__name__)

# =====================================================================
# 💾  LIVE STATE SNAPSHOT (bot.state) — active positions survive a restart
# =====================================================================
def save_bot_state(state: Dict[str, Dict[str, dict]]):
    """Writes a snapshot of the whole bot.state (ticker -> tf -> state dict)
    to disk. Called periodically (every scan) and on graceful shutdown
    (SIGTERM/SIGINT, see main.py), so active positions (a_active_trade/
    u_active_trade — entry, TP1/TP2, SL, tp1_hit) survive a container
    restart instead of being lost like before. signals_history.json is a
    separate audit log and already survived restarts on its own; this file
    is specifically "what the bot currently considers open"."""
    try:
        safe_json_save(BOT_STATE_FILE, state)
    except Exception as e:
        logger.error(f"[STATE] Failed to save state snapshot: {e}", exc_info=True)


def load_bot_state() -> Dict[str, Dict[str, dict]]:
    """Reads the snapshot from disk. Returns an empty dict if the file
    doesn't exist (first run) or is corrupted — in that case the caller
    (bot.py) rebuilds state from scratch via make_state() for any missing
    ticker/tf, same as before."""
    try:
        return safe_json_load(BOT_STATE_FILE, {})
    except Exception as e:
        logger.error(f"[STATE] Failed to load state snapshot, starting fresh: {e}", exc_info=True)
        return {}


def reconcile_orphaned_signals(state: Dict[str, Dict[str, dict]]):
    """Marks any exit_type='open' record in signals_history.json as
    'cancelled' if there's no corresponding active_trade in the passed-in
    (restored) state.

    Why: before save_bot_state()/load_bot_state() existed, every container
    restart wiped bot.state completely (make_state() from scratch), but
    records in signals_history.json stayed stuck at exit_type="open"
    forever — the bot no longer tracked them, TP/SL was never checked for
    them, and they just hung "open" with no honest exit_type ever recorded.
    The same scenario is technically still possible today in a narrow
    window (if the container crashes non-gracefully exactly between opening
    a signal and the first snapshot save) — so this isn't a one-off
    migration, it's a standing check on every startup. "cancelled" is an
    honest label for "we don't know how this closed", not a pretense that
    we know the real tp/sl outcome."""
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
                        continue  # !sim synthetic records don't map to live bot.state

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
# 💾  SIGNAL HISTORY MANAGEMENT
# =====================================================================

_history_cache: Optional[Dict] = None
# 🆕 FIX: backtest_history runs in a separate thread (via asyncio.to_thread),
# concurrently with the main event loop (market_scanner). Both read/write
# this same global cache without synchronization — threading.Lock (not
# asyncio.Lock; we need protection between actual OS threads, not just
# coroutines) closes the main risk: a non-atomic "check-then-create"/
# "reassign-and-save". A fully narrow scenario (clear_history_cache() firing
# exactly while a background backtest is running) is still possible, but
# needs a rare coincidence (a simultaneous !reset and a background
# backtest) — not guarding against that separately, disproportionate
# complexity for a personal bot with infrequent add/reset.
_history_lock = threading.Lock()


def clear_history_cache():
    """Resets the signal history cache — call after deleting the history file."""
    global _history_cache
    with _history_lock:
        _history_cache = None


def load_signals_history() -> Dict:
    """Loads the signal history from file."""
    global _history_cache
    with _history_lock:
        if _history_cache is not None:
            return _history_cache
        _history_cache = safe_json_load(SIGNALS_HISTORY_FILE, {})
        return _history_cache


def save_signals_history(history: Dict):
    """Saves the signal history to file."""
    global _history_cache
    with _history_lock:
        _history_cache = history
        safe_json_save(SIGNALS_HISTORY_FILE, history)


def _ensure_history_slot(history: Dict, ticker: str, tf: str):
    """Creates a slot for the pair/timeframe if it doesn't exist yet."""
    if ticker not in history:
        history[ticker] = {}
    if tf not in history[ticker]:
        history[ticker][tf] = {"long": [], "short": []}


def normalize_timestamp(timestamp) -> str:
    """Normalizes a timestamp into ISO format."""
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
    """Adds a record for a new signal.

    🆕 FIX: records are now tagged with `track` ("a" | "u" | "sim" | ...).
    Previously history was keyed only by ticker/tf/side, so if the A-track
    and U-track simultaneously held a position on the same side for the same
    ticker/tf, history would end up with two exit_type="open" records with
    no way to tell them apart — update_signal_record/update_signal_mae_mfe
    would close/update the wrong trade.
    `synthetic` marks records created outside the real scanner (e.g. !sim),
    so they're excluded from adaptive TP/SL calibration.
    """
    history = load_signals_history()
    _ensure_history_slot(history, ticker, tf)

    record = {
        "entry": round_price(entry),
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
    Finds the open record for the given track.

    🆕 FIX: first look for a record with an exact track match (new data).
    If not found — fall back to a record with no track field at all (old
    data written before this fix), so we don't break existing history.
    Takes the most recent matching record (reversed).
    """
    for rec in reversed(records):
        if rec.get("exit_type") == "open" and rec.get("track") == track:
            return rec
    for rec in reversed(records):
        if rec.get("exit_type") == "open" and "track" not in rec:
            return rec
    return None


def _pct_move(side: str, entry: float, price: float) -> float:
    """% price move in favor of the position — shared formula for long/short."""
    return (price - entry) / entry * 100 if side == "long" else (entry - price) / entry * 100


def update_signal_record(
    ticker: str, tf: str, side: str, exit_price: float, exit_type: str, bars_held: int,
    track: str = "a", tp1_hit: bool = False, tp1_price: Optional[float] = None,
):
    """Closes an open signal (for the given track).

    🆕 FIX: PnL used to always be computed naively as entry→exit_price over
    the WHOLE position. But if TP1 was already hit, 50% was in fact closed
    at TP1's profit, and Atom manually moves the SL on the remaining 50% to
    breakeven (the bot only reflects this as a notification, it doesn't
    touch the actual exchange position — see bot.py). So the final "sl"
    result at the OLD SL for such a trade would, in reality, never have
    happened: price would have had to retrace through breakeven first. So
    when tp1_hit=True, we compute PnL as the average between the half
    locked in at TP1 and the result of the second half — that's the real
    economics of the trade, not a distorted "closed everything at the old
    SL"."""
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        logger.warning(f"Cannot update signal: no history for {ticker} {tf}")
        return

    records = history[ticker][tf][side]
    rec = _find_open_record(records, track)
    if rec is None:
        logger.warning(f"No open signal found to close for {ticker} {tf} {side} (track={track})")
        return

    rec["exit"] = round_price(exit_price)
    rec["exit_type"] = exit_type
    rec["bars_held"] = bars_held
    rec["tp1_hit"] = bool(tp1_hit)
    entry = rec["entry"]

    if tp1_hit and tp1_price is not None:
        tp1_leg_pct = _pct_move(side, entry, tp1_price)      # first 50%, locked in at TP1
        remainder_leg_pct = _pct_move(side, entry, exit_price)  # second 50%, closed later (usually breakeven or TP2)
        rec["moved_pct"] = round((tp1_leg_pct + remainder_leg_pct) / 2, 4)
    else:
        rec["moved_pct"] = round(_pct_move(side, entry, exit_price), 4)

    save_signals_history(history)
    logger.info(f"[SIGNAL] CLOSED {side} signal for {ticker} {tf} | Track: {track} | PnL: {rec['moved_pct']:.2f}% | "
                f"TP1 hit: {tp1_hit} | Regime: {rec.get('regime', 'unknown')}")


def update_signal_mae_mfe(ticker: str, tf: str, side: str, current_price: float, track: str = "a",
                           high: Optional[float] = None, low: Optional[float] = None):
    """
    Updates MFE/MAE for the open signal on the given track.

    🆕 FIX (Kimi review): previously this only accepted current_price (bar
    close) — live stats were computed from the closing price, while
    backtest_history() computes MAE/MFE from each bar's high/low (actual
    intrabar extremes) at the same time. Both samples get written into the
    same signals_history.json and jointly calibrate
    calculate_adaptive_sl/calculate_combined_tp — the methodology mismatch
    systematically understated live MAE/MFE relative to backtest. high/low
    are now optional and backward compatible: old call sites (e.g. !sim,
    where current_price is already the target TP/SL price itself, not a
    bar) still use a single current_price for both sides of the
    calculation, same as before.
    """
    history = load_signals_history()
    if ticker not in history or tf not in history[ticker]:
        return

    records = history[ticker][tf][side]
    rec = _find_open_record(records, track)

    if rec is not None:
        entry = rec["entry"]
        fav_price = (high if high is not None else current_price) if side == "long" else (low if low is not None else current_price)
        adv_price = (low if low is not None else current_price) if side == "long" else (high if high is not None else current_price)

        if side == "long":
            favorable = (fav_price - entry) / entry * 100
            adverse = (entry - adv_price) / entry * 100
        else:
            favorable = (entry - fav_price) / entry * 100
            adverse = (adv_price - entry) / entry * 100

        new_favorable = round(max(float(rec.get("max_favorable_pct", 0)), favorable), 4)
        new_adverse = round(max(float(rec.get("max_adverse_pct", 0)), adverse), 4)

        if new_favorable != rec.get("max_favorable_pct") or new_adverse != rec.get("max_adverse_pct"):
            rec["max_favorable_pct"] = new_favorable
            rec["max_adverse_pct"] = new_adverse
            save_signals_history(history)


# =====================================================================
# 📊  SIGNAL STATISTICS
# =====================================================================

def get_signal_stats(ticker: str, tf: str, side: str, regime: Optional[str] = None) -> Dict:
    """Returns statistics for the given signal."""
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
    # 🆕 FIX: synthetic records (!sim) are excluded from stats/calibration —
    # they don't reflect real market behavior and were skewing the percentiles.
    # 🆕 FIX BUG-LO008: "sl_after_tp1" (TP1 gave profit, the remainder closed
    # at the moved SL) is also a real closed outcome and should count in
    # stats alongside "tp"/"sl"/"cancelled", not be dropped from the sample.
    closed = [r for r in records if r["exit_type"] in ("tp", "sl", "sl_after_tp1", "cancelled") and not r.get("synthetic", False)]
    if not closed:
        return empty

    # 🆕 FIX: filter by regime, if given
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
        elif r["exit_type"] == "sl_after_tp1":
            # 🆕 FIX BUG-LO008: TP1 already delivered profit on 50% of the
            # position, and the remainder closed at the moved SL
            # (breakeven/half_tp1) — that's a partial success, not a full
            # loss. Give it half weight on both sides instead of counting
            # it as a plain "sl" alongside a trade that never reached TP1
            # at all.
            tp_hits += 0.5
            sl_hits += 0.5

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
# 🎯  ADAPTIVE TP (HYBRID: REGIME + WEIGHTING + HIT RATE FEEDBACK)
# =====================================================================

def _extract_weighted_mfes(records: List[Dict]) -> List[Tuple[float, float]]:
    """
    Extracts MFE values weighted by exit type.
    Returns a list of (mfe, weight).
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
        elif exit_type == "sl_after_tp1":
            # 🆕 FIX BUG-LO008: TP1 was actually reached (confirms the level
            # was statistically justified), the remainder just didn't reach
            # TP2 — weightier than a plain "sl", but less than a full "tp".
            weight = 0.8
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
    Builds a weighted sample for the percentile calculation.
    Max 2 copies per signal (tp=2, everything else=1) — keeps the sample
    from ballooning.
    """
    expanded = []
    for mfe, weight in weighted_mfes:
        copies = 2 if weight >= 1.0 else 1
        expanded.extend([mfe] * copies)
    return expanded


def _calculate_hit_rate(records: List[Dict]) -> Tuple[float, int, int]:
    """
    Returns (tp_hit_rate, tp_count, total_exits) for the given records.

    🆕 FIX BUG-LO008: "sl_after_tp1" (TP1 gave profit on 50%, the remainder
    closed at the moved SL — breakeven/half_tp1) used to be counted as a
    plain "sl", even though the trade's overall PnL is usually positive.
    Now it gets half weight in both the hit count and the total — a partial
    success, not a full loss on par with a trade that never even reached
    TP1.
    """
    tp_hits = sum(1 for r in records if r["exit_type"] == "tp")
    sl_hits = sum(1 for r in records if r["exit_type"] == "sl")
    partial = sum(1 for r in records if r["exit_type"] == "sl_after_tp1")
    total = tp_hits + sl_hits + partial
    if total == 0:
        return 0.0, 0, 0
    weighted_hits = tp_hits + 0.5 * partial
    return weighted_hits / total, tp_hits, total


def _adjust_percentile_by_hit_rate(
    base_percentile: float,
    tp_hit_rate: float,
    target_hit_rate: float = 0.35,
    min_pct: float = 0.30,
    max_pct: float = 0.85,
) -> Tuple[float, str]:
    """
    Auto-adjusts the percentile based on the real hit rate.

    If the hit rate is below target — lower the percentile (TP closer,
    easier to reach). If it's above target — raise it (TP farther, more
    profit).

    Returns: (adjusted_percentile, reason)
    """
    if tp_hit_rate < target_hit_rate * 0.7:
        # Too few TP hits — TP is too aggressive
        adjustment = -0.12
        reason = f"hit_rate={tp_hit_rate:.1%} < target, lowering pct {base_percentile:.0%} → {max(min_pct, base_percentile + adjustment):.0%}"
    elif tp_hit_rate < target_hit_rate * 0.9:
        # Slightly below target — small adjustment
        adjustment = -0.06
        reason = f"hit_rate={tp_hit_rate:.1%} slightly low, adjusting pct {base_percentile:.0%} → {max(min_pct, base_percentile + adjustment):.0%}"
    elif tp_hit_rate > target_hit_rate * 1.5:
        # TP hit too often — can afford to be more aggressive
        adjustment = +0.08
        reason = f"hit_rate={tp_hit_rate:.1%} high, raising pct {base_percentile:.0%} → {min(max_pct, base_percentile + adjustment):.0%}"
    elif tp_hit_rate > target_hit_rate * 1.2:
        # Slightly above target — small boost
        adjustment = +0.04
        reason = f"hit_rate={tp_hit_rate:.1%} good, slight boost {base_percentile:.0%} → {min(max_pct, base_percentile + adjustment):.0%}"
    else:
        # Within target range — leave it alone
        adjustment = 0.0
        reason = f"hit_rate={tp_hit_rate:.1%} in target zone, pct unchanged {base_percentile:.0%}"

    adjusted = max(min_pct, min(max_pct, base_percentile + adjustment))
    return adjusted, reason


def _apply_realistic_capture(mfe_pct: float, capture_rate: float = 0.70) -> float:
    """
    Applies a "realistic capture rate" to MFE.
    The ideal MFE can never actually be captured in practice — adjust it down.
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
    Adaptive TP based on historical MFE with a hit-rate feedback loop.

    Hybrid logic:
    1. If there are ≥ 10 signals for this regime — use only those
    2. If 5-9 signals — use regime + overall with a discount
    3. If < 5 — use all signals weighted by exit_type
    4. 🆕 Auto-adjusts the percentile based on the real hit rate
    5. 🆕 Realistic capture rate (70% of the ideal MFE)
    """
    history = load_signals_history()
    risk = abs(entry - current_sl)
    fallback_tp = entry + (2.0 * risk) if side == "long" else entry - (2.0 * risk)

    if ticker not in history or tf not in history[ticker]:
        return round_price(fallback_tp)

    records = history[ticker][tf][side]
    # 🆕 FIX: synthetic records (!sim) are excluded — see get_signal_stats.
    # 🆕 FIX BUG-LO008: sl_after_tp1 is a real closed outcome, included in the sample.
    closed = [r for r in records if r["exit_type"] in ("tp", "sl", "sl_after_tp1", "cancelled") and not r.get("synthetic", False)]

    if len(closed) < 3:
        return round_price(fallback_tp)

    # 🆕 FIX: HYBRID LOGIC BY REGIME
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

    # 🆕 FIX: AUTO-ADJUST PERCENTILE BY HIT RATE
    base_percentile = _cfg.SAFE_TP_PERCENTILE if _cfg.USE_SAFE_TP else _cfg.TP_PERCENTILE
    adjusted_percentile = base_percentile
    hit_rate_info = ""

    # Only if there's enough data for a statistically meaningful estimate
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
        return round_price(fallback_tp)

    expanded = _build_weighted_sample(weighted_mfes)

    tp_pct = float(np.percentile(expanded, adjusted_percentile * 100))

    # Apply the regime discount
    tp_pct *= regime_discount

    # 🛡️ Realistic capture rate
    # Only apply the capture rate if regime_discount didn't kick in (no
    # discount) — otherwise a triple discount (weighted + regime + capture)
    # makes TP too close
    if regime_discount >= 1.0:
        tp_pct = _apply_realistic_capture(tp_pct, capture_rate=_cfg.TP_CAPTURE_RATE)
    else:
        # When regime_discount is active, soften capture_rate to the average
        # between 1.0 and capture_rate
        soft_capture = (_cfg.TP_CAPTURE_RATE + 1.0) / 2
        tp_pct = _apply_realistic_capture(tp_pct, capture_rate=soft_capture)
        logger.debug(f"[TP] Soft capture {soft_capture:.2f} (regime_discount={regime_discount})")

    # 🛡️ ATR cap
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
    return round_price(tp)


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
    Combined TP with two levels:
      TP1 — statistical (MFE percentile without an R:R cap), target for 50% of the position
      TP2 — with an R:R cap (minimum R:R 1.5), target for the remaining 50%

    Returns: (tp1, tp2, desc)
    """
    stats = get_signal_stats(ticker, tf, side, regime)
    risk = abs(entry - sl)
    mode_label = "SAFE" if _cfg.USE_SAFE_TP else "AGGR"
    regime_label = f" | Regime: {regime}" if regime else ""

    # ── TP1: purely statistical, no R:R cap ───────────────────────
    tp1 = calculate_adaptive_tp(ticker, tf, side, entry, sl, atr14, regime)

    # ── TP2: with R:R cap (minimum 1.5) ──────────────────────────────────
    min_rr_tp = entry + 1.5 * risk if side == "long" else entry - 1.5 * risk
    if side == "long":
        tp2 = max(tp1, min_rr_tp)
    else:
        tp2 = min(tp1, min_rr_tp)
    tp2 = round_price(tp2)

    if stats["count"] >= 5:
        active_pct = _cfg.SAFE_TP_PERCENTILE if _cfg.USE_SAFE_TP else _cfg.TP_PERCENTILE
        hit_info = f" | Hit rate: {stats.get('tp_hit_rate', 0):.1%}" if stats.get('tp_hit_rate', 0) > 0 else ""
        desc = f"📚 Adaptive {active_pct:.0%} %ile [{mode_label}] | {stats['count']} signals{hit_info}{regime_label}"
    else:
        desc = f"📐 Fallback R:R 2.0 (only {stats['count']} signals){regime_label}"

    return tp1, tp2, desc
