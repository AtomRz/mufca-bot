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
# 🔄  БЕЗОПАСНЫЙ FETCH С ПОВТОРАМИ
# =====================================================================
async def safe_fetch_ohlcv(
    exchange: ccxt.Exchange,
    ticker: str,
    timeframe: str,
    limit: int = 100,
    retries: int = 3
) -> List[List[float]]:
    """Безопасный fetch с экспоненциальной задержкой при ошибках."""
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
# 📊  ПАРСИНГ OHLCV
# =====================================================================
def parse_ohlcv(bars: List[List[float]]) -> pd.DataFrame:
    """Конвертирует OHLCV в DataFrame."""
    if not bars:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    return pd.DataFrame(
        bars,
        columns=["timestamp", "open", "high", "low", "close", "volume"]
    )

# =====================================================================
# 🔍  ВАЛИДАЦИЯ ДАННЫХ
# =====================================================================
def validate_dataframe(df: pd.DataFrame, min_rows: int = 50) -> bool:
    """Проверяет, что DataFrame содержит достаточно данных."""
    return not df.empty and len(df) >= min_rows

# =====================================================================
# ⏱️  ТАЙМЕР ДЛЯ КЭША
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
