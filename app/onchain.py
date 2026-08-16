"""
onchain.py — On-Chain анализ для MUFCA Bot
Источники:
  - Etherscan API: балансы биржевых ETH-адресов (реальные притоки/оттоки)
  - CoinGecko API: Fear & Greed, BTC dominance, volume change
Обновление: раз в час (кэш TTL = 3600с)
"""

import asyncio
import aiohttp
import logging
import os
import time
from typing import Dict, Optional, Any, Tuple

from config import (
    DATA_DIR,
    ETHERSCAN_API_KEY,
    COINGECKO_API_KEY,
    ONCHAIN_CACHE_TTL,
    ONCHAIN_FLOW_THRESHOLD_ETH,
    ONCHAIN_FLOW_THRESHOLD_LARGE_ETH,
    safe_json_load,
    safe_json_save,
)

logger = logging.getLogger(__name__)

# =====================================================================
# 🏦  БИРЖЕВЫЕ ETH-АДРЕСА (публично известные hot wallets)
# =====================================================================
EXCHANGE_ETH_ADDRESSES: Dict[str, str] = {
    "Binance":  "0x28C6c06298d514Db089934071355E5743bf21d60",
    "Coinbase": "0x71660c4005BA85c37ccec55d0C4493E66Fe775d3",
    "Kraken":   "0xE853c56864A2ebe4576a807D26Fdc4A0adA51919",
    "OKX":      "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b",
}

# =====================================================================
# 💾  КЭШ
# =====================================================================
_cache: Dict[str, Tuple[float, Any]] = {}   # key -> (timestamp, data)

# 🆕 FIX: baseline balances used to live in a plain in-memory dict and were lost
# on every container restart, forcing a fresh "first_run" and delaying flow
# analysis by a full ONCHAIN_CACHE_TTL cycle (~1h) each time. Persisted to disk
# so a restart doesn't reset the baseline.
_ONCHAIN_BASELINE_FILE = os.path.join(DATA_DIR, "onchain_baseline.json")
_prev_balances: Dict[str, Optional[float]] = safe_json_load(_ONCHAIN_BASELINE_FILE, {})


def _cache_get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < ONCHAIN_CACHE_TTL:
        return entry[1]
    return None

def _cache_set(key: str, value: Any):
    _cache[key] = (time.time(), value)

def clear_onchain_cache():
    """Сбрасывает TTL-кэш on-chain данных.

    BUGFIX BUG-HI004: ранее очищала и _prev_balances, из-за чего следующий вызов
    get_eth_flow_delta() снова попадал в ветку first_run (нет baseline → возвращает
    note='first_run' → bot.py вызывал clear_onchain_cache() → цикл бесконечно).
    Теперь _prev_balances НЕ сбрасывается — baseline сохраняется между сбросами кэша.
    Для полного сброса включая baseline используйте clear_onchain_cache_full().
    """
    _cache.clear()
    logger.info("[ONCHAIN] TTL cache cleared (baseline balances preserved)")

def clear_onchain_cache_full():
    """Полный сброс кэша включая baseline балансов (использовать только при !reset_cache)."""
    _cache.clear()
    _prev_balances.clear()
    # Иначе на следующем скане baseline подхватится обратно из файла на диске.
    safe_json_save(_ONCHAIN_BASELINE_FILE, {})
    logger.info("[ONCHAIN] Full cache cleared (including baseline balances)")

# =====================================================================
# 🔗  ETHERSCAN — балансы бирж
# =====================================================================

async def _fetch_eth_balance(session: aiohttp.ClientSession, address: str, retries: int = 3) -> Optional[float]:
    """Возвращает ETH баланс адреса (в ETH, не в Wei).

    BUGFIX BUG-ME002: Etherscan имеет rate limit 5 calls/second для бесплатных ключей.
    4 параллельных запроса через asyncio.gather могут вызвать 429 Too Many Requests.
    Добавлен retry с exponential backoff.
    """
    # 🆕 FIX: Etherscan deprecated V1 endpoint (2025+).
    # Мигрируем на V2 API: добавляем /v2/ и обязательный chainid=1 (Ethereum mainnet).
    url = (
        f"https://api.etherscan.io/v2/api"
        f"?chainid=1"
        f"&module=account&action=balance"
        f"&address={address}"
        f"&tag=latest"
        f"&apikey={ETHERSCAN_API_KEY}"
    )
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    # Rate limit — ждём и повторяем
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(f"[ONCHAIN] Etherscan 429 for {address[:10]}…, retrying in {wait}s (attempt {attempt+1}/{retries})")
                    await asyncio.sleep(wait)
                    continue
                data = await resp.json()
                if data.get("status") == "1":
                    return int(data["result"]) / 1e18   # Wei → ETH
                # status == "0" может быть rate limit или другая ошибка
                if data.get("message") == "NOTOK" and "rate limit" in str(data.get("result", "")).lower():
                    wait = 2 ** attempt
                    logger.warning(f"[ONCHAIN] Etherscan rate limit (message), retrying in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                # Ошибка но не rate limit — не retry
                logger.warning(f"[ONCHAIN] Etherscan error for {address[:10]}…: {data.get('message')} — {data.get('result')}")
                return None
        except asyncio.TimeoutError:
            logger.warning(f"[ONCHAIN] Etherscan timeout for {address[:10]}… (attempt {attempt+1}/{retries})")
            if attempt < retries - 1:
                await asyncio.sleep(1)
            else:
                return None
        except Exception as e:
            logger.warning(f"[ONCHAIN] Etherscan balance error {address[:10]}…: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(1)
            else:
                return None
    return None


async def get_exchange_balances() -> Dict[str, Optional[float]]:
    """
    Получает балансы всех биржевых адресов параллельно.
    Возвращает: {exchange_name: balance_eth}
    """
    cached = _cache_get("eth_balances")
    if cached is not None:
        return cached

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_fetch_eth_balance(session, addr) for addr in EXCHANGE_ETH_ADDRESSES.values()],
            return_exceptions=True,
        )

    balances = {}
    for name, result in zip(EXCHANGE_ETH_ADDRESSES.keys(), results):
        balances[name] = result if not isinstance(result, Exception) else None

    _cache_set("eth_balances", balances)
    logger.info(f"[ONCHAIN] ETH balances fetched: { {k: f'{v:.0f}' if v else 'N/A' for k, v in balances.items()} }")
    return balances


async def get_eth_flow_delta() -> Dict:
    """
    Сравнивает текущие балансы с предыдущими.
    Возвращает:
      delta_eth:   суммарная дельта по всем биржам (+ = приток на биржу, - = отток с биржи)
      flow:        "inflow" | "outflow" | "neutral"
      strength:    "large" | "normal" | "weak"
      per_exchange: детали по каждой бирже
    """
    curr_balances = await get_exchange_balances()

    # При первом запуске — сохраняем как базу и сразу делаем второй запрос через паузу
    if not _prev_balances:
        _prev_balances.update(curr_balances)
        safe_json_save(_ONCHAIN_BASELINE_FILE, _prev_balances)
        logger.info("[ONCHAIN] First run: saved baseline balances, delta will be available on next cycle.")
        return {
            "delta_eth": 0.0,
            "flow": "neutral",
            "strength": "weak",
            "per_exchange": {},
            "note": "first_run",
        }

    # Считаем дельту
    delta_total = 0.0
    per_exchange = {}

    for name in EXCHANGE_ETH_ADDRESSES:
        curr = curr_balances.get(name)
        prev = _prev_balances.get(name)
        if curr is not None and prev is not None:
            delta = curr - prev    # + = приток на биржу, - = отток с биржи
            delta_total += delta
            per_exchange[name] = round(delta, 2)

    # Обновляем предыдущие балансы для следующего цикла
    _prev_balances.update(curr_balances)
    safe_json_save(_ONCHAIN_BASELINE_FILE, _prev_balances)

    # Классифицируем
    abs_delta = abs(delta_total)
    if abs_delta >= ONCHAIN_FLOW_THRESHOLD_LARGE_ETH:
        strength = "large"
    elif abs_delta >= ONCHAIN_FLOW_THRESHOLD_ETH:
        strength = "normal"
    else:
        strength = "weak"

    flow = "inflow" if delta_total > 100 else "outflow" if delta_total < -100 else "neutral"

    logger.info(f"[ONCHAIN] ETH flow: {flow} | delta={delta_total:.0f} ETH | strength={strength}")

    return {
        "delta_eth": round(delta_total, 2),
        "flow": flow,
        "strength": strength,
        "per_exchange": per_exchange,
        "note": "ok",
    }

# =====================================================================
# 📊  COINGECKO — Fear & Greed, dominance, volume
# =====================================================================

def _fg_label_from_value(value: int) -> str:
    """Конвертирует числовое значение F&G в текстовый лейбл."""
    if value <= 20:   return "Extreme Fear"
    if value <= 40:   return "Fear"
    if value <= 60:   return "Neutral"
    if value <= 80:   return "Greed"
    return "Extreme Greed"


async def _fetch_fear_greed(session: aiohttp.ClientSession) -> tuple:
    """
    Получает Fear & Greed Index с fallback цепочкой:
      1. CoinGecko Pro/Demo API (/fear-greed-index) — если есть ключ
      2. alternative.me/fng — публичный, но нестабильный
      3. Дефолт 50 (Neutral) — если оба упали

    Returns: (value: int, label: str)
    """
    # ── Источник 1: CoinGecko Fear & Greed (платный/demo endpoint) ──
    if COINGECKO_API_KEY:
        try:
            async with session.get(
                "https://pro-api.coingecko.com/api/v3/fear-greed-index"
                if not COINGECKO_API_KEY.startswith("CG-") else
                "https://api.coingecko.com/api/v3/fear-greed-index",
                headers={
                    "x-cg-demo-api-key": COINGECKO_API_KEY
                } if COINGECKO_API_KEY.startswith("CG-") else {
                    "x-cg-pro-api-key": COINGECKO_API_KEY
                },
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    val = int(data.get("data", [{}])[0].get("value", 0))
                    lbl = data.get("data", [{}])[0].get("value_classification",
                                                         _fg_label_from_value(val))
                    logger.info(f"[ONCHAIN] F&G from CoinGecko: {val} ({lbl})")
                    return val, lbl
                else:
                    logger.warning(f"[ONCHAIN] CoinGecko F&G status={resp.status}, trying fallback")
        except Exception as e:
            logger.warning(f"[ONCHAIN] CoinGecko F&G failed: {e}, trying fallback")

    # ── Источник 2: alternative.me ──────────────────────────────────
    try:
        async with session.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            if resp.status == 200:
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type or "text/json" in content_type:
                    fg_data = await resp.json()
                    val = int(fg_data["data"][0]["value"])
                    lbl = fg_data["data"][0]["value_classification"]
                    logger.info(f"[ONCHAIN] F&G from alternative.me: {val} ({lbl})")
                    return val, lbl
                else:
                    # HTML вместо JSON — сервис недоступен
                    text = await resp.text()
                    logger.warning(f"[ONCHAIN] alternative.me returned non-JSON ({content_type}): {text[:80]}")
            else:
                logger.warning(f"[ONCHAIN] alternative.me F&G status={resp.status}")
    except Exception as e:
        logger.warning(f"[ONCHAIN] alternative.me F&G failed: {e}")

    # ── Источник 3: дефолт ──────────────────────────────────────────
    logger.warning("[ONCHAIN] All F&G sources failed, using default 50 (Neutral)")
    return 50, "Neutral"


async def get_coingecko_data() -> Dict:
    """
    Получает с CoinGecko:
      - fear_and_greed (0-100)
      - fg_label: "Extreme Fear" / "Fear" / "Neutral" / "Greed" / "Extreme Greed"
      - btc_dominance (%)
      - eth_volume_change_24h (%)
      - btc_volume_change_24h (%)
    """
    cached = _cache_get("coingecko")
    if cached is not None:
        return cached

    headers = (
        {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY.startswith("CG-")
        else {"x-cg-pro-api-key": COINGECKO_API_KEY}
    ) if COINGECKO_API_KEY else {}
    result = {
        "fear_and_greed": 50,
        "fg_label": "Neutral",
        "btc_dominance": 50.0,
        "eth_total_volume_24h": 0.0,
        "btc_total_volume_24h": 0.0,
        "eth_volume_change_24h": None,  # BUG-LO003: требует отдельного endpoint
        "btc_volume_change_24h": None,  # BUG-LO003: требует отдельного endpoint
        "error": None,
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            # 1. Global market data (dominance)
            async with session.get(
                "https://api.coingecko.com/api/v3/global",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                mcp = data.get("data", {}).get("market_cap_percentage", {})
                result["btc_dominance"] = round(mcp.get("btc", 50.0), 2)

            # 2. ETH + BTC volume/price change
            async with session.get(
                "https://api.coingecko.com/api/v3/coins/markets"
                "?vs_currency=usd&ids=bitcoin,ethereum&order=market_cap_desc",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                coins = await resp.json()
                for coin in coins:
                    # 🆕 FIX BUG-LO003: CoinGecko markets endpoint не предоставляет
                    # volume_change_24h. Использование price_change как прокси вводит
                    # в заблуждение — изменение цены ≠ изменение объёма.
                    # Убираем некорректные поля; реальный volume_change доступен только
                    # через /coins/{id}/market_chart (volume) с ручным расчётом дельты.
                    # Пока оставляем total_volume для справки, но volume_change = N/A.
                    if coin["id"] == "ethereum":
                        result["eth_total_volume_24h"] = coin.get("total_volume", 0.0) or 0.0
                        result["eth_volume_change_24h"] = None  # недоступно без доп. запросов
                    elif coin["id"] == "bitcoin":
                        result["btc_total_volume_24h"] = coin.get("total_volume", 0.0) or 0.0
                        result["btc_volume_change_24h"] = None  # недоступно без доп. запросов

            # 3. Fear & Greed Index
            # Цепочка источников: CoinGecko → alternative.me → дефолт 50
            fg_value, fg_label = await _fetch_fear_greed(session)
            result["fear_and_greed"] = fg_value
            result["fg_label"] = fg_label

    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"[ONCHAIN] CoinGecko/FNG error: {e}")

    _cache_set("coingecko", result)
    logger.info(f"[ONCHAIN] CoinGecko: F&G={result['fear_and_greed']} ({result['fg_label']}) | BTC.dom={result['btc_dominance']}%")
    return result

# =====================================================================
# 🧠  ONCHAIN BIAS — объединённый результат
# =====================================================================

async def get_onchain_bias() -> Dict:
    """
    Главная функция. Собирает все данные и возвращает:
      flow_data:      результат get_eth_flow_delta()
      cg_data:        результат get_coingecko_data()
      bias_long:      итоговый скор для long  (-15 … +15)
      bias_short:     итоговый скор для short (-15 … +15)
      tp_mult_long:   множитель TP для long  (0.85 … 1.15)
      tp_mult_short:  множитель TP для short (0.85 … 1.15)
      sl_mult_long:   множитель SL для long  (0.90 … 1.10)
      sl_mult_short:  множитель SL для short (0.90 … 1.10)
      lev_delta:      корректировка лева     (-2 … +1)
      summary:        текстовое описание
    """
    cached = _cache_get("onchain_bias")
    if cached is not None:
        return cached

    # Запускаем параллельно
    flow_data, cg_data = await asyncio.gather(
        get_eth_flow_delta(),
        get_coingecko_data(),
        return_exceptions=True,
    )

    # Безопасные дефолты при ошибке
    if isinstance(flow_data, Exception):
        flow_data = {"flow": "neutral", "strength": "weak", "delta_eth": 0.0, "per_exchange": {}, "note": "error"}
    if isinstance(cg_data, Exception):
        cg_data = {"fear_and_greed": 50, "fg_label": "Neutral", "btc_dominance": 50.0,
                   "eth_volume_change_24h": 0.0, "btc_volume_change_24h": 0.0, "error": str(cg_data)}

    flow      = flow_data.get("flow", "neutral")      # "inflow" | "outflow" | "neutral"
    strength  = flow_data.get("strength", "weak")     # "large" | "normal" | "weak"
    fg        = cg_data.get("fear_and_greed", 50)     # 0-100
    btc_dom   = cg_data.get("btc_dominance", 50.0)

    # ─────────────────────────────────────────────────────────────────
    # CONFIDENCE BIAS (±15 баллов)
    # ─────────────────────────────────────────────────────────────────
    bias_long  = 0
    bias_short = 0
    reasons    = []

    # ETH Exchange Flow
    # Отток с бирж (flow="outflow") = накопление = бычий сигнал для LONG
    # Приток на биржи (flow="inflow") = давление продаж = медвежий
    flow_pts = {"large": 10, "normal": 6, "weak": 3}.get(strength, 0)

    if flow == "outflow":
        bias_long  += flow_pts
        bias_short -= flow_pts
        reasons.append(f"ETH outflow {flow_data.get('delta_eth', 0):.0f} ETH 🟢")
    elif flow == "inflow":
        bias_long  -= flow_pts
        bias_short += flow_pts
        reasons.append(f"ETH inflow +{flow_data.get('delta_eth', 0):.0f} ETH 🔴")

    # Fear & Greed — контрарный индикатор
    # Extreme Fear (<20) + long = дно вероятно → +5 к long
    # Extreme Greed (>80) + short = перегрев → +5 к short
    # Extreme Greed + long = осторожно → -5 к long
    if fg < 20:
        bias_long  += 5
        reasons.append(f"Extreme Fear F&G={fg} (contrarian long) 😱")
    elif fg < 35:
        bias_long  += 2
        reasons.append(f"Fear F&G={fg} (mild long boost) 😨")
    elif fg > 80:
        bias_short += 5
        bias_long  -= 5
        reasons.append(f"Extreme Greed F&G={fg} (contrarian short) 🤑")
    elif fg > 65:
        bias_short += 2
        reasons.append(f"Greed F&G={fg} (mild short boost) 😏")

    # Клипуем до ±15
    bias_long  = max(-15, min(15, bias_long))
    bias_short = max(-15, min(15, bias_short))

    # ─────────────────────────────────────────────────────────────────
    # TP MULTIPLIER  (0.85 … 1.15)
    # ─────────────────────────────────────────────────────────────────
    tp_mult_long  = 1.0
    tp_mult_short = 1.0

    if flow == "outflow" and strength in ("large", "normal"):
        tp_mult_long  = 1.10   # держим long дольше — накопление
    elif flow == "inflow" and strength in ("large", "normal"):
        tp_mult_long  = 0.90   # берём прибыль раньше — давление продаж

    if flow == "inflow" and strength in ("large", "normal"):
        tp_mult_short = 1.10   # держим short дольше
    elif flow == "outflow" and strength in ("large", "normal"):
        tp_mult_short = 0.90

    # Extreme Fear — чуть дальше TP для long (дно близко)
    if fg < 20:
        tp_mult_long = min(1.15, tp_mult_long * 1.05)
    if fg > 80:
        tp_mult_short = min(1.15, tp_mult_short * 1.05)

    # ─────────────────────────────────────────────────────────────────
    # SL MULTIPLIER  (0.90 … 1.10)
    # Больше 1.0 = SL дальше (защита от шума)
    # Меньше 1.0 = SL ближе (больше риска — защищаемся)
    # ─────────────────────────────────────────────────────────────────
    sl_mult_long  = 1.0
    sl_mult_short = 1.0

    if flow == "inflow" and strength == "large":
        # Большой приток на биржи при long — подтягиваем SL ближе
        sl_mult_long  = 0.90
    if flow == "outflow" and strength == "large":
        sl_mult_short = 0.90

    # Extreme Fear — волатильность высокая, SL чуть дальше
    if fg < 20:
        sl_mult_long  = min(1.10, sl_mult_long  * 1.10)
        sl_mult_short = min(1.10, sl_mult_short * 1.10)

    # ─────────────────────────────────────────────────────────────────
    # LEVERAGE DELTA  (-2 … +1)
    # ─────────────────────────────────────────────────────────────────
    lev_delta = 0

    if flow == "outflow" and strength in ("large", "normal") and fg < 60:
        lev_delta = +1
    elif flow == "inflow" and strength == "large":
        lev_delta = -2
    elif flow == "inflow" and strength == "normal":
        lev_delta = -1

    # ─────────────────────────────────────────────────────────────────
    # ТЕКСТОВЫЙ СОВЕТ
    # ─────────────────────────────────────────────────────────────────
    if not reasons:
        summary = "📊 On-chain нейтрален. Стандартные параметры."
    else:
        summary = " | ".join(reasons)

    result = {
        "flow_data":      flow_data,
        "cg_data":        cg_data,
        "bias_long":      bias_long,
        "bias_short":     bias_short,
        "tp_mult_long":   round(tp_mult_long,  3),
        "tp_mult_short":  round(tp_mult_short, 3),
        "sl_mult_long":   round(sl_mult_long,  3),
        "sl_mult_short":  round(sl_mult_short, 3),
        "lev_delta":      lev_delta,
        "summary":        summary,
        "fear_and_greed": fg,
        "fg_label":       cg_data.get("fg_label", "Neutral"),
        "btc_dominance":  btc_dom,
        "eth_delta_eth":  flow_data.get("delta_eth", 0.0),
        "flow":           flow,
        "flow_strength":  strength,
    }

    _cache_set("onchain_bias", result)
    logger.info(f"[ONCHAIN] Bias: long={bias_long:+d} short={bias_short:+d} | lev_delta={lev_delta:+d} | {summary}")
    return result


# =====================================================================
# 🖨️  ФОРМАТИРОВАНИЕ ДЛЯ DISCORD
# =====================================================================

def format_onchain_report(bias: Dict) -> str:
    """Форматирует on-chain данные для вывода в Discord (!onchain)."""
    fg     = bias.get("fear_and_greed", 50)
    fglbl  = bias.get("fg_label", "Neutral")
    dom    = bias.get("btc_dominance", 50.0)
    flow   = bias.get("flow", "neutral")
    delta  = bias.get("eth_delta_eth", 0.0)
    stren  = bias.get("flow_strength", "weak")
    bl     = bias.get("bias_long",  0)
    bs     = bias.get("bias_short", 0)
    lev_d  = bias.get("lev_delta",  0)
    per_ex = bias.get("flow_data", {}).get("per_exchange", {})
    note   = bias.get("flow_data", {}).get("note", "")
    cg_err = bias.get("cg_data", {}).get("error")

    fg_emoji   = "😱" if fg < 20 else "😨" if fg < 35 else "😐" if fg < 65 else "😏" if fg < 80 else "🤑"
    flow_emoji = "🟢" if flow == "outflow" else "🔴" if flow == "inflow" else "⚪"
    lev_str    = f"+{lev_d}" if lev_d > 0 else str(lev_d)

    lines = [
        "**📡 On-Chain Analysis**",
        "",
        f"**Fear & Greed:** {fg_emoji} `{fg}` — {fglbl}",
        f"**BTC Dominance:** `{dom:.1f}%`",
        "",
        f"**ETH Exchange Flow:** {flow_emoji} `{'EXCHANGE INFLOW' if flow == 'inflow' else 'EXCHANGE OUTFLOW' if flow == 'outflow' else 'NEUTRAL'}` ({stren})",
    ]

    if note == "first_run":
        lines.append("  _(первый запуск — дельта будет на следующем цикле)_")
    else:
        lines.append(f"  Суммарная дельта: `{delta:+.0f} ETH`")
        if per_ex:
            for exch, d in per_ex.items():
                em = "🔴" if d > 0 else "🟢" if d < 0 else "⚪"
                lines.append(f"  {em} {exch}: `{d:+.0f} ETH`")

    lines += [
        "",
        f"**Влияние на сигналы:**",
        f"  Long bias:  `{bl:+d}` pts к confidence",
        f"  Short bias: `{bs:+d}` pts к confidence",
        f"  Leverage:   `{lev_str}` к рекомендованному",
    ]

    if bias.get("tp_mult_long", 1.0) != 1.0 or bias.get("tp_mult_short", 1.0) != 1.0:
        lines.append(f"  TP mult:  Long `×{bias['tp_mult_long']}` | Short `×{bias['tp_mult_short']}`")
    if bias.get("sl_mult_long", 1.0) != 1.0 or bias.get("sl_mult_short", 1.0) != 1.0:
        lines.append(f"  SL mult:  Long `×{bias['sl_mult_long']}` | Short `×{bias['sl_mult_short']}`")

    lines += ["", f"**Вывод:** {bias.get('summary', '—')}"]

    if cg_err:
        lines.append(f"\n⚠️ CoinGecko error: `{cg_err}`")

    return "\n".join(lines)
