"""
onchain.py — On-chain analysis for MUFCA Bot.

Sources:
  - Etherscan API: exchange ETH-address balances (real inflows/outflows)
  - CoinGecko API: Fear & Greed, BTC dominance, volume change
Refresh: configurable interval (15m/30m/1h, default 1h) — see config.ONCHAIN_CACHE_TTL
"""

import asyncio
import aiohttp
import copy
import logging
import os
import re
import time
from typing import Dict, List, Optional, Any, Tuple

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
# Several hot wallets per exchange. One address is a poor proxy for a whole
# exchange: exchanges spread and rotate hot wallets, and a quiet address reads as
# a permanent 0 delta (Coinbase/Kraken/OKX sat at 0 while only Binance moved).
# Balances are tracked per wallet and summed per exchange, so transfers between an
# exchange's own tracked wallets net out in that sum.
# Format: {exchange: {wallet_label: address}}. These are the DEFAULTS: wallets can
# also be added/removed at runtime from the web UI (see "RUNTIME WALLET EDITS"
# below), and that is stored outside the code in DATA_DIR/onchain_wallets.json.
EXCHANGE_ETH_ADDRESSES: Dict[str, Dict[str, str]] = {
    # Binance: the FULL ETH-mainnet wallet list from Binance's Proof of Reserves
    # (snapshot at block 26,309,019, taken right after the 2026-10-29 hot-wallet
    # drain). Binance 14 / 15 / 16 were emptied in that incident and Binance
    # announced that all further withdrawals go out of newly generated wallets,
    # so the old 3-address set (Binance, Binance 14, Binance 15) went flat. The
    # old "Binance" 0x3f5CE5... is not in the PoR list and was dropped.
    # Tracking the whole set (cold + hot + Ceffu custody) lets hot<->cold
    # rebalancing net out in the per-exchange sum; only real in/outflow of the
    # set shows up as flow (see get_eth_flow_delta). When Binance publishes a new
    # PoR list, re-sync this block with the "ETH,ETH" rows.
    "Binance": {
        "Binance 14 (drained)": "0x28c6c06298d514db089934071355e5743bf21d60",
        "Binance 15 (drained)": "0x21a31ee1afc51d94c2efccaa2092ad1028285549",
        "Binance 16 (drained)": "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",
        # Found with !ethtrace on 2026-11-03: received ~285.8k ETH from tracked
        # wallets 0xf977/0x4ed6/0x5a52 and sent ~37.4k back, 18 txs, active
        # minutes before the check. No public label found — verify on Etherscan.
        "0x690c…91cc (hub)": "0x690c3eebf6ee237f00fc96f9766282f096c191cc",
        "0xbe0e…33e8": "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8",
        "0xf977…acec": "0xf977814e90da44bfa03b6295a0616a897441acec",
        "0x98ad…ba9d": "0x98adef6f2ac8572ec48965509d69a8dd5e8bba9d",
        "0x32e1…1149": "0x32e11a20337ebc79abd0eeab2d91bafbd9591149",
        "0x8356…0007": "0x835678a611b28684005a5e2233695fb6cbbb0007",
        "0x5a1e…6694": "0x5a1e5b56aa33c056ed7bb93b88b1774a456f6694",
        "0x4368…f042": "0x43684d03d81d3a4c70da68febdd61029d426f042",
        "0x8652…8d96": "0x86523c87c8ec98c7539e2c58cd813ee9d1a08d96",
        "0x18e2…e4d1": "0x18e226459ccf0eec276514a4fd3b226d8961e4d1",
        "0x4fdf…271e": "0x4fdfe365436b5273a42f135c6a6244a20404271e",
        "0xa7c0…592d": "0xa7c0d36c4698981fab42a7d8c783674c6fe2592d",
        "0x4ed6…17f1": "0x4ed6cf63bd9c009d247ee51224fc1c7041f517f1",
        "0x5a52…efcb": "0x5a52e96bacdabb82fd05763e25335261b270efcb",
        "0x4aec…4b96": "0x4aec0e98fc1fb55b9cc2faaa7a81acca42cb4e96",
        "0x9696…6976": "0x9696f59e4d72e237be84ffd425dcad154bf96976",
        "0x56ed…b17f": "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",
        "0x4976…2327":"0x4976a4a02f38326660d17bf34b431dc6e2eb2327",
        "Ceffu 0xd3a2…c4a1": "0xd3a22590f8243f8e83ac230d1842c9af0404c4a1",
        "Ceffu 0x3a3c…1dc8": "0x3a3c006053a9b40286b9951a11be4c5808c11dc8",
        "Ceffu 0x4a9e…261f": "0x4a9e49a45a4b2545cb177f79c7381a30e1dc261f",
    },
    "Coinbase": {
        # Only the old single address so far — add Coinbase's active hot wallets here.
        "Coinbase": "0x71660c4005BA85c37ccec55d0C4493E66Fe775d3",
    },
    "Kraken": {
        "Hot Wallet 1": "0xe9f7eCAe3A53D2A67105292894676b00d1FaB785",
        "Hot Wallet 2": "0xf30ba13e4b04Ce5dC4D254Ae5FA95477800F0EB0",
        "Hot Wallet 3": "0xcC282E2004428939ee5149A9e7872F0B4d5d5ec7",
        "Hot Wallet 4": "0x05ff6964D21e5dAE3b1010D5AE0465b3c450F381",
    },
    "OKX": {
        "OKX":    "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b",
        "OKX 4":  "0x2c8FBB630289363Ac80705A1a61273f76fD5a161",
        "OKX 27": "0xD7efCbB86eFdD9E8dE014dafA5944AaE36E817e4",
    },
}

# =====================================================================
# 🧩  RUNTIME WALLET EDITS (web "Add" / "Remove" buttons)
# =====================================================================
# EXCHANGE_ETH_ADDRESSES above is the DEFAULT list that ships with the code.
# Edits made from the web UI live in DATA_DIR/onchain_wallets.json and are merged
# on top of it at startup and after every edit, so the list can change without
# touching the code, and a code update neither loses nor resurrects UI edits:
#   {"added": {exchange: {label: address}}, "removed": [address, ...]}
# "removed" only hides a default wallet; removing a wallet that was added from the
# UI just deletes it from "added". EXCHANGE_ETH_ADDRESSES, _WALLETS and
# _WALLET_KEYS are rebuilt IN PLACE, so every reference to them sees the new set.
_DEFAULT_ETH_ADDRESSES: Dict[str, Dict[str, str]] = copy.deepcopy(EXCHANGE_ETH_ADDRESSES)
_WALLET_OVERRIDES_FILE = os.path.join(DATA_DIR, "onchain_wallets.json")
_MAX_CUSTOM_WALLETS = 50      # every tracked wallet costs one Etherscan call per refresh
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ZERO_ADDRESS = "0x" + "0" * 40


class WalletEditError(Exception):
    """A wallet add/remove that was refused. .status is the matching HTTP code."""
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _load_wallet_overrides() -> Dict[str, Any]:
    """Overrides from disk, sanitised: a hand-edited or corrupt file must never
    put a malformed address into the tracked set."""
    raw = safe_json_load(_WALLET_OVERRIDES_FILE, {})
    added: Dict[str, Dict[str, str]] = {}
    removed: List[str] = []
    if isinstance(raw, dict):
        if isinstance(raw.get("added"), dict):
            for ex, wallets in raw["added"].items():
                if ex not in _DEFAULT_ETH_ADDRESSES or not isinstance(wallets, dict):
                    continue
                for label, addr in wallets.items():
                    if isinstance(label, str) and isinstance(addr, str) and _ADDRESS_RE.match(addr):
                        added.setdefault(ex, {})[label] = addr.lower()
        if isinstance(raw.get("removed"), list):
            removed = list(dict.fromkeys(
                a.lower() for a in raw["removed"] if isinstance(a, str) and _ADDRESS_RE.match(a)
            ))
    return {"added": added, "removed": removed}


_wallet_overrides: Dict[str, Any] = _load_wallet_overrides()

# Flat view: (exchange, label, lowercase address). Balances/baseline are keyed by
# the lowercase address — stable even if a label is renamed.
_WALLETS: List[Tuple[str, str, str]] = []
_WALLET_KEYS: set = set()


def _rebuild_wallet_tables() -> None:
    """EXCHANGE_ETH_ADDRESSES = defaults - removed + added, rebuilt in place."""
    removed = set(_wallet_overrides["removed"])
    effective: Dict[str, Dict[str, str]] = {}
    seen: set = set()
    for ex, wallets in _DEFAULT_ETH_ADDRESSES.items():
        effective[ex] = {label: addr for label, addr in wallets.items() if addr.lower() not in removed}
        if not effective[ex]:
            # an exchange with no wallet would silently drop out of the flow: ignore the removals
            logger.warning(f"[ONCHAIN] every default {ex} wallet is marked removed — ignoring the removals")
            effective[ex] = dict(wallets)
        seen.update(a.lower() for a in effective[ex].values())
    for ex, wallets in _wallet_overrides["added"].items():
        for label, addr in wallets.items():
            if addr in seen:
                continue   # a hand-edited file must not track one address twice
            while label in effective[ex]:
                label += " +"
            effective[ex][label] = addr
            seen.add(addr)
    EXCHANGE_ETH_ADDRESSES.clear()
    EXCHANGE_ETH_ADDRESSES.update(effective)
    _WALLETS[:] = [
        (ex, label, addr.lower())
        for ex, wallets in EXCHANGE_ETH_ADDRESSES.items()
        for label, addr in wallets.items()
    ]
    _WALLET_KEYS.clear()
    _WALLET_KEYS.update(addr for _ex, _label, addr in _WALLETS)


_rebuild_wallet_tables()

# Free Etherscan keys allow 5 calls/s; ~10 wallets are fetched sequentially with
# this pause between calls (parallel requests mostly came back as 429).
_ETHERSCAN_CALL_SPACING = 0.25

# =====================================================================
# 💾  CACHE
# =====================================================================
_cache: Dict[str, Tuple[float, Any]] = {}   # key -> (timestamp, data)
_bias_lock = asyncio.Lock()

# 🆕 FIX: baseline balances used to live in a plain in-memory dict and were lost
# on every container restart, forcing a fresh "first_run" and delaying flow
# analysis by a full ONCHAIN_CACHE_TTL cycle (~1h) each time. Persisted to disk
# so a restart doesn't reset the baseline.
_ONCHAIN_BASELINE_FILE = os.path.join(DATA_DIR, "onchain_baseline.json")
# The baseline is keyed by lowercase wallet address. A baseline saved by the old
# one-address-per-exchange version (keys like "Binance") has no matching keys and
# is dropped here, so the first cycle after the upgrade is a clean "first_run"
# and no manual !reset_cache is needed.
_prev_balances: Dict[str, Optional[float]] = {
    k: v for k, v in safe_json_load(_ONCHAIN_BASELINE_FILE, {}).items() if k in _WALLET_KEYS
}

# 🆕 Timestamp of the last baseline snapshot — needed to normalize the
# ONCHAIN_FLOW_THRESHOLD_ETH/_LARGE_ETH thresholds against the actually
# elapsed time, instead of treating the window as always 1h. The thresholds
# used to be absolute ETH figures regardless of window length — on a 30m/15m
# interval this overweighted the relative significance of one-off internal
# exchange transactions (hot/cold rebalancing) and produced false "large"
# triggers. See get_eth_flow_delta().
_ONCHAIN_BASELINE_TS_FILE = os.path.join(DATA_DIR, "onchain_baseline_ts.json")
_prev_balances_ts: float = safe_json_load(_ONCHAIN_BASELINE_TS_FILE, {}).get("ts", 0.0)

# Exchange-wallet anomaly state (see config.ONCHAIN_ANOMALY_*). Persisted so a
# container restart in the middle of an incident doesn't silently re-enable the
# "outflow = bullish" reading for a wallet that was just drained.
#   {"until": <epoch seconds>, "events": [ {exchange, prev_eth, curr_eth, ...}, ... ]}
_ONCHAIN_ANOMALY_FILE = os.path.join(DATA_DIR, "onchain_anomaly.json")
_anomaly_state: Dict[str, Any] = safe_json_load(_ONCHAIN_ANOMALY_FILE, {})


def _cache_get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _cfg.ONCHAIN_CACHE_TTL:
        return entry[1]
    return None

def _cache_set(key: str, value: Any):
    _cache[key] = (time.time(), value)

def clear_onchain_cache_full():
    """Full cache reset, including the balance baseline (only use with !reset_cache).

    🆕 REMOVED (dead code cleanup): the partial clear_onchain_cache()
    (TTL cache only, baseline preserved) that used to sit above this
    function had no caller left anywhere in the codebase — it was
    imported into discord_commands.py but never actually invoked there,
    only mentioned in a comment. Only this full-reset version is used
    (by !reset_cache)."""
    global _prev_balances_ts
    _cache.clear()
    _prev_balances.clear()
    _prev_balances_ts = 0.0
    _anomaly_state.clear()
    # Otherwise the baseline would just get pulled back from the on-disk file on the next scan.
    safe_json_save(_ONCHAIN_BASELINE_FILE, {})
    safe_json_save(_ONCHAIN_BASELINE_TS_FILE, {"ts": 0.0})
    safe_json_save(_ONCHAIN_ANOMALY_FILE, {})
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
    Fetches the balance of every tracked wallet.
    Returns: {wallet_address_lowercase: balance_eth}  (None = fetch failed)

    Wallets are fetched one after another with a short pause — see
    _ETHERSCAN_CALL_SPACING (free-key limit is 5 calls/s).
    """
    cached = _cache_get("eth_balances")
    if cached is not None:
        return cached

    balances: Dict[str, Optional[float]] = {}
    async with aiohttp.ClientSession() as session:
        # a snapshot: a wallet added/removed from the web UI during these awaits
        # must not shift the list under the loop
        for i, (_ex, _label, addr) in enumerate(list(_WALLETS)):
            if i:
                await asyncio.sleep(_ETHERSCAN_CALL_SPACING)
            try:
                balances[addr] = await _fetch_eth_balance(session, addr)
            except Exception as e:
                logger.warning(f"[ONCHAIN] Etherscan balance error {addr[:10]}…: {e}")
                balances[addr] = None

    # 🆕 FIX: an all-None result (Etherscan down / rate-limited) was cached for the
    # whole TTL, so one bad moment froze "N/A" for up to an hour.
    if any(v is not None for v in balances.values()):
        _cache_set("eth_balances", balances)
    shown = {
        f"{ex}/{label}": (f"{balances[addr]:.0f}" if balances.get(addr) is not None else "N/A")
        for ex, label, addr in _WALLETS
    }
    logger.info(f"[ONCHAIN] ETH balances fetched: {shown}")
    return balances


async def get_eth_flow_delta() -> Dict:
    """
    Compares current balances against the previous snapshot.
    Returns:
      delta_eth:    total delta across all exchanges (+ = inflow to exchanges, - = outflow from exchanges)
      flow:         "inflow" | "outflow" | "neutral"
      strength:     "large" | "normal" | "weak"
      per_exchange: per-exchange breakdown (sum over the exchange's tracked wallets)
      per_wallet:   {exchange: {wallet_label: delta}} — shows which wallets actually move

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

    # 🆕 FIX: when Etherscan returned nothing for every exchange, the code below
    # carried on: delta 0, note "ok", and — worse — the baseline TIMESTAMP was
    # moved to now although no balance had been refreshed, so the next window
    # looked short against a delta that really spanned several cycles (false
    # "large" flows). Report an honest failure and leave the baseline alone.
    if all(v is None for v in curr_balances.values()):
        logger.warning("[ONCHAIN] No exchange balance could be fetched — baseline left untouched")
        return {
            "delta_eth": 0.0,
            "flow": "neutral",
            "strength": "weak",
            "per_exchange": {},
            "note": "fetch_failed",
        }

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

    # Compute the delta: per wallet first, then summed per exchange
    delta_total = 0.0
    per_exchange: Dict[str, float] = {}
    per_wallet: Dict[str, Dict[str, float]] = {}
    anomalies = []

    for exchange, wallets in EXCHANGE_ETH_ADDRESSES.items():
        # label -> (delta, prev, curr); only wallets with both a baseline and a fresh balance
        wallet_deltas: Dict[str, Tuple[float, float, float]] = {}
        for label, address in wallets.items():
            addr = address.lower()
            curr = curr_balances.get(addr)
            prev = _prev_balances.get(addr)
            if curr is not None and prev is not None:
                # + = inflow to the exchange, - = outflow from the exchange
                wallet_deltas[label] = (curr - prev, prev, curr)
        if not wallet_deltas:
            continue

        net = sum(d for d, _p, _c in wallet_deltas.values())
        per_exchange[exchange] = round(net, 2)
        per_wallet[exchange] = {label: round(d, 2) for label, (d, _p, _c) in wallet_deltas.items()}

        # 🆕 Anomaly guard: a change this large relative to the wallet's own
        # size is a drain/surge of the wallet itself (hack, emergency
        # cold-storage move, internal consolidation), not market flow.
        # With several wallets per exchange a big move of one wallet is often just
        # a transfer to another tracked wallet of the same exchange (hot-wallet
        # rebalancing): it nets out in the exchange sum and is NOT an anomaly.
        # It only counts if at least ANOMALY_CHANGE_FRACTION of the moved amount
        # really left/entered the tracked set (|net| vs the candidates' moves).
        # An anomalous exchange is excluded from the flow total as a whole, so a
        # half-visible transfer can't leak into it as fake inflow/outflow.
        candidates = {
            label: v for label, v in wallet_deltas.items()
            if v[1] >= _cfg.ONCHAIN_ANOMALY_MIN_BALANCE_ETH
            and abs(v[0]) / v[1] >= _cfg.ONCHAIN_ANOMALY_CHANGE_FRACTION
        }
        if candidates:
            moved = sum(abs(v[0]) for v in candidates.values())
            if abs(net) >= _cfg.ONCHAIN_ANOMALY_CHANGE_FRACTION * moved:
                for label, (delta, prev, curr) in candidates.items():
                    anomalies.append({
                        "exchange": exchange,
                        "wallet": label,
                        "kind": "drain" if delta < 0 else "surge",
                        "prev_eth": round(prev, 2),
                        "curr_eth": round(curr, 2),
                        "change_eth": round(delta, 2),
                        "change_pct": round(delta / prev * 100, 1),
                        "detected_ts": now,
                    })
                continue
            logger.info(
                f"[ONCHAIN] {exchange}: large wallet move(s) {moved:.0f} ETH but net {net:+.0f} ETH "
                f"— internal transfer between tracked wallets, not an anomaly"
            )
        delta_total += net

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
    # The timestamp only moves when every exchange that has a baseline was
    # refreshed. If one failed, its baseline is now older than the others', and
    # keeping the older timestamp makes the thresholds scale up (more
    # conservative) instead of treating a multi-cycle delta as a one-cycle one.
    fully_refreshed = all(
        curr_balances.get(addr) is not None
        for _ex, _label, addr in _WALLETS if _prev_balances.get(addr) is not None
    )
    for name, curr in curr_balances.items():
        if curr is not None:
            _prev_balances[name] = curr
    if fully_refreshed:
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

    # 🆕 Anomaly handling. A fresh anomaly (re)starts a hold window: the balance
    # baseline has already been moved to the post-event value above, so on the
    # next cycles the wallet would look "quiet" (or show a small leftover
    # outflow, as happened on 2026-10-30) even though the incident is ongoing.
    # While the hold is active the flow reading is forced neutral.
    for ev in anomalies:
        logger.warning(
            f"[ONCHAIN] ⚠️ ANOMALY: {ev['exchange']} ({ev['wallet']}) wallet {ev['kind']} "
            f"{ev['prev_eth']:.0f} -> {ev['curr_eth']:.0f} ETH ({ev['change_pct']:+.1f}%) — "
            f"not read as market flow"
        )
    if anomalies:
        _anomaly_state["until"] = now + _cfg.ONCHAIN_ANOMALY_HOLD_SECONDS
        _anomaly_state["events"] = anomalies
        safe_json_save(_ONCHAIN_ANOMALY_FILE, _anomaly_state)

    anomaly = None
    anomaly_until = float(_anomaly_state.get("until", 0.0) or 0.0)
    if now < anomaly_until:
        anomaly = {
            "active": True,
            "fresh": bool(anomalies),          # True only on the cycle that detected it (used to alert once)
            "until": anomaly_until,
            "events": _anomaly_state.get("events", []),
        }
        flow, strength = "neutral", "weak"

    logger.info(
        f"[ONCHAIN] ETH flow: {flow} | delta={delta_total:.0f} ETH | strength={strength} "
        f"| window={elapsed_seconds/60:.0f}m (scale={scale:.2f}, thresholds={threshold_normal:.0f}/{threshold_large:.0f})"
    )

    return {
        "delta_eth": round(delta_total, 2),
        "flow": flow,
        "strength": strength,
        "per_exchange": per_exchange,
        "per_wallet": per_wallet,
        "note": "ok",
        "anomaly": anomaly,
    }

# =====================================================================
# 🔎  WALLET TRACE — find an exchange's NEW wallets (used by !ethtrace)
# =====================================================================
# Exchanges rotate hot wallets (Binance did after the 2026-10-29 incident). When
# the tracked set goes quiet, the funds went to addresses we don't track. This
# looks at the latest normal transactions of every tracked wallet of an exchange
# and lists the big counterparties that are NOT in the tracked set — those are
# the candidates to add to EXCHANGE_ETH_ADDRESSES. Read-only: changes nothing.
_TRACE_TX_PER_WALLET = 100   # latest N normal txs per wallet (Etherscan page size)


async def _fetch_txlist(session: aiohttp.ClientSession, address: str, retries: int = 3) -> Optional[List[dict]]:
    """Latest normal transactions of an address, newest first.
    [] = the address has none; None = the fetch failed."""
    url = (
        f"https://api.etherscan.io/v2/api"
        f"?chainid=1&module=account&action=txlist"
        f"&address={address}&startblock=0&endblock=99999999"
        f"&page=1&offset={_TRACE_TX_PER_WALLET}&sort=desc"
        f"&apikey={ETHERSCAN_API_KEY}"
    )
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                data = await resp.json()
                result = data.get("result")
                if str(data.get("status")) == "1" and isinstance(result, list):
                    return result
                if "no transactions found" in str(data.get("message", "")).lower():
                    return []
                if "rate limit" in str(result).lower():
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.warning(f"[ONCHAIN] txlist error for {address[:10]}…: {data.get('message')} — {str(result)[:120]}")
                return None
        except Exception as e:
            logger.warning(f"[ONCHAIN] txlist error {address[:10]}… (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(1)
    return None


def _aggregate_counterparties(wallet: str, label: str, txs: List[dict], tracked: set,
                              agg: Dict[str, Dict[str, Any]]) -> None:
    """Adds one wallet's transactions to `agg` ({counterparty: totals}).
    Only successful transfers with value > 0 whose other side is not tracked.
    out_eth = sent BY the tracked wallet TO the counterparty; in_eth = the reverse."""
    for tx in txs:
        if str(tx.get("isError", "0")) == "1":
            continue
        try:
            value = int(tx.get("value", "0")) / 1e18
            ts = int(tx.get("timeStamp", 0))
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        frm = str(tx.get("from") or "").lower()
        to = str(tx.get("to") or "").lower()
        if frm == wallet:
            other, key = to, "out_eth"
        elif to == wallet:
            other, key = frm, "in_eth"
        else:
            continue
        if not other or other in tracked:
            continue
        rec = agg.setdefault(other, {"in_eth": 0.0, "out_eth": 0.0, "txs": 0, "last_ts": 0, "via": set()})
        rec[key] += value
        rec["txs"] += 1
        rec["last_ts"] = max(rec["last_ts"], ts)
        rec["via"].add(label)


async def trace_exchange_wallets(exchange: str = "Binance", min_eth: float = 1000.0, top_n: int = 15) -> Dict:
    """Big counterparties (>= min_eth in total) of an exchange's tracked wallets
    that are not tracked themselves, ranked by volume. Returns
    {exchange, wallets_checked, failed: [labels], min_eth, counterparties: [...]}
    or {"error": "..."} for an unknown exchange."""
    wanted = next((ex for ex in EXCHANGE_ETH_ADDRESSES if ex.lower() == exchange.strip().lower()), None)
    if wanted is None:
        return {"error": f"unknown exchange {exchange!r}, choose from: {', '.join(EXCHANGE_ETH_ADDRESSES)}"}

    agg: Dict[str, Dict[str, Any]] = {}
    failed: List[str] = []
    wallets = list(EXCHANGE_ETH_ADDRESSES[wanted].items())
    async with aiohttp.ClientSession() as session:
        for i, (label, address) in enumerate(wallets):
            if i:
                await asyncio.sleep(_ETHERSCAN_CALL_SPACING)
            addr = address.lower()
            txs = await _fetch_txlist(session, addr)
            if txs is None:
                failed.append(label)
                continue
            _aggregate_counterparties(addr, label, txs, _WALLET_KEYS, agg)

    rows = []
    for other, r in agg.items():
        total = r["in_eth"] + r["out_eth"]
        if total >= min_eth:
            rows.append({
                "address": other,
                "in_eth": round(r["in_eth"], 2),
                "out_eth": round(r["out_eth"], 2),
                "txs": r["txs"],
                "last_ts": r["last_ts"],
                "via": sorted(r["via"]),
            })
    rows.sort(key=lambda x: x["in_eth"] + x["out_eth"], reverse=True)
    return {
        "exchange": wanted,
        "wallets_checked": len(wallets) - len(failed),
        "failed": failed,
        "min_eth": min_eth,
        "counterparties": rows[:top_n],
        "total_matches": len(rows),
    }


# =====================================================================
# ✅  WALLET VERIFY — health check of the tracked set + new candidates
# =====================================================================
# Web "Verify wallets" button and Discord !ethverify. Read-only: one txlist and
# one balance call per tracked wallet (~2 x N Etherscan calls, 25-45 s for
# Binance). It never edits EXCHANGE_ETH_ADDRESSES, it only recommends. Every
# verdict is a heuristic from on-chain activity (Etherscan name tags are not in
# the free API), so confirm on Etherscan before changing the list.
# Limits: only normal txs (no internal transfers) and only the latest
# _TRACE_TX_PER_WALLET txs per wallet are looked at.
_VERIFY_ACTIVE_DAYS = 7        # a tx within this many days = "active"
_VERIFY_STALE_DAYS = 30        # no tx for this long AND a dust balance = "stale"
_VERIFY_DUST_ETH = 100.0       # below this a quiet wallet carries no flow signal
_VERIFY_CONDUIT_ETH = 10.0     # a recently active wallet holding less than this is a pass-through
_CANDIDATE_STALE_DAYS = 90     # candidate untouched this long = "skip"
# Big counterparties that are not exchange wallets at all.
_KNOWN_NON_EXCHANGE: Dict[str, str] = {
    "0x00000000219ab540356cbb839cbe05303d7705fa": "Beacon Deposit Contract (ETH staking)",
}
_WALLET_STATUS_ORDER = {"stale": 0, "unknown": 1, "quiet": 2, "holding": 3, "conduit": 4, "active": 5}
_CANDIDATE_VERDICT_ORDER = {"add": 0, "review": 1, "skip": 2}


def _newest_ts(txs: Optional[List[dict]]) -> Optional[int]:
    """Timestamp of the newest tx in a txlist, None if there are none."""
    best = 0
    for tx in txs or []:
        try:
            best = max(best, int(tx.get("timeStamp", 0)))
        except (TypeError, ValueError):
            continue
    return best or None


def _wallet_status(balance: Optional[float], last_ts: Optional[int], txs_ok: bool, now: float) -> Tuple[str, str]:
    """(status, reason) for one tracked wallet. The reason never repeats the
    balance or the last-tx age: the web row and the Discord line show those next to it.
    active  = tx in the last _VERIFY_ACTIVE_DAYS days
    conduit = recent txs but holds < _VERIFY_CONDUIT_ETH: a pass-through that no
              longer carries the exchange's balance (harmless to keep)
    holding = quiet but still holds >= _VERIFY_DUST_ETH (cold storage: keep)
    stale   = no tx for _VERIFY_STALE_DAYS days and a dust balance (remove?)
    quiet   = in between, nothing to act on
    unknown = a fetch failed and the wallet is not visibly active"""
    age_days = (now - last_ts) / 86400 if last_ts else None
    if age_days is not None and age_days <= _VERIFY_ACTIVE_DAYS:
        if balance is not None and balance < _VERIFY_CONDUIT_ETH:
            return "conduit", "pass-through: moves ETH but holds almost none, harmless to keep"
        return "active", ""
    if balance is None or not txs_ok:
        return "unknown", "an Etherscan fetch failed, re-run"
    if balance >= _VERIFY_DUST_ETH:
        return "holding", "quiet but holds funds (cold storage?), keep"
    if age_days is None or age_days > _VERIFY_STALE_DAYS:
        return "stale", "no recent activity and almost no balance: candidate to remove"
    return "quiet", "no recent activity, nothing to act on"


def _classify_candidate(addr: str, rec: Dict[str, Any], now: float) -> Tuple[str, str]:
    """(verdict, reason) for an untracked counterparty of the tracked set.
    add    = ETH moves both to AND from tracked wallets: behaves like an
             internal wallet of the exchange.
    review = one-way only. A sender that only ever feeds a hub is usually a
             deposit address being swept (millions exist, never worth tracking);
             a receiver that only gets funds may be a client or a staking
             address, and tracking it would net real withdrawals out of the flow.
    skip   = known non-wallet or inactive."""
    known = _KNOWN_NON_EXCHANGE.get(addr)
    if known:
        return "skip", f"not an exchange wallet: {known}"
    age_days = (now - rec["last_ts"]) / 86400 if rec["last_ts"] else None
    if age_days is None or age_days > _CANDIDATE_STALE_DAYS:
        return "skip", "inactive for " + ("a long time" if age_days is None else f"{age_days:.0f}d")
    if rec["in_eth"] > 0 and rec["out_eth"] > 0:
        return "add", "moves ETH both to and from tracked wallets"
    if rec["in_eth"] > 0:
        return "review", "only sends to tracked wallets: likely a deposit/sweep address, add only if Etherscan labels it as the exchange"
    return "review", "only receives from tracked wallets: may be a client or staking address, add only if Etherscan labels it as the exchange"


async def verify_exchange_wallets(exchange: str = "Binance", min_eth: float = 1000.0, top_n: int = 20) -> Dict:
    """Health check of an exchange's tracked wallets plus classified new candidates.
    Returns {exchange, wallets_checked, failed: [labels], min_eth, summary,
    wallets: [...], candidates: [...], total_candidates, snippet}
    or {"error": "..."} for an unknown exchange."""
    wanted = next((ex for ex in EXCHANGE_ETH_ADDRESSES if ex.lower() == exchange.strip().lower()), None)
    if wanted is None:
        return {"error": f"unknown exchange {exchange!r}, choose from: {', '.join(EXCHANGE_ETH_ADDRESSES)}"}

    wallets = list(EXCHANGE_ETH_ADDRESSES[wanted].items())
    now = time.time()
    custom_addrs = {a for w in _wallet_overrides["added"].values() for a in w.values()}
    agg: Dict[str, Dict[str, Any]] = {}
    rows: List[Dict[str, Any]] = []
    failed: List[str] = []
    async with aiohttp.ClientSession() as session:
        for i, (label, address) in enumerate(wallets):
            if i:
                await asyncio.sleep(_ETHERSCAN_CALL_SPACING)
            addr = address.lower()
            txs = await _fetch_txlist(session, addr)
            await asyncio.sleep(_ETHERSCAN_CALL_SPACING)
            balance = await _fetch_eth_balance(session, addr)
            if txs is None or balance is None:
                failed.append(label)
            if txs:
                _aggregate_counterparties(addr, label, txs, _WALLET_KEYS, agg)
            last_ts = _newest_ts(txs)
            status, reason = _wallet_status(balance, last_ts, txs is not None, now)
            rows.append({
                "label": label,
                "address": addr,
                "balance_eth": None if balance is None else round(balance, 2),
                "last_ts": last_ts,
                "status": status,
                "reason": reason,
                "custom": addr in custom_addrs,   # added from the web UI, not from the code defaults
            })
    rows.sort(key=lambda r: (_WALLET_STATUS_ORDER[r["status"]], -(r["balance_eth"] or 0.0)))

    cands: List[Dict[str, Any]] = []
    for other, r in agg.items():
        if r["in_eth"] + r["out_eth"] < min_eth:
            continue
        verdict, reason = _classify_candidate(other, r, now)
        cands.append({
            "address": other,
            "verdict": verdict,
            "reason": reason,
            "in_eth": round(r["in_eth"], 2),
            "out_eth": round(r["out_eth"], 2),
            "txs": r["txs"],
            "last_ts": r["last_ts"],
            "via": sorted(r["via"]),
        })
    cands.sort(key=lambda c: (_CANDIDATE_VERDICT_ORDER[c["verdict"]], -(c["in_eth"] + c["out_eth"])))

    summary: Dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    # ready-to-paste lines for EXCHANGE_ETH_ADDRESSES, only for "add" verdicts
    snippet = "\n".join(
        f'        "{c["address"][:6]}…{c["address"][-4:]}": "{c["address"]}",'
        for c in cands if c["verdict"] == "add"
    )
    return {
        "exchange": wanted,
        "wallets_checked": len(wallets) - len(failed),
        "failed": failed,
        "min_eth": min_eth,
        "summary": summary,
        "wallets": rows,
        "candidates": cands[:top_n],
        "total_candidates": len(cands),
        "snippet": snippet,
    }


# =====================================================================
# ✏️  ADD / REMOVE TRACKED WALLETS (web buttons, see "RUNTIME WALLET EDITS")
# =====================================================================
# Why this is safe for the flow signal: get_eth_flow_delta() only computes a delta
# for a wallet that has BOTH a baseline and a fresh balance. A newly added wallet
# has no baseline, so its first refresh just records one (no fake inflow); a removed
# wallet simply stops being read, and its baseline is dropped here so a later
# re-add starts clean instead of diffing against a stale balance.

def _clean_label(label: Optional[str], addr: str) -> str:
    """User label -> short safe text. Labels are printed in Discord messages, so
    markdown/mention characters (@ ` * _ ~ | < >) are dropped; max 40 chars;
    falls back to the short address."""
    text = re.sub(r"[^\w .\-()#/…+]|_", "", label or "")
    text = re.sub(r"\s+", " ", text).strip()[:40].strip()
    return text or f"{addr[:6]}…{addr[-4:]}"


def _canonical_exchange(exchange: str) -> str:
    ex = next((e for e in _DEFAULT_ETH_ADDRESSES if e.lower() == (exchange or "").strip().lower()), None)
    if ex is None:
        raise WalletEditError(f"unknown exchange {exchange!r}, choose from: {', '.join(_DEFAULT_ETH_ADDRESSES)}", 400)
    return ex


def _clean_address(address: str) -> str:
    addr = (address or "").strip()
    if not _ADDRESS_RE.match(addr):
        raise WalletEditError("address must be 0x followed by 40 hex characters", 400)
    return addr.lower()


def _commit_wallet_edit() -> None:
    safe_json_save(_WALLET_OVERRIDES_FILE, _wallet_overrides)
    _rebuild_wallet_tables()
    # cached balances belong to the old set: the next refresh fetches the new one
    _cache.pop("eth_balances", None)


def add_tracked_wallet(exchange: str, address: str, label: Optional[str] = None) -> Dict[str, Any]:
    """Starts tracking `address` for `exchange`. Re-adding a removed default wallet
    of that exchange simply undoes the removal. Raises WalletEditError."""
    ex = _canonical_exchange(exchange)
    addr = _clean_address(address)
    if addr == _ZERO_ADDRESS:
        raise WalletEditError("the zero address cannot be tracked", 400)
    if addr in _KNOWN_NON_EXCHANGE:
        raise WalletEditError(f"not an exchange wallet: {_KNOWN_NON_EXCHANGE[addr]}", 400)
    for t_ex, t_label, t_addr in _WALLETS:
        if t_addr == addr:
            raise WalletEditError(f"already tracked under {t_ex} as {t_label!r}", 409)

    removed = _wallet_overrides["removed"]
    default_label = next((l for l, a in _DEFAULT_ETH_ADDRESSES[ex].items() if a.lower() == addr), None)
    if addr in removed and default_label is not None:
        removed.remove(addr)                       # undo of a "Remove" on a default wallet
        final_label = default_label
    else:
        if sum(len(w) for w in _wallet_overrides["added"].values()) >= _MAX_CUSTOM_WALLETS:
            raise WalletEditError(f"at most {_MAX_CUSTOM_WALLETS} wallets can be added from the UI", 409)
        base = _clean_label(label, addr)
        final_label, n = base, 2
        while final_label in EXCHANGE_ETH_ADDRESSES[ex]:
            final_label, n = f"{base} ({n})", n + 1
        _wallet_overrides["added"].setdefault(ex, {})[final_label] = addr
    _commit_wallet_edit()
    logger.info(f"[ONCHAIN] Wallet added from the UI: {ex} / {final_label} / {addr}")
    return {"exchange": ex, "address": addr, "label": final_label, "tracked": len(EXCHANGE_ETH_ADDRESSES[ex])}


def remove_tracked_wallet(exchange: str, address: str) -> Dict[str, Any]:
    """Stops tracking `address` for `exchange` (a default wallet is hidden, a UI-added
    one is deleted). The last wallet of an exchange cannot be removed.
    Raises WalletEditError."""
    ex = _canonical_exchange(exchange)
    addr = _clean_address(address)
    label = next((l for l, a in EXCHANGE_ETH_ADDRESSES[ex].items() if a.lower() == addr), None)
    if label is None:
        raise WalletEditError(f"{addr} is not tracked under {ex}", 404)
    if len(EXCHANGE_ETH_ADDRESSES[ex]) <= 1:
        raise WalletEditError(f"cannot remove the last tracked wallet of {ex}", 409)

    added = _wallet_overrides["added"].get(ex, {})
    custom_label = next((l for l, a in added.items() if a == addr), None)
    if custom_label is not None:
        del added[custom_label]
        if not added:
            _wallet_overrides["added"].pop(ex, None)
    elif addr not in _wallet_overrides["removed"]:
        _wallet_overrides["removed"].append(addr)
    _commit_wallet_edit()
    _prev_balances.pop(addr, None)
    safe_json_save(_ONCHAIN_BASELINE_FILE, _prev_balances)
    logger.info(f"[ONCHAIN] Wallet removed from the UI: {ex} / {label} / {addr}")
    return {"exchange": ex, "address": addr, "label": label, "tracked": len(EXCHANGE_ETH_ADDRESSES[ex])}


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
                    # 🆕 FIX: a missing "value" used to default to 0 — i.e. a
                    # genuine-looking "Extreme Fear" that boosted long bias
                    # and TP/SL multipliers. Missing/out-of-range = failure,
                    # fall through to the next source.
                    item = (data.get("data") or [{}])[0]
                    val = int(item["value"])
                    if not (0 <= val <= 100):
                        raise ValueError(f"F&G value out of range: {val}")
                    lbl = item.get("value_classification") or _fg_label_from_value(val)
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

    # 🆕 FIX: the three requests used to share one try/except, so a failure in the
    # FIRST one (e.g. an HTML 502 page from CoinGecko -> resp.json() raises)
    # skipped everything after it — including the Fear & Greed lookup, which then
    # silently stayed at the default 50 although alternative.me was reachable.
    # Each step now fails on its own.
    errors = []
    async with aiohttp.ClientSession(headers=headers) as session:
        # 1. Global market data (dominance)
        try:
            async with session.get(
                f"{base}/global",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                # 🆕 FIX: if CoinGecko returned an error (429/401/502), data
                # might not be a dict with the expected structure — .get() on
                # a non-dict would raise.
                if resp.status == 200 and isinstance(data, dict):
                    mcp = (data.get("data") or {}).get("market_cap_percentage") or {}
                    btc_dom = mcp.get("btc")
                    if isinstance(btc_dom, (int, float)) and 0 < btc_dom < 100:
                        result["btc_dominance"] = round(float(btc_dom), 2)
                    else:
                        logger.warning("[ONCHAIN] CoinGecko /global had no usable BTC dominance, using default")
                else:
                    logger.warning(f"[ONCHAIN] CoinGecko /global status={resp.status}, using default dominance")
        except Exception as e:
            errors.append(f"global: {e}")
            logger.warning(f"[ONCHAIN] CoinGecko /global failed: {e}")

        # 2. ETH + BTC volume/price change
        try:
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
                    if not isinstance(coin, dict):
                        continue
                    if coin.get("id") == "ethereum":
                        result["eth_total_volume_24h"] = coin.get("total_volume", 0.0) or 0.0
                        result["eth_volume_change_24h"] = None  # unavailable without extra requests
                    elif coin.get("id") == "bitcoin":
                        result["btc_total_volume_24h"] = coin.get("total_volume", 0.0) or 0.0
                        result["btc_volume_change_24h"] = None  # unavailable without extra requests
        except Exception as e:
            errors.append(f"markets: {e}")
            logger.warning(f"[ONCHAIN] CoinGecko /coins/markets failed: {e}")

        # 3. Fear & Greed Index
        # Source chain: CoinGecko → alternative.me → default 50
        try:
            fg_value, fg_label = await _fetch_fear_greed(session)
            result["fear_and_greed"] = fg_value
            result["fg_label"] = fg_label
        except Exception as e:
            errors.append(f"fear_greed: {e}")
            logger.warning(f"[ONCHAIN] Fear & Greed failed: {e}")

    if errors:
        result["error"] = "; ".join(errors)

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
    # 🆕 FIX: serialise the refresh. The scanner and a manual !onchain can both
    # find the cache expired at the same moment; the second run then compared
    # fresh balances against the baseline the first run had JUST updated
    # (delta ~0, window < 5 min) and overwrote the correct result in the cache
    # with a neutral one.
    async with _bias_lock:
        cached = _cache_get("onchain_bias")
        if cached is not None:
            return cached
        return await _compute_onchain_bias()


async def _compute_onchain_bias() -> Dict:
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

    # Exchange-wallet anomaly: flow was forced neutral in get_eth_flow_delta()
    # (no directional bias, no TP/SL tilt). The cause of the anomaly is unknown
    # (hack / emergency move), so the one thing we do apply is less leverage.
    anomaly = flow_data.get("anomaly")
    if anomaly and anomaly.get("active"):
        lev_delta = min(lev_delta, _cfg.ONCHAIN_ANOMALY_LEV_DELTA)
        for ev in anomaly.get("events", []):
            reasons.append(
                f"⚠️ {ev.get('exchange')} wallet {ev.get('kind')} "
                f"{ev.get('prev_eth', 0):.0f} → {ev.get('curr_eth', 0):.0f} ETH "
                f"({ev.get('change_pct', 0):+.0f}%) — flow ignored, leverage reduced"
            )

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
        "anomaly":        anomaly if (anomaly and anomaly.get("active")) else None,
    }

    # a failed balance fetch is reported but not cached, so the next caller
    # retries instead of serving "neutral" for a whole TTL
    if flow_data.get("note") != "fetch_failed":
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
    elif note == "fetch_failed":
        lines.append("  _(Etherscan returned no exchange balances — baseline kept, flow unknown this cycle)_")
    elif note == "error":
        lines.append("  _(failed to fetch exchange balances — showing the last known result)_")
    else:
        anomaly = bias.get("anomaly")
        if anomaly and anomaly.get("active"):
            for ev in anomaly.get("events", []):
                lines.append(
                    f"  ⚠️ **ANOMALY:** {ev.get('exchange')} wallet {ev.get('kind')} "
                    f"`{ev.get('prev_eth', 0):.0f} → {ev.get('curr_eth', 0):.0f} ETH` "
                    f"({ev.get('change_pct', 0):+.0f}%) — treated as a wallet event, not market flow"
                )
            hold_min = max(0, int((anomaly.get("until", 0) - time.time()) / 60))
            lines.append(f"  _(on-chain flow is held neutral for another ~{hold_min} min)_")
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
