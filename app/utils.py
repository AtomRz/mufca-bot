import asyncio
import time
import threading
import logging
from typing import Optional, List, Dict, Any
import ccxt
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# =====================================================================
# 🔄  SAFE FETCH WITH RETRIES
# =====================================================================
async def safe_fetch_ohlcv(
    exchange: ccxt.Exchange,
    ticker: str,
    timeframe: str,
    limit: int = 100,
    retries: int = 3
) -> List[List[float]]:
    """Safe fetch with exponential backoff on errors."""
    # Guard against an accidentally huge limit (typo, bad param) causing memory
    # pressure. Largest legitimate caller today is 900 bars; backtest_history()
    # calls exchange.fetch_ohlcv directly and isn't affected by this bound.
    limit = max(1, min(limit, 2000))
    for attempt in range(retries):
        try:
            return await asyncio.to_thread(
                exchange.fetch_ohlcv,
                ticker,
                timeframe,
                limit=limit
            )
        except ccxt.RateLimitExceeded:
            wait = 2 ** attempt
            logger.warning(f"Rate limit for {ticker} {timeframe}, waiting {wait}s")
            await asyncio.sleep(wait)
        except ccxt.BadSymbol:
            logger.error(f"Bad symbol: {ticker}")
            return []
        except Exception as e:
            logger.error(f"Fetch error {ticker} {timeframe}: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(1)
            else:
                logger.error(f"Fetch failed after {retries} retries for {ticker} {timeframe}, returning empty")
                return []
    return []

# =====================================================================
# 📊  OHLCV PARSING
# =====================================================================
def parse_ohlcv(bars: List[List[float]]) -> pd.DataFrame:
    """Converts OHLCV bars into a DataFrame."""
    if not bars:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    return pd.DataFrame(
        bars,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )

# =====================================================================
# 🔍  DATA VALIDATION
# =====================================================================
def validate_dataframe(df: pd.DataFrame, min_rows: int = 50) -> bool:
    """Checks that the DataFrame has enough rows, no NaNs in the key OHLC
    columns, and is internally consistent (sorted/unique timestamps,
    high/low actually bracket open/close, non-negative volume).

    🆕 FIX: previously this only checked row count — if the exchange
    occasionally returned NaN in a bar (network glitch, incomplete last bar,
    etc.), that NaN would silently leak into every indicator downstream
    (FRAMA, Andean, Heikin Ashi, volume delta — each would break on NaN in
    its own way). We validate once here, at the data entry point, instead of
    guarding against NaN separately in every indicator.

    🆕 FIX (external review, P2): the above only ever checked "is there a
    NaN", not "does this data make structural sense". A duplicated or
    out-of-order timestamp doesn't produce a NaN anywhere, but every rolling
    indicator (ATR, FRAMA, Andean, MFI, the B-track squeeze/breakout logic,
    cooldown bar-counting, the whole backtest) implicitly assumes strictly
    increasing, one-row-per-bar timestamps — silently treating a duplicate
    as if it were four separate consecutive bars, or a resorted-in-place gap
    as a real price jump. Same reasoning for high < max(open,close) or
    low > min(open,close): not NaN, but not a physically possible candle
    either, and every indicator here trusts high/low as the bar's true
    extremes. Reject rather than silently compute on data that doesn't
    represent what it claims to."""
    if df.empty or len(df) < min_rows:
        return False
    if df[["open", "high", "low", "close"]].isna().any().any():
        return False
    ts = df["timestamp"]
    if not ts.is_monotonic_increasing:
        return False
    if ts.duplicated().any():
        return False
    if (df["high"] < df[["open", "close"]].max(axis=1)).any():
        return False
    if (df["low"] > df[["open", "close"]].min(axis=1)).any():
        return False
    if "volume" in df.columns and (df["volume"] < 0).any():
        return False
    return True

# =====================================================================
# 💵  PRICE FORMATTING (adaptive precision)
# =====================================================================
def round_price(x: float, sig_figs: int = 6) -> float:
    """Rounds a price to a fixed number of significant figures instead of a
    fixed number of decimal places.

    FIX: state.py and signals.py used round(x, 4) everywhere for
    entry/exit/tp/tp1/tp2 storage and calculation. That's fine for BTC/ETH,
    but for any pair priced under $0.0001 (SHIB, PEPE, BONK, etc. — the
    tracked pair list is user-editable via web/Discord, not hardcoded) it
    collapses the price straight to 0.0, which then causes division-by-zero
    or garbage percentages in _pct_move() and the whole adaptive TP/SL
    calibration pipeline. Significant-figure rounding keeps consistent
    relative precision regardless of price magnitude."""
    if x == 0:
        return 0.0
    from math import log10, floor
    d = sig_figs - int(floor(log10(abs(x)))) - 1
    return round(x, d)


def format_price(x: float) -> str:
    """Formats a price with a number of decimal places that scales with its
    magnitude, instead of a fixed round(x, 2), which used to be used almost
    everywhere in Discord messages/push notifications (embeds.py, bot.py,
    discord_commands.py).

    🆕 FIX (TODO): on low-nominal pairs like DOGE (~$0.08), a fixed 2 decimal
    places collapsed Entry/SL/TP1/TP2 into the same displayed number (all of
    them just showed "$0.08"), even though internally the bot works with
    full precision and genuinely distinct values — this was misleading when
    reading a signal/notification. The issue was never in the calculations
    (float precision was always preserved there), only in the display.
    Always use this function instead of round(x, 2) for any price shown to
    the user (entry/sl/tp/tp1/tp2/exit)."""
    ax = abs(x)
    if ax == 0:
        decimals = 2
    elif ax >= 1:
        decimals = 2
    elif ax >= 0.01:
        decimals = 4
    elif ax >= 0.0001:
        decimals = 6
    else:
        decimals = 8
    return f"{x:,.{decimals}f}"

# =====================================================================
# ⏱️  CACHE TIMER
# =====================================================================
class Timer:
    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._data:
                return None
            data, timestamp = self._data[key]
            if (time.time() - timestamp) > self.ttl:
                del self._data[key]
                return None
            return data

    def set(self, key: str, value: Any):
        with self._lock:
            self._data[key] = (value, time.time())

    def clear(self):
        with self._lock:
            self._data.clear()
