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
    """Проверяет, что DataFrame содержит достаточно данных и не содержит NaN
    в ключевых OHLC-колонках.

    🆕 FIX: раньше проверялось только количество строк — если биржа изредка
    отдавала NaN в отдельных барах (сетевой глюк, неполный последний бар и
    т.п.), это NaN тихо просачивалось во все индикаторы дальше по цепочке
    (FRAMA, Andean, Heikin Ashi, volume delta — каждый по-своему ломался бы
    на NaN). Проверяем один раз здесь, на входе данных, вместо того чтобы
    защищаться от NaN в каждом индикаторе по отдельности."""
    if df.empty or len(df) < min_rows:
        return False
    if df[["open", "high", "low", "close"]].isna().any().any():
        return False
    return True

# =====================================================================
# 💵  ФОРМАТИРОВАНИЕ ЦЕНЫ (адаптивная точность)
# =====================================================================
def format_price(x: float) -> str:
    """Форматирует цену с числом знаков после запятой, зависящим от порядка
    величины, вместо фиксированного round(x, 2), который использовался почти
    везде в Discord-сообщениях/push (embeds.py, bot.py, discord_commands.py).

    🆕 FIX (TODO): на низкономинальных парах типа DOGE (~$0.08) фикс. 2 знака
    схлопывали Entry/SL/TP1/TP2 в одно и то же отображаемое число (все — просто
    "$0.08"), хотя внутри бот оперирует полной точностью и реально разными
    значениями — вводило в заблуждение при чтении сигнала/уведомления. Дело
    было не в вычислениях (там точность float всегда сохранялась), а именно в
    отображении. Всегда используем эту функцию вместо round(x, 2) для любой
    цены, которую видит пользователь (entry/sl/tp/tp1/tp2/exit)."""
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
