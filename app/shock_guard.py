"""
Market shock guard.

Detects an abnormal price move over the last few closed bars, measured as a
multiple of the ATR taken *before* the move started. News-driven cascades
(exchange hacks, flash crashes) look like this, and the A/U tracks are not
built for them: they look for reversals and trend starts,
so during a cascade the bot usually stays silent without telling anyone why.

This module only records and reports the condition. check_signals() calls
update_shock() once per ticker/timeframe scan; bot.py raises the alerts;
web_api exposes the active shocks in /api/status. Entries are blocked only if
config.SHOCK_GUARD_BLOCK_ENTRIES is on (off by default - warn only).
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

import config as _cfg

logger = logging.getLogger(__name__)

# (ticker, timeframe) -> shock info for shocks that are currently active
_active_shocks: Dict[Tuple[str, str], Dict] = {}
# (ticker, timeframe) -> epoch seconds of the last alert that was handed out
_last_alert_ts: Dict[Tuple[str, str], float] = {}


def evaluate_shock(
    closes: List[float],
    atrs: List[float],
    idx: int,
    bars: int,
    atr_mult: float,
) -> Optional[Dict]:
    """Pure check: did the close at `idx` move >= atr_mult x ATR away from the
    close `bars` bars earlier?

    The ATR used is the one at the START of the window (idx - bars), so the
    move itself can't inflate its own yardstick. Returns None when there is
    not enough history, the ATR is unusable, or the move is not abnormal.
    """
    start = idx - bars
    if bars < 1 or start < 0 or idx >= len(closes) or idx >= len(atrs):
        return None
    start_close = float(closes[start])
    end_close = float(closes[idx])
    atr_start = float(atrs[start])
    # NaN-safe: NaN fails every comparison below, so it also returns None
    if not (atr_start > 0.0) or not (start_close > 0.0) or end_close != end_close:
        return None

    move = end_close - start_close
    move_atr = abs(move) / atr_start
    if move_atr < atr_mult:
        return None
    return {
        "direction": "down" if move < 0 else "up",
        "move_pct": round(move / start_close * 100, 2),
        "move_atr": round(move_atr, 1),
        "bars": bars,
        "start_close": start_close,
        "end_close": end_close,
    }


def update_shock(ticker: str, timeframe: str, bar_time: int, closes, atrs, idx: int) -> Optional[Dict]:
    """Evaluates the shock condition for one ticker/timeframe and records the
    result. Returns the active shock info, or None when there isn't one.

    Never raises: a failure here must not be able to break signal generation.
    """
    key = (ticker, timeframe)
    if not getattr(_cfg, "SHOCK_GUARD_ENABLED", False):
        _active_shocks.pop(key, None)
        return None
    try:
        info = evaluate_shock(closes, atrs, idx, _cfg.SHOCK_GUARD_BARS, _cfg.SHOCK_GUARD_ATR_MULT)
    except Exception as e:
        logger.warning(f"[SHOCK] {ticker} {timeframe}: evaluation failed: {e}")
        return _active_shocks.get(key)

    if info is None:
        if key in _active_shocks:
            logger.info(f"[SHOCK] {ticker} {timeframe}: back to normal")
        _active_shocks.pop(key, None)
        return None

    info["ticker"] = ticker
    info["timeframe"] = timeframe
    info["bar_time"] = bar_time
    previous = _active_shocks.get(key)
    info["since_ts"] = previous["since_ts"] if previous else time.time()
    _active_shocks[key] = info
    if previous is None:
        logger.warning(
            f"[SHOCK] {ticker} {timeframe}: {info['direction'].upper()} {info['move_pct']:+.2f}% "
            f"in {info['bars']} bars = {info['move_atr']}x ATR"
        )
    return info


def get_active_shock(ticker: str, timeframe: str) -> Optional[Dict]:
    return _active_shocks.get((ticker, timeframe))


def get_active_shocks() -> List[Dict]:
    """Snapshot of every active shock (for /api/status and alerts)."""
    return [dict(v) for v in _active_shocks.values()]


def pop_alerts() -> List[Dict]:
    """Active shocks that should be announced now. At most one alert per
    ticker/timeframe per SHOCK_GUARD_ALERT_COOLDOWN_SECONDS, so a cascade that
    lasts many bars doesn't spam the channel."""
    now = time.time()
    due = []
    for key, info in _active_shocks.items():
        if now - _last_alert_ts.get(key, 0.0) >= _cfg.SHOCK_GUARD_ALERT_COOLDOWN_SECONDS:
            _last_alert_ts[key] = now
            due.append(dict(info))
    return due


def format_shock_alert(info: Dict) -> str:
    arrow = "🔻" if info["direction"] == "down" else "🔺"
    action = (
        "New entries are **blocked** while the shock lasts."
        if getattr(_cfg, "SHOCK_GUARD_BLOCK_ENTRIES", False)
        else "Signals are **not blocked**, but A/U tracks are not built for news cascades — "
             "expect no (or late) signals and wide candles."
    )
    return (
        f"{arrow} **MARKET SHOCK** | `{info['ticker']}` `{info['timeframe']}` | "
        f"{info['move_pct']:+.2f}% in {info['bars']} bars (**{info['move_atr']}× ATR**)\n"
        f"{action}"
    )


def reset() -> None:
    """Clears all state (used by tests)."""
    _active_shocks.clear()
    _last_alert_ts.clear()
