"""
filter_stats.py - live entry-filter funnel counters for MUFCA Bot.

Answers "which filter is eating my signals?" from live data, without changing
any trading decision. For every raw A/U trigger that fires while its track is
free (not in a position, cooldown over, bar not attempted yet) check_signals()
calls record() once per bar with the list of ENABLED filters that rejected it
(empty list = passed every filter). Counts are kept per ticker/timeframe/
track/side:

    triggers    raw triggers seen
    passed      triggers no enabled filter rejected
    blocked_by  {filter: how many triggers that filter rejected}   (overlapping)
    sole        {filter: how many triggers ONLY that filter rejected}

"sole" is the number that matters for "did we overdo it": it is how many signals
would come back if that one filter were switched off. "passed" is counted before
the later gates (entry switches, shock guard, R:R/SL checks), so it is not the
number of trades opened.

Persisted to filter_stats.json with a throttled write (same pattern as the
spread history); flush() is called on graceful shutdown.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import config as _cfg
from config import safe_json_load, safe_json_save

logger = logging.getLogger(__name__)

# Must match signals.FILTER_KEYS; duplicated here so this module has no
# dependency on signals.py (which imports it).
FILTER_KEYS = ("frama", "slope", "chop", "atr", "htf", "fake_break", "liq_sweep", "hurst")
TRACK_SIDES = ("a_long", "a_short", "u_long", "u_short")


def _empty_cell() -> Dict:
    return {"triggers": 0, "passed": 0,
            "blocked_by": {k: 0 for k in FILTER_KEYS}, "sole": {k: 0 for k in FILTER_KEYS}}


def _sanitize(raw) -> Dict:
    """Keeps only well-formed cells from a (possibly hand-edited) file."""
    out = {"since": None, "pairs": {}}
    if not isinstance(raw, dict):
        return out
    since = raw.get("since")
    out["since"] = since if isinstance(since, str) else None
    pairs = raw.get("pairs")
    if not isinstance(pairs, dict):
        return out
    for ticker, tfs in pairs.items():
        if not isinstance(tfs, dict):
            continue
        for tf, cells in tfs.items():
            if not isinstance(cells, dict):
                continue
            for key in TRACK_SIDES:
                cell = cells.get(key)
                if not isinstance(cell, dict):
                    continue
                clean = _empty_cell()
                for fld in ("triggers", "passed"):
                    v = cell.get(fld)
                    clean[fld] = v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0
                for fld in ("blocked_by", "sole"):
                    d = cell.get(fld)
                    if isinstance(d, dict):
                        for k in FILTER_KEYS:
                            v = d.get(k)
                            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                                clean[fld][k] = v
                out["pairs"].setdefault(str(ticker), {}).setdefault(str(tf), {})[key] = clean
    return out


_stats: Dict = _sanitize(safe_json_load(_cfg.FILTER_STATS_FILE, {}))
if _stats["since"] is None:
    _stats["since"] = datetime.now(timezone.utc).isoformat()

# (ticker, tf, track_side) -> last bar_time already counted, so the repeated
# ~15-60 s scans of one bar (A-track keeps its trigger for the whole bar) count once.
_last_bar: Dict[Tuple[str, str, str], int] = {}

_last_saved = 0.0
_dirty = False


def _save_throttled() -> None:
    global _last_saved, _dirty
    _dirty = True
    now = time.time()
    if now - _last_saved >= _cfg.FILTER_STATS_SAVE_INTERVAL:
        safe_json_save(_cfg.FILTER_STATS_FILE, _stats)
        _last_saved = now
        _dirty = False


def flush() -> None:
    """Immediate save regardless of the throttle - call on graceful shutdown."""
    global _last_saved, _dirty
    if _dirty:
        safe_json_save(_cfg.FILTER_STATS_FILE, _stats)
        _last_saved = time.time()
        _dirty = False


def record(ticker: str, tf: str, track: str, side: str, bar_time: int, failing: List[str]) -> None:
    """Counts one raw trigger (once per bar). `failing` = enabled filters that
    rejected it; empty = it passed all of them. Never raises."""
    try:
        key = f"{track}_{side}"
        if key not in TRACK_SIDES:
            return
        dedupe = (ticker, tf, key)
        if _last_bar.get(dedupe) == bar_time:
            return
        _last_bar[dedupe] = bar_time

        cell = _stats["pairs"].setdefault(ticker, {}).setdefault(tf, {}).setdefault(key, _empty_cell())
        cell["triggers"] += 1
        if not failing:
            cell["passed"] += 1
        for name in failing:
            if name in cell["blocked_by"]:
                cell["blocked_by"][name] += 1
        if len(failing) == 1 and failing[0] in cell["sole"]:
            cell["sole"][failing[0]] += 1
        _save_throttled()
    except Exception as e:  # counting must never be able to break a scan
        logger.warning(f"[FILTER_STATS] record failed for {ticker} {tf} {track}-{side}: {e}")


def aggregate(ticker: Optional[str] = None, tf: Optional[str] = None) -> Dict:
    """Sums the counters over the requested scope (default: every currently
    tracked pair and timeframe). Returns {"since", "scope", "cells": {"a_long": cell, ...}}."""
    tickers = [ticker] if ticker else list(_cfg.TICKERS)
    tfs_filter = [tf] if tf else None
    cells = {k: _empty_cell() for k in TRACK_SIDES}
    for t in tickers:
        for f, per_tf in _stats["pairs"].get(t, {}).items():
            if tfs_filter is not None and f not in tfs_filter:
                continue
            for key, cell in per_tf.items():
                agg = cells[key]
                agg["triggers"] += cell["triggers"]
                agg["passed"] += cell["passed"]
                for k in FILTER_KEYS:
                    agg["blocked_by"][k] += cell["blocked_by"][k]
                    agg["sole"][k] += cell["sole"][k]
    return {"since": _stats["since"], "scope": {"tickers": tickers, "tf": tf}, "cells": cells}


def reset() -> None:
    """Starts the counters over (e.g. after changing filter toggles/thresholds,
    so the new numbers describe the new setup only)."""
    global _last_saved, _dirty
    _stats["pairs"] = {}
    _stats["since"] = datetime.now(timezone.utc).isoformat()
    _last_bar.clear()
    safe_json_save(_cfg.FILTER_STATS_FILE, _stats)
    _last_saved = time.time()
    _dirty = False
    logger.info("[FILTER_STATS] counters reset")
