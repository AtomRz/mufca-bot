"""
onchain.py — On-chain analysis for MUFCA Bot.

Sources:
  - Etherscan API: exchange ETH-address balances (real inflows/outflows)
  - CoinGecko API: Fear & Greed, BTC dominance, volume change
Refresh: configurable interval (15m/30m/1h, default 1h) — see config.ONCHAIN_CACHE_TTL
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
    ONCHAIN_FLOW_THRESHOLD_ETH,
    ONCHAIN_FLOW_THRESHOLD_LARGE_ETH,
    safe_json_load,
    safe_json_save,
)
import config as _cfg

logger = logging.getLogger(__name__)

# =====================================================================
# 🏦  EXCHANGE ETH ADDRESSES (publicly known hot wallets)
# =====================================================================
EXCHANGE_ETH_ADDRESSES: Dict[str, str] = {
    "Binance":  "0x28C6c06298d514Db089934071355E5743bf21d60",
    "Coinbase": "0x71660c4005BA85c37ccec55d0C4493E66Fe775d3",
    "Kraken":   "0xE853c56864A2ebe4576a807D26Fdc4A0adA51919",
    "OKX":      "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b",
}

# =====================================================================
# 💾  CACHE
# =====================================================================
_cache: Dict[str, Tuple[float, Any]] = {}   # key -> (timestamp, data)

# 🆕 FIX: baseline balances used to live in a plain in-memory dict and were lost
# on every container restart, forcing a fresh "first_run" and delaying flow
# analysis by a full ONCHAIN_CACHE_TTL cycle (~1h) each time. Persisted to disk
# so a restart doesn't reset the baseline.
_ONCHAIN_BASELINE_FILE = os.path.join(DATA_DIR, "onchain_baseline.json")
_prev_balances: Dict[str, Optional[float]] = safe_json_load(_ONCHAIN_BASELINE_FILE, {})

# 🆕 Timestamp of the last baseline snapshot — needed to normalize the
# ONCHAIN_FLOW_THRESHOLD_ETH/_LARGE_ETH thresholds against the actually
# elapsed time, instead of treating the window as always 1h. The thresholds
# used to be absolute ETH figures regardless of window length — on a 30m/15m
# interval this overweighted the relative significance of one-off internal
# exchange transactions (hot/cold rebalancing) and produced false "large"
# triggers. See get_eth_flow_delta().
_ONCHAIN_BASELINE_TS_FILE = os.path.join(DATA_DIR, "onchain_baseline_ts.json")
_prev_balances_ts: float = safe_json_load(_ONCHAIN_BASELINE_TS_FILE, {}).get("ts", 0.0)


def _cache_get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _cfg.ONCHAIN_CACHE_TTL:
        return entry[1]
    return None

def _cache_set(key: str, value: Any):
    _cache[key] = (time.time(), value)

def clear_onchain_cache():
    """Clears the TTL cache of on-chain data.

    BUGFIX BUG-HI004: this used to also clear _prev_balances, which meant the
    next call to get_eth_flow_delta() fell into the first_run branch again
    (no baseline → returns note='first_run' → bot.py called
    clear_onchain_cache() → infinite loop). Now _prev_balances is NOT
    cleared — the baseline survives a cache reset. For a full reset including
    the baseline, use clear_onchain_cache_full().
    """
    _cache.clear()
    logger.info("[ONCHAIN] TTL cache cleared (baseline balances preserved)")

def clear_onchain_cache_full():
    """Full cache reset, including the balance baseline (only use with !reset_cache)."""
    global _prev_balances_ts
    _cache.clear()
    _prev_balances.clear()
    _prev_balances_ts = 0.0
    # Otherwise the baseline would just get pulled back from the on-disk file on the next scan.
    safe_json_save(_ONCHAIN_BASELINE_FILE, {})
    safe_json_save(_ONCHAIN_BASELINE_TS_FILE, {"ts": 0.0})
    logger.info("[ONCHAIN] Full cache cleared (including baseline balances)")

# =====================================================================
# 🔗  ETHERSCAN — exchange balances
# =====================================================================

async def _fetch_eth_balance(session: aiohttp.ClientSession, address: str, retries: int = 3) -> Optional[float]:
    """Returns the ETH balance of an address (in ETH, not Wei).

    BUGFIX BUG-ME002: Etherscan has a rate limit of 5 calls/second for free
    keys. 4 parallel requests via asyncio.gather can trigger a 429 Too Many
    Requests. Added retry with exponential backoff.
    """
    # 🆕 FIX: Etherscan deprecated the V1 endpoint (2025+).
    # Migrated to the V2 API: added /v2/ and the required chainid=1 (Ethereum mainnet).
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
                    # Rate limit — wait and retry
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(f"[ONCHAIN] Etherscan 429 for {address[:10]}…, retrying in {wait}s (attempt {attempt+1}/{retries})")
                    await asyncio.sleep(wait)
                    continue
                data = await resp.json()
                if str(data.get("status")) == "1":
                    return int(data["result"]) / 1e18   # Wei → ETH
                # status == "0" could be a rate limit or another error
                if data.get("message") == "NOTOK" and "rate limit" in str(data.get("result", "")).lower():
                    wait = 2 ** attempt
                    logger.warning(f"[ONCHAIN] Etherscan rate limit (message), retrying in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                # An error, but not a rate limit — don't retry
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
    Fetches all exchange address balances in parallel.
    Returns: {exchange_name: balance_eth}
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
    logger.info(f"[ONCHAIN] ETH balances fetched: { {k: f'{v:.0f}' if v is not None else 'N/A' for k, v in balances.items()} }")
    return balances


async def get_eth_flow_delta() -> Dict:
    """
    Compares current balances against the previous snapshot.
    Returns:
      delta_eth:    total delta across all exchanges (+ = inflow to exchanges, - = outflow from exchanges)
      flow:         "inflow" | "outflow" | "neutral"
      strength:     "large" | "normal" | "weak"
      per_exchange: per-exchange breakdown

    🆕 The strength/flow thresholds are normalized against the actually
    elapsed time since the last baseline snapshot (elapsed), instead of being
    treated as fixed ETH figures regardless of window length.
    ONCHAIN_FLOW_THRESHOLD_ETH/_LARGE_ETH are set for a "1 hour" baseline —
    on a 30m/15m window they scale down proportionally, otherwise a one-off
    internal exchange transaction (hot/cold rebalancing) would more often be
    mistakenly read as a large market flow on a short window. scale is
    clamped to [0.25, 4.0] and additionally floored at 1.0 when elapsed is
    close to zero (two calls back to back — e.g. a manual !onchain twice
    within a minute) — otherwise at a scale of 0.25 the thresholds sag to a
    quarter of nominal and a one-off internal exchange transaction can be
    falsely read as "large" on a short window where it actually wasn't.
    """
    global _prev_balances_ts
    curr_balances = await get_exchange_balances()
    now = time.time()

    # 🆕 FIX: this used to only check "is the dict empty?" — if the baseline
    # was loaded from disk but every value in it was None (e.g. the container
    # crashed exactly during the one successful fetch), a non-empty dict full
    # of Nones silently fell into the delta-calculation branch and returned
    # delta=0/note="ok" instead of an honest "no data yet, wait for the next cycle".
    baseline_is_empty = not _prev_balances or all(v is None for v in _prev_balances.values())

    # On the first run — save this as the baseline and let the second request happen after a pause
    if baseline_is_empty:
        _prev_balances.update(curr_balances)
        _prev_balances_ts = now
        safe_json_save(_ONCHAIN_BASELINE_FILE, _prev_balances)
        safe_json_save(_ONCHAIN_BASELINE_TS_FILE, {"ts": _prev_balances_ts})
        logger.info("[ONCHAIN] First run: saved baseline balances, delta will be available on next cycle.")
        return {
            "delta_eth": 0.0,
            "flow": "neutral",
            "strength": "weak",
            "per_exchange": {},
            "note": "first_run",
        }

    # Compute the delta
    delta_total = 0.0
    per_exchange = {}

    for name in EXCHANGE_ETH_ADDRESSES:
        curr = curr_balances.get(name)
        prev = _prev_balances.get(name)
        if curr is not None and prev is not None:
            delta = curr - prev    # + = inflow to the exchange, - = outflow from the exchange
            delta_total += delta
            per_exchange[name] = round(delta, 2)

    # How much time has actually elapsed since the last baseline — thresholds scale off this
    elapsed_seconds = (now - _prev_balances_ts) if _prev_balances_ts else 3600.0
    if elapsed_seconds < 300:
        # Two calls almost back to back (a manual !onchain twice within a
        # couple minutes) — don't let the thresholds collapse to the 0.25
        # floor, behave as if it's a full hour.
        scale = 1.0
    else:
        scale = max(0.25, min(4.0, elapsed_seconds / 3600.0))

    # 🆕 FIX: _prev_balances.update(curr_balances) used to overwrite a valid
    # baseline with None if Etherscan failed for one exchange THIS cycle —
    # that exchange dropped out of the calculation not just for the current
    # cycle (expected), but for the next one too (the baseline was already
    # corrupted). Now we only update the baseline for exchanges where a real
    # new value actually came back.
    for name, curr in curr_balances.items():
        if curr is not None:
            _prev_balances[name] = curr
    _prev_balances_ts = now
    safe_json_save(_ONCHAIN_BASELINE_FILE, _prev_balances)
    safe_json_save(_ONCHAIN_BASELINE_TS_FILE, {"ts": _prev_balances_ts})

    # Classify — thresholds normalized to the actual window length
    threshold_large = ONCHAIN_FLOW_THRESHOLD_LARGE_ETH * scale
    threshold_normal = ONCHAIN_FLOW_THRESHOLD_ETH * scale
    neutral_threshold = max(20.0, 100.0 * scale)

    abs_delta = abs(delta_total)
    if abs_delta >= threshold_large:
        strength = "large"
    elif abs_delta >= threshold_normal:
        strength = "normal"
    else:
        strength = "weak"

    flow = "inflow" if delta_total > neutral_threshold else "outflow" if delta_total < -neutral_threshold else "neutral"

    logger.info(
        f"[ONCHAIN] ETH flow: {flow} | delta={delta_total:.0f} ETH | strength={strength} "
        f"| window={elapsed_seconds/60:.0f}m (scale={scale:.2f}, thresholds={threshold_normal:.0f}/{threshold_large:.0f})"
    )

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
    """Converts a numeric F&G value into a text label."""
    if value <= 20:   return "Extreme Fear"
    if value <= 40:   return "Fear"
    if value <= 60:   return "Neutral"
    if value <= 80:   return "Greed"
    return "Extreme Greed"


async def _fetch_fear_greed(session: aiohttp.ClientSession) -> tuple:
    """
    Fetches the Fear & Greed Index with a fallback chain:
      1. CoinGecko Pro/Demo API (/fear-greed-index) — if a key is present
      2. alternative.me/fng — public, but flaky
      3. Default 50 (Neutral) — if both fail

    Returns: (value: int, label: str)
    """
    # ── Source 1: CoinGecko Fear & Greed (paid/demo endpoint) ──
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

    # ── Source 2: alternative.me ──────────────────────────────────
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
                    # HTML instead of JSON — service unavailable
                    text = await resp.text()
                    logger.warning(f"[ONCHAIN] alternative.me returned non-JSON ({content_type}): {text[:80]}")
            else:
                logger.warning(f"[ONCHAIN] alternative.me F&G status={resp.status}")
    except Exception as e:
        logger.warning(f"[ONCHAIN] alternative.me F&G failed: {e}")

    # ── Source 3: default ──────────────────────────────────────────
    logger.warning("[ONCHAIN] All F&G sources failed, using default 50 (Neutral)")
    return 50, "Neutral"


async def get_coingecko_data() -> Dict:
    """
    Fetches from CoinGecko:
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
    # 🆕 FIX: _fetch_fear_greed() already accounted for the pro/demo base URL,
    # but /global and /coins/markets were hardcoded to the free
    # api.coingecko.com — with a Pro key, such requests could fail with a
    # rate limit/401, since they hit the wrong host for the key they were issued for.
    base = "https://pro-api.coingecko.com/api/v3" if COINGECKO_API_KEY and not COINGECKO_API_KEY.startswith("CG-") else "https://api.coingecko.com/api/v3"
    result = {
        "fear_and_greed": 50,
        "fg_label": "Neutral",
        "btc_dominance": 50.0,
        "eth_total_volume_24h": 0.0,
        "btc_total_volume_24h": 0.0,
        "eth_volume_change_24h": None,  # BUG-LO003: requires a separate endpoint
        "btc_volume_change_24h": None,  # BUG-LO003: requires a separate endpoint
        "error": None,
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            # 1. Global market data (dominance)
            async with session.get(
                f"{base}/global",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                # 🆕 FIX: if CoinGecko returned an error (429/401/502), data
                # might not be a dict with the expected structure — .get() on
                # a non-dict would raise.
                if resp.status == 200 and isinstance(data, dict):
                    mcp = data.get("data", {}).get("market_cap_percentage", {})
                    result["btc_dominance"] = round(mcp.get("btc", 50.0), 2)
                else:
                    logger.warning(f"[ONCHAIN] CoinGecko /global status={resp.status}, using default dominance")

            # 2. ETH + BTC volume/price change
            async with session.get(
                f"{base}/coins/markets"
                "?vs_currency=usd&ids=bitcoin,ethereum&order=market_cap_desc",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                coins = await resp.json()
                # 🆕 FIX: on an error (429/401/502) the markets endpoint returns
                # a dict describing the error, not a list — `for coin in coins:
                # coin["id"]` would raise TypeError. Explicitly check the
                # type/status before iterating.
                if resp.status != 200 or not isinstance(coins, list):
                    logger.warning(f"[ONCHAIN] CoinGecko /coins/markets status={resp.status}, skipping volume data")
                    coins = []
                for coin in coins:
                    # 🆕 FIX BUG-LO003: the CoinGecko markets endpoint doesn't
                    # provide volume_change_24h. Using price_change as a proxy
                    # is misleading — a price change ≠ a volume change.
                    # Removing the incorrect fields; real volume_change is
                    # only available via /coins/{id}/market_chart (volume)
                    # with a manual delta computation. Keeping total_volume
                    # for reference for now, but volume_change = N/A.
                    if coin["id"] == "ethereum":
                        result["eth_total_volume_24h"] = coin.get("total_volume", 0.0) or 0.0
                        result["eth_volume_change_24h"] = None  # unavailable without extra requests
                    elif coin["id"] == "bitcoin":
                        result["btc_total_volume_24h"] = coin.get("total_volume", 0.0) or 0.0
                        result["btc_volume_change_24h"] = None  # unavailable without extra requests

            # 3. Fear & Greed Index
            # Source chain: CoinGecko → alternative.me → default 50
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
# 🧠  ONCHAIN BIAS — combined result
# =====================================================================

async def get_onchain_bias() -> Dict:
    """
    Main entry point. Gathers all the data and returns:
      flow_data:      result of get_eth_flow_delta()
      cg_data:        result of get_coingecko_data()
      bias_long:      final long score  (-15 … +15)
      bias_short:     final short score (-15 … +15)
      tp_mult_long:   TP multiplier for long  (0.85 … 1.15)
      tp_mult_short:  TP multiplier for short (0.85 … 1.15)
      sl_mult_long:   SL multiplier for long  (0.90 … 1.10)
      sl_mult_short:  SL multiplier for short (0.90 … 1.10)
      lev_delta:      leverage adjustment    (-2 … +1)
      summary:        text description
    """
    cached = _cache_get("onchain_bias")
    if cached is not None:
        return cached

    # Run in parallel
    flow_data, cg_data = await asyncio.gather(
        get_eth_flow_delta(),
        get_coingecko_data(),
        return_exceptions=True,
    )

    # Safe defaults on error
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
    # CONFIDENCE BIAS (±15 points)
    # ─────────────────────────────────────────────────────────────────
    bias_long  = 0
    bias_short = 0
    reasons    = []

    # ETH Exchange Flow
    # Outflow from exchanges (flow="outflow") = accumulation = bullish signal for LONG
    # Inflow to exchanges (flow="inflow") = sell pressure = bearish
    flow_pts = {"large": 10, "normal": 6, "weak": 3}.get(strength, 0)

    if flow == "outflow":
        bias_long  += flow_pts
        bias_short -= flow_pts
        reasons.append(f"ETH outflow {flow_data.get('delta_eth', 0):.0f} ETH 🟢")
    elif flow == "inflow":
        bias_long  -= flow_pts
        bias_short += flow_pts
        reasons.append(f"ETH inflow +{flow_data.get('delta_eth', 0):.0f} ETH 🔴")

    # Fear & Greed — a contrarian indicator
    # Extreme Fear (<20) + long = likely a bottom → +5 to long
    # Extreme Greed (>80) + short = overheated → +5 to short
    # Extreme Greed + long = caution → -5 to long
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

    # Clip to ±15
    bias_long  = max(-15, min(15, bias_long))
    bias_short = max(-15, min(15, bias_short))

    # ─────────────────────────────────────────────────────────────────
    # TP MULTIPLIER  (0.85 … 1.15)
    # ─────────────────────────────────────────────────────────────────
    tp_mult_long  = 1.0
    tp_mult_short = 1.0

    if flow == "outflow" and strength in ("large", "normal"):
        tp_mult_long  = 1.10   # hold longs longer — accumulation
    elif flow == "inflow" and strength in ("large", "normal"):
        tp_mult_long  = 0.90   # take profit sooner — sell pressure

    if flow == "inflow" and strength in ("large", "normal"):
        tp_mult_short = 1.10   # hold shorts longer
    elif flow == "outflow" and strength in ("large", "normal"):
        tp_mult_short = 0.90

    # Extreme Fear — push TP slightly farther for longs (a bottom is likely close)
    if fg < 20:
        tp_mult_long = min(1.15, tp_mult_long * 1.05)
    if fg > 80:
        tp_mult_short = min(1.15, tp_mult_short * 1.05)

    # ─────────────────────────────────────────────────────────────────
    # SL MULTIPLIER  (0.90 … 1.10)
    # Above 1.0 = SL farther (protection from noise)
    # Below 1.0 = SL closer (more risk — tighten protection)
    # ─────────────────────────────────────────────────────────────────
    sl_mult_long  = 1.0
    sl_mult_short = 1.0

    if flow == "inflow" and strength == "large":
        # A large inflow to exchanges while long — pull SL closer
        sl_mult_long  = 0.90
    if flow == "outflow" and strength == "large":
        sl_mult_short = 0.90

    # Extreme Fear — volatility is high, push SL slightly farther
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
    # TEXT SUMMARY
    # ─────────────────────────────────────────────────────────────────
    if not reasons:
        summary = "📊 On-chain neutral. Using standard parameters."
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
# 🖨️  DISCORD FORMATTING
# =====================================================================

def format_onchain_report(bias: Dict) -> str:
    """Formats on-chain data for output in Discord (!onchain)."""
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
        lines.append("  _(first run — delta will be available on the next cycle)_")
    elif note == "error":
        lines.append("  _(failed to fetch exchange balances — showing the last known result)_")
    else:
        lines.append(f"  Total delta: `{delta:+.0f} ETH`")
        if per_ex:
            for exch, d in per_ex.items():
                em = "🔴" if d > 0 else "🟢" if d < 0 else "⚪"
                lines.append(f"  {em} {exch}: `{d:+.0f} ETH`")

    lines += [
        "",
        f"**Impact on signals:**",
        f"  Long bias:  `{bl:+d}` pts to confidence",
        f"  Short bias: `{bs:+d}` pts to confidence",
        f"  Leverage:   `{lev_str}` to the recommended value",
    ]

    if bias.get("tp_mult_long", 1.0) != 1.0 or bias.get("tp_mult_short", 1.0) != 1.0:
        lines.append(f"  TP mult:  Long `×{bias['tp_mult_long']}` | Short `×{bias['tp_mult_short']}`")
    if bias.get("sl_mult_long", 1.0) != 1.0 or bias.get("sl_mult_short", 1.0) != 1.0:
        lines.append(f"  SL mult:  Long `×{bias['sl_mult_long']}` | Short `×{bias['sl_mult_short']}`")

    lines += ["", f"**Summary:** {bias.get('summary', '—')}"]

    if cg_err:
        lines.append(f"\n⚠️ CoinGecko error: `{cg_err}`")

    return "\n".join(lines)
