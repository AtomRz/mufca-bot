"""
spread.py — Order book spread collection & live liquidity gate for MUFCA Bot.

Works for both spot and futures (unlike derivatives.py, which is
futures-only) — bid/ask spread exists on any order book.

Structurally mirrors derivatives.py's OI-baseline pattern: a per-ticker
rolling history persisted to disk with a throttled write, plus a
clear_ticker_cache()/flush_*() pair for !remove and graceful shutdown.

Key difference from every other filter in this bot (Hurst, CHOP, FRAMA...):
those are all computed from OHLCV, so they can run identically inside
backtest_history() over past bars. Order book depth isn't part of OHLCV —
the bot only ever sees the spread at the moment it fetches the book. This
filter can therefore only gate the LIVE scan path; backtest_history() simply
never passes spread_info, so the gate is a structural no-op there (see the
SPREAD section in config.py for the full reasoning).

Warm-up-by-design: with config.ENABLE_SPREAD_FILTER off (the default),
bot.py still calls get_spread_snapshot() every scan tick, so the rolling
history keeps building in the background. Flipping the toggle on later
starts from an already-populated per-pair baseline instead of a cold start.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

import config as _cfg
from config import DATA_DIR, safe_json_load, safe_json_save
from derivatives import _to_swap_symbol

logger = logging.getLogger(__name__)

# =====================================================================
# 💾  ROLLING HISTORY (per-ticker, capped at SPREAD_HISTORY_MAX_SAMPLES)
# =====================================================================
_history: Dict[str, List[float]] = safe_json_load(_cfg.SPREAD_HISTORY_FILE, {})

_history_last_saved = 0.0
_history_dirty = False


def _save_history_throttled():
    global _history_last_saved, _history_dirty
    _history_dirty = True
    now = time.time()
    if now - _history_last_saved >= _cfg.SPREAD_HISTORY_SAVE_INTERVAL:
        safe_json_save(_cfg.SPREAD_HISTORY_FILE, _history)
        _history_last_saved = now
        _history_dirty = False


def flush_spread_history():
    """Forces an immediate save regardless of the throttle interval — call
    on graceful shutdown so the last in-memory history isn't lost."""
    global _history_last_saved, _history_dirty
    if _history_dirty:
        safe_json_save(_cfg.SPREAD_HISTORY_FILE, _history)
        _history_last_saved = time.time()
        _history_dirty = False


def clear_ticker_cache(ticker: str):
    """Drops one ticker's spread history (use with !remove) — otherwise a
    later !add re-add's rolling median starts from stale pre-removal data
    instead of a clean warm-up."""
    had_history = _history.pop(ticker, None) is not None
    if had_history:
        safe_json_save(_cfg.SPREAD_HISTORY_FILE, _history)
    logger.info(f"[SPREAD] Cleared history for {ticker}")


def clear_all_history():
    """Full reset (use with !reset_cache)."""
    _history.clear()
    safe_json_save(_cfg.SPREAD_HISTORY_FILE, {})
    logger.info("[SPREAD] Full history cleared")


def get_sample_count(ticker: str) -> int:
    return len(_history.get(ticker, []))


def _rolling_median(ticker: str) -> Optional[float]:
    samples = _history.get(ticker)
    if not samples:
        return None
    s = sorted(samples)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return s[mid]


def _record(ticker: str, spread_pct: float):
    samples = _history.setdefault(ticker, [])
    samples.append(spread_pct)
    if len(samples) > _cfg.SPREAD_HISTORY_MAX_SAMPLES:
        del samples[: len(samples) - _cfg.SPREAD_HISTORY_MAX_SAMPLES]
    _save_history_throttled()


# =====================================================================
# 📡  LIVE FETCH
# =====================================================================
async def get_spread_snapshot(exchange, ticker: str) -> Optional[Dict]:
    """Fetches the current top-of-book spread for `ticker`, records it into
    the rolling history (always — regardless of ENABLE_SPREAD_FILTER, so the
    filter can warm up in the background before it's switched on), and
    returns a dict ready to hand to signals.check_signals() as spread_info.

    Returns None on fetch failure (network hiccup, symbol not listed, etc.)
    — the caller should treat that the same as "no data", not as a hard
    stop; a single bad book fetch shouldn't block a whole scan cycle."""
    # 🆕 FIX: Gate.io's ccxt instance loads spot and swap markets together
    # even with options.defaultType='swap' — market(symbol) resolves the
    # plain spot-format "BTC/USDT" as a literal dict key hit against the
    # *spot* market (that lookup succeeds unconditionally and doesn't even
    # consult defaultType; the type-disambiguation path only runs for
    # exchange-native IDs, not already-unified symbols). Silently fetching
    # the spot order book while MARKET_MODE == "futures" doesn't raise —
    # it just feeds the futures signal gate numbers from the wrong market.
    # Same swap-symbol requirement as derivatives.py's funding rate/OI
    # fetches; reuse the same conversion for consistency.
    fetch_ticker = _to_swap_symbol(ticker) if _cfg.MARKET_MODE == "futures" else ticker
    try:
        book = await asyncio.to_thread(exchange.fetch_order_book, fetch_ticker, 5)
    except Exception as e:
        logger.warning(f"[SPREAD] fetch_order_book failed for {ticker}: {e}")
        return None

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    # 🆕 FIX: `not bids`/`not asks` only guards against an empty outer list.
    # An inner empty entry (bids == [[]]) passes that check but then
    # bids[0][0] raises IndexError, which was uncaught here and would have
    # taken down the whole scan cycle for this ticker.
    if not bids or not asks or not bids[0] or not asks[0]:
        return None

    # 🆕 FIX: ccxt's own parsing normally already returns floats, but this
    # value flows straight into JSON (safe_json_save) and arithmetic below —
    # an explicit cast is cheap insurance against Decimal/numpy/str leaking
    # through from an exchange response and blowing up json.dump or silently
    # string-concatenating instead of adding.
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
    except (TypeError, ValueError):
        return None
    if best_bid <= 0 or best_ask <= 0:
        return None

    mid = (best_bid + best_ask) / 2.0
    spread_pct = (best_ask - best_bid) / mid

    _record(ticker, spread_pct)

    return {
        "spread_pct": spread_pct,
        "rolling_median": _rolling_median(ticker),
        "sample_count": len(_history.get(ticker, [])),
        "bid": best_bid,
        "ask": best_ask,
    }


def evaluate_spread_gate(spread_info: Optional[Dict], sl_distance_pct: float) -> Dict:
    """Applies the two gate conditions described in config.py's SPREAD
    section to one already-fetched snapshot + this signal's own SL distance.

    Always returns a full diagnostic dict (used by !spread for a live
    preview even while the toggle is off) — `blocked` only actually matters
    to check_signals() when config.ENABLE_SPREAD_FILTER is True."""
    if not spread_info or spread_info.get("spread_pct") is None:
        return {"blocked": False, "reason": "no_data"}

    spread_pct = spread_info["spread_pct"]
    rolling_median = spread_info.get("rolling_median")
    sample_count = spread_info.get("sample_count", 0)

    eats_sl_pct = (spread_pct / sl_distance_pct) if sl_distance_pct > 0 else 0.0
    eats_too_much = eats_sl_pct > _cfg.SPREAD_SL_EAT_MAX_PCT

    is_anomaly = (
        rolling_median is not None
        and rolling_median > 0
        and sample_count >= _cfg.SPREAD_MIN_SAMPLES_FOR_ANOMALY
        and spread_pct > _cfg.SPREAD_ANOMALY_MULT * rolling_median
    )

    blocked = eats_too_much or is_anomaly
    reason = None
    if eats_too_much and is_anomaly:
        reason = "eats_sl_and_anomaly"
    elif eats_too_much:
        reason = "eats_sl"
    elif is_anomaly:
        reason = "anomaly"

    return {
        "blocked": blocked,
        "reason": reason,
        "spread_pct": spread_pct,
        "eats_sl_pct": eats_sl_pct,
        "rolling_median": rolling_median,
        "sample_count": sample_count,
        "warmed_up": sample_count >= _cfg.SPREAD_MIN_SAMPLES_FOR_ANOMALY,
    }
