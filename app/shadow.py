"""Shadow signals — "what would this blocked entry have done?"

When a track/side is switched off in Settings → Entry (config.ENTRY_BLOCK),
the signal is no longer opened, so it would normally leave no trace and its
quality could never be re-checked on live data. Instead the signal is run
through the same SL/TP/R:R/spread pipeline as a real one (signals.open_position
with dry_run=True) and, if it would have opened, stored here as a *shadow*
record: entry/SL/TP1/TP2 plus the confidence components at that moment. Its
outcome is then replayed from the closed bars that follow.

Deliberately NOT stored in signals_history.json: that file feeds adaptive
TP/SL calibration, hit-rate and the History tab, and a trade that was never
actually taken must not train any of them. The shadow file has the same
{ticker: {tf: {side: [record, ...]}}} shape, so state.analyze_confidence_
components() can read it (include_shadow=True) and report shadow rows
separately from live ones — never pooled.

Outcome model = backtest_history()'s closed-bar replay (same SL-before-TP2
order, same SAME_BAR_TP1_POLICY, TP1 credits half and moves the stop per
TP1_SL_FRACTION, a trade unresolved after MAX_HOLD_BARS is "cancelled" at that
bar's close). It is a closed-bar model: a wick the bot would have caught via the
live ticker poll or the 1m intrabar check is seen here only through its bar's
high/low. A shadow record therefore tracks the backtest, not a real fill.

A shadow position also respects a one-position-per-track rule and the usual
COOLDOWN_BARS after its exit, so shadow samples are spaced like real trades
rather than a signal firing on every consecutive bar.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import config as _cfg
from config import safe_json_load, safe_json_save
from utils import round_price

logger = logging.getLogger(__name__)

SHADOW_FILE = os.path.join(_cfg.DATA_DIR, "shadow_signals.json")
SHADOW_MAX_PER_SIDE = 300  # oldest records beyond this are dropped per ticker/tf/side

# (timestamp_ms, high, low, close) of a CLOSED bar
Bar = Tuple[int, float, float, float]


def load_shadow_history() -> Dict:
    return safe_json_load(SHADOW_FILE, {})


def _save(history: Dict):
    safe_json_save(SHADOW_FILE, history)


def _pct(side: str, entry: float, price: float) -> float:
    return (price - entry) / entry * 100 if side == "long" else (entry - price) / entry * 100


def simulate_shadow(
    side: str,
    entry: float,
    sl: float,
    tp1: float,
    tp: float,
    bars: List[Bar],
    max_hold: int,
    tp1_moved_sl: Callable[[float, float, str], float],
    same_bar_tp1_policy: str = "tp1_first",
) -> Dict:
    """Replays `bars` (closed bars strictly AFTER the entry bar, oldest first)
    against one position. Pure function — no I/O, safe to call repeatedly
    from scratch each scan.

    Mirrors the per-bar logic of backtest_history() (see the long/short blocks
    there) with long/short folded into one set of conditions.

    Returns:
      exit_type   "tp" | "sl" | "sl_after_tp1" | "cancelled" | "open"
                  ("open" = not resolved and fewer than max_hold bars seen yet)
      exit, exit_bar_ts, bars_held, tp1_hit, moved_pct
      max_favorable_pct / max_adverse_pct   — up to the exit (like a live record)
      raw_mfe_pct / raw_mae_pct             — over ALL of the first max_hold
                                              bars, whatever the exit was
      done        True once max_hold bars have been seen (raw_* are final)
    """
    is_long = side == "long"
    bars = bars[:max_hold]

    cur_sl = sl
    tp1_reached = False
    resolved: Optional[str] = None
    exit_price = entry
    exit_ts: Optional[int] = None
    bars_held = 0
    max_fav = max_adv = 0.0
    raw_fav = raw_adv = 0.0

    for i, (ts, hi, lo, cl) in enumerate(bars, start=1):
        fav_price, adv_price = (hi, lo) if is_long else (lo, hi)
        fav = _pct(side, entry, fav_price)
        adv = -_pct(side, entry, adv_price)
        raw_fav = max(raw_fav, fav)
        raw_adv = max(raw_adv, adv)

        if resolved is not None:
            continue  # exit already decided; keep only extending raw_*

        bars_held = i
        max_fav = max(max_fav, fav)
        max_adv = max(max_adv, adv)

        touch_sl = (lo <= cur_sl) if is_long else (hi >= cur_sl)
        reach_tp1 = (hi >= tp1) if is_long else (lo <= tp1)
        reach_tp = (hi >= tp) if is_long else (lo <= tp)

        if not tp1_reached:
            if same_bar_tp1_policy == "tp1_first" and reach_tp1 and not reach_tp and touch_sl:
                # one bar crossed both TP1 and the original SL: TP1 credited
                # first, then the moved stop is what closes the rest
                tp1_reached = True
                cur_sl = tp1_moved_sl(entry, tp1, side)
                resolved, exit_price, exit_ts = "sl", cur_sl, ts
            elif touch_sl:
                resolved, exit_price, exit_ts = "sl", cur_sl, ts
            elif reach_tp:
                tp1_reached = True  # TP2 is beyond TP1: passed through it
                resolved, exit_price, exit_ts = "tp", tp, ts
            elif reach_tp1:
                # SL/TP2 are not re-checked on the SAME bar — only from the
                # next one (can't know whether the touch came early or late)
                tp1_reached = True
                cur_sl = tp1_moved_sl(entry, tp1, side)
        else:
            if touch_sl:
                resolved, exit_price, exit_ts = "sl", cur_sl, ts
            elif reach_tp:
                resolved, exit_price, exit_ts = "tp", tp, ts

    done = len(bars) >= max_hold

    if resolved == "tp":
        exit_type = "tp"
    elif resolved == "sl":
        exit_type = "sl_after_tp1" if tp1_reached else "sl"
    elif done and bars:
        exit_type = "cancelled"
        exit_price = bars[-1][3]
        exit_ts = bars[-1][0]
        bars_held = len(bars)
    else:
        exit_type = "open"

    moved_pct = 0.0
    if exit_type != "open":
        if tp1_reached:
            moved_pct = (_pct(side, entry, tp1) + _pct(side, entry, exit_price)) / 2
        else:
            moved_pct = _pct(side, entry, exit_price)

    return {
        "exit_type": exit_type,
        "exit": round_price(exit_price) if exit_type != "open" else None,
        "exit_bar_ts": exit_ts if exit_type != "open" else None,
        "bars_held": bars_held,
        "tp1_hit": tp1_reached,
        "moved_pct": round(moved_pct, 4),
        "max_favorable_pct": round(max_fav, 4),
        "max_adverse_pct": round(max_adv, 4),
        "raw_mfe_pct": round(raw_fav, 4),
        "raw_mae_pct": round(raw_adv, 4),
        "done": done,
    }


def shadow_gate_ok(ticker: str, tf: str, track: str, side: str, bar_time: int, tf_ms: int) -> bool:
    """Whether a new shadow position may open now — one position per track at
    a time (real flags: {track}_in_long/_in_short), plus COOLDOWN_BARS after
    the last exit on the same track+side, counted from the exit bar (what
    close_trade() arms the real cooldown from)."""
    slot = load_shadow_history().get(ticker, {}).get(tf, {})
    last_exit_same_side = None
    for s, recs in slot.items():
        for rec in recs:
            if rec.get("track") != track:
                continue
            if rec.get("exit_type") == "open":
                return False
            if s == side:
                if rec.get("entry_bar_ts") == bar_time:
                    return False
                ex = rec.get("exit_bar_ts")
                if ex is not None and (last_exit_same_side is None or ex > last_exit_same_side):
                    last_exit_same_side = ex
    if last_exit_same_side is not None and (bar_time - last_exit_same_side) < _cfg.COOLDOWN_BARS * tf_ms:
        return False
    return True


def add_shadow_record(
    ticker: str,
    tf: str,
    side: str,
    track: str,
    regime: str,
    entry: float,
    sl: float,
    tp1: float,
    tp: float,
    entry_bar_ts: int,
    confidence_components: Optional[Dict[str, float]],
):
    history = load_shadow_history()
    recs = history.setdefault(ticker, {}).setdefault(tf, {}).setdefault(side, [])
    recs.append({
        "shadow": True,
        "entry": round_price(entry),
        "sl": round_price(sl),
        "tp1": round_price(tp1),
        "tp": round_price(tp),
        "entry_bar_ts": int(entry_bar_ts),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "track": track,
        "synthetic": False,
        "confidence_components": confidence_components,
        "exit": None,
        "exit_type": "open",
        "exit_bar_ts": None,
        "bars_held": 0,
        "moved_pct": 0.0,
        "tp1_hit": False,
        "max_favorable_pct": 0.0,
        "max_adverse_pct": 0.0,
        "raw_mfe_pct": 0.0,
        "raw_mae_pct": 0.0,
        "done": False,
    })
    history[ticker][tf][side] = recs[-SHADOW_MAX_PER_SIDE:]
    _save(history)
    logger.info(
        f"[SHADOW] recorded {track.upper()}-{side.upper()} {ticker} {tf} @ {entry} "
        f"(regime={regime}, SL={sl}, TP1={tp1}, TP2={tp}) — blocked by Entry switch, tracking only"
    )


_WIN_EXIT_TYPES = ("tp", "sl_after_tp1")  # same definition as state.WIN_EXIT_TYPES


def summary() -> Dict:
    """Cheap overview for the web top-bar lamp (GET /api/shadow): how many
    shadow positions are open / closed, per track+side, and when the last one
    was recorded. Reads only shadow_signals.json."""
    by_key: Dict[str, Dict[str, int]] = {}
    open_n = closed_n = 0
    last_recorded: Optional[str] = None
    for _ticker, tfs in load_shadow_history().items():
        for _tf, sides in tfs.items():
            for side, recs in sides.items():
                for rec in recs:
                    b = by_key.setdefault(f"{rec.get('track', '?')}_{side}", {"open": 0, "closed": 0, "wins": 0})
                    if rec.get("exit_type") == "open":
                        b["open"] += 1
                        open_n += 1
                    else:
                        b["closed"] += 1
                        closed_n += 1
                        if rec.get("exit_type") in _WIN_EXIT_TYPES:
                            b["wins"] += 1
                    ts = rec.get("timestamp")
                    if ts and (last_recorded is None or ts > last_recorded):
                        last_recorded = ts
    return {"open": open_n, "closed": closed_n, "last_recorded": last_recorded, "by_key": by_key}


_OUTCOME_KEYS = (
    "exit_type", "exit", "exit_bar_ts", "bars_held", "tp1_hit", "moved_pct",
    "max_favorable_pct", "max_adverse_pct", "raw_mfe_pct", "raw_mae_pct", "done",
)


def resolve_shadow_records(
    ticker: str,
    tf: str,
    closed_bars: List[Bar],
    tp1_moved_sl: Callable[[float, float, str], float],
    tf_ms: int,
) -> int:
    """Re-replays every unfinished shadow record of this ticker/tf against the
    closed bars fetched this scan. Stateless per record (replays from the
    entry bar each time), so it is idempotent and survives restarts. Returns
    how many records changed.

    A record whose entry bar fell out of the fetched window (long downtime)
    can't be replayed faithfully and is dropped rather than guessed at."""
    history = load_shadow_history()
    slot = history.get(ticker, {}).get(tf)
    if not slot or not closed_bars:
        return 0

    first_ts = closed_bars[0][0]
    changed = 0
    for side in list(slot.keys()):
        kept = []
        for rec in slot[side]:
            if rec.get("done"):
                kept.append(rec)
                continue
            entry_ts = rec["entry_bar_ts"]
            if first_ts > entry_ts + tf_ms:
                logger.warning(f"[SHADOW] dropping {ticker} {tf} {side} record from bar {entry_ts}: outside the fetched window")
                changed += 1
                continue
            bars = [b for b in closed_bars if b[0] > entry_ts]
            res = simulate_shadow(
                side, rec["entry"], rec["sl"], rec["tp1"], rec["tp"], bars,
                _cfg.MAX_HOLD_BARS, tp1_moved_sl, _cfg.SAME_BAR_TP1_POLICY,
            )
            if any(rec.get(k) != res[k] for k in _OUTCOME_KEYS):
                if res["exit_type"] != "open" and rec.get("exit_type") == "open":
                    logger.info(
                        f"[SHADOW] {rec.get('track', '?').upper()}-{side.upper()} {ticker} {tf} resolved: "
                        f"{res['exit_type']} ({res['moved_pct']:+.2f}%, regime={rec.get('regime')})"
                    )
                rec.update({k: res[k] for k in _OUTCOME_KEYS})
                changed += 1
            kept.append(rec)
        slot[side] = kept

    if changed:
        _save(history)
    return changed
