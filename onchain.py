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
import time
from typing import Dict, Optional, Any, Tuple

from config import (
    ETHERSCAN_API_KEY,
    COINGECKO_API_KEY,
    ONCHAIN_CACHE_TTL,
    ONCHAIN_FLOW_THRESHOLD_ETH,
    ONCHAIN_FLOW_THRESHOLD_LARGE_ETH,
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

def _cache_get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < ONCHAIN_CACHE_TTL:
        return entry[1]
    return None

def _cache_set(key: str, value: Any):
    _cache[key] = (time.time(), value)

def clear_onchain_cache():
    """Сбрасывает весь on-chain кэш."""
    _cache.clear()
    logger.info("[ONCHAIN] Cache cleared")

# =====================================================================
# 🔗  ETHERSCAN — балансы бирж
# =====================================================================

async def _fetch_eth_balance(session: aiohttp.ClientSession, address: str) -> Optional[float]:
    """Возвращает ETH баланс адреса (в ETH, не в Wei)."""
    url = (
        f"https://api.etherscan.io/api"
        f"?module=account&action=balance"
        f"&address={address}"
        f"&tag=latest"
        f"&apikey={ETHERSCAN_API_KEY}"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if data.get("status") == "1":
                return int(data["result"]) / 1e18   # Wei → ETH
    except Exception as e:
        logger.warning(f"[ONCHAIN] Etherscan balance error {address[:10]}…: {e}")
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
      delta_eth:   суммарная дельта по всем биржам (+ = приток, - = отток)
      flow:        "inflow" | "outflow" | "neutral"
      strength:    "large" | "normal" | "weak"
      per_exchange: детали по каждой бирже
    """
    prev_balances = _cache_get("eth_balances_prev")
    curr_balances = await get_exchange_balances()

    # При первом запуске сохраняем как "предыдущие" и возвращаем neutral
    if prev_balances is None:
        _cache_set("eth_balances_prev", curr_balances)
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
        prev = prev_balances.get(name)
        if curr is not None and prev is not None:
            delta = curr - prev    # + = приток на биржу, - = отток с биржи
            delta_total += delta
            per_exchange[name] = round(delta, 2)

    # Обновляем "предыдущие"
    _cache_set("eth_balances_prev", curr_balances)

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
        "flow": flow,          # inflow  = давление продаж (медвежий)
        "strength": strength,  # outflow = накопление    (бычий)
        "per_exchange": per_exchange,
        "note": "ok",
    }

# =====================================================================
# 📊  COINGECKO — Fear & Greed, dominance, volume
# =====================================================================

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

    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    result = {
        "fear_and_greed": 50,
        "fg_label": "Neutral",
        "btc_dominance": 50.0,
        "eth_volume_change_24h": 0.0,
        "btc_volume_change_24h": 0.0,
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
                    # CoinGecko не даёт volume_change напрямую, используем price_change как прокси
                    price_change = coin.get("price_change_percentage_24h", 0.0) or 0.0
                    if coin["id"] == "ethereum":
                        result["eth_volume_change_24h"] = round(price_change, 2)
                    elif coin["id"] == "bitcoin":
                        result["btc_volume_change_24h"] = round(price_change, 2)

            # 3. Fear & Greed Index (альтернативный эндпоинт)
            async with session.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                fg_data = await resp.json()
                fg_value = int(fg_data["data"][0]["value"])
                fg_label = fg_data["data"][0]["value_classification"]
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
        f"**ETH Exchange Flow:** {flow_emoji} `{flow.upper()}` ({stren})",
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
