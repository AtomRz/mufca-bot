"""
derivatives.py — Funding Rate & Open Interest bias for MUFCA Bot.

Only meaningful for perpetual futures (config.MARKET_MODE == "futures") —
neither concept exists for spot trading. Structurally mirrors onchain.py
(cache, baseline snapshot for delta tracking, same bias dict shape:
bias_long/short, tp_mult/sl_mult, lev_delta, summary) so bot.py can combine
it with the on-chain bias the same way both are already threaded through
signals.check_signals()/apply_onchain_with_safety() — one code path handles
both, not two different data models bolted together.

Key difference from onchain.py: on-chain flow is a single GLOBAL reading
(ETH balances across exchanges, doesn't vary per traded pair), while funding
rate and OI are PER-TICKER (BTC funding != ETH funding). Everything here is
keyed by ticker accordingly.
"""

import asyncio
import logging
import os
import time
from typing import Dict, Optional, Any

import config as _cfg
from config import DATA_DIR, safe_json_load, safe_json_save

logger = logging.getLogger(__name__)

# =====================================================================
# 💾  CACHE (per-ticker, TTL = config.DERIVATIVES_CACHE_TTL)
# =====================================================================
_cache: Dict[str, tuple] = {}  # ticker -> (timestamp, bias_dict)

# 🆕 Previous OI snapshot per ticker, persisted so a restart doesn't lose the
# baseline and delay OI-delta analysis by a full cache cycle — same reasoning
# as onchain.py's exchange-balance baseline.
_OI_BASELINE_FILE = os.path.join(DATA_DIR, "derivatives_oi_baseline.json")
_oi_baseline: Dict[str, Dict[str, Any]] = safe_json_load(_OI_BASELINE_FILE, {})

# 🆕 Throttle for the OI baseline disk write. The in-memory dict is updated
# on every fetch (needed for accurate deltas), but persisting to disk on
# every single fetch (every DERIVATIVES_CACHE_TTL, per ticker) is disk
# writes the freshness doesn't need — a restart only loses at most
# _OI_BASELINE_SAVE_INTERVAL seconds of baseline accuracy either way, same
# tradeoff as most other periodic snapshots in this project. Graceful
# shutdown (main.py's _flush_state_to_disk) calls flush_oi_baseline() to
# persist the final in-memory state regardless of the throttle.
_OI_BASELINE_SAVE_INTERVAL = 300  # seconds
_oi_baseline_last_saved = 0.0
_oi_baseline_dirty = False


def clear_derivatives_cache():
    """Clears the TTL cache (does not touch the OI baseline)."""
    _cache.clear()
    logger.info("[DERIVATIVES] TTL cache cleared (OI baseline preserved)")


def clear_derivatives_cache_full():
    """Full reset, including the OI baseline (use with !reset_cache)."""
    _cache.clear()
    _oi_baseline.clear()
    safe_json_save(_OI_BASELINE_FILE, {})
    logger.info("[DERIVATIVES] Full cache cleared (including OI baseline)")


def _save_oi_baseline_throttled():
    """Marks the baseline dirty and persists it if enough time has passed
    since the last disk write (see _OI_BASELINE_SAVE_INTERVAL)."""
    global _oi_baseline_last_saved, _oi_baseline_dirty
    _oi_baseline_dirty = True
    now = time.time()
    if now - _oi_baseline_last_saved >= _OI_BASELINE_SAVE_INTERVAL:
        safe_json_save(_OI_BASELINE_FILE, _oi_baseline)
        _oi_baseline_last_saved = now
        _oi_baseline_dirty = False


def flush_oi_baseline():
    """Forces an immediate save regardless of the throttle interval — call
    on graceful shutdown so the last in-memory baseline isn't lost."""
    global _oi_baseline_last_saved, _oi_baseline_dirty
    if _oi_baseline_dirty:
        safe_json_save(_OI_BASELINE_FILE, _oi_baseline)
        _oi_baseline_last_saved = time.time()
        _oi_baseline_dirty = False


def clear_ticker_cache(ticker: str):
    """Drops one ticker's TTL cache entry and OI baseline (use with !remove).

    Without this, removing and later re-adding a ticker reuses the stale
    baseline from before removal, so the first post-re-add OI delta is
    computed against whatever OI happened to be current when the ticker was
    removed rather than the current run — a misleading first reading."""
    _cache.pop(ticker, None)
    had_baseline = _oi_baseline.pop(ticker, None) is not None
    if had_baseline:
        safe_json_save(_OI_BASELINE_FILE, _oi_baseline)
    logger.info(f"[DERIVATIVES] Cleared cache/baseline for {ticker}")


# =====================================================================
# 📡  FETCH — funding rate & open interest via ccxt
# =====================================================================

def _to_swap_symbol(ticker: str) -> str:
    """Converts a spot-format ticker ('BTC/USDT') into ccxt's unified swap
    symbol format ('BTC/USDT:USDT').

    Gate.io's exchange instance loads both spot and swap markets under one
    ccxt object, even with options.defaultType='swap' — the plain
    spot-format symbol stays ambiguous, and fetch_funding_rate/
    fetch_open_interest_history both raise ("supports swap contracts only")
    without the explicit :SETTLE suffix that unambiguously points at the
    swap market. Every pair this bot tracks is USDT-quoted, so the
    settlement currency is always the quote currency (USDT-margined linear
    perpetual) — this isn't Gate.io-specific in principle, but Gate.io is
    the only exchange this bot talks to.
    """
    if ":" in ticker:
        return ticker  # already in swap format
    base, _, quote = ticker.partition("/")
    return f"{base}/{quote}:{quote}"


async def _fetch_funding_rate(exchange, ticker: str) -> Optional[float]:
    """Returns the current funding rate as a fraction (e.g. 0.0001 = 0.01%),
    or None if unavailable."""
    try:
        data = await asyncio.to_thread(exchange.fetch_funding_rate, _to_swap_symbol(ticker))
        rate = data.get("fundingRate")
        return float(rate) if rate is not None else None
    except Exception as e:
        logger.warning(f"[DERIVATIVES] fetch_funding_rate failed for {ticker}: {e}")
        return None


async def _fetch_open_interest(exchange, ticker: str) -> Optional[float]:
    """Returns current open interest (in the exchange's native units for
    this symbol — usually base currency or contracts; we only ever use it
    as a relative delta, never an absolute cross-pair comparison, so the
    unit doesn't need to be normalized).

    🆕 FIX: Gate.io doesn't implement fetchOpenInterest at all in ccxt
    (exchange.has['fetchOpenInterest'] is False — calling it raises
    "is not supported yet", not a symbol-format problem like funding rate
    above). fetchOpenInterestHistory IS supported, so we pull the shortest
    available window (5m candles) with limit=1 and read the single most
    recent point instead — same information, different endpoint.
    """
    try:
        history = await asyncio.to_thread(
            exchange.fetch_open_interest_history, _to_swap_symbol(ticker), "5m", None, 1
        )
        if not history:
            return None
        latest = history[-1]
        oi = latest.get("openInterestAmount") or latest.get("openInterestValue") or latest.get("openInterest")
        return float(oi) if oi is not None else None
    except Exception as e:
        logger.warning(f"[DERIVATIVES] fetch_open_interest failed for {ticker}: {e}")
        return None


async def _get_oi_delta(exchange, ticker: str) -> Dict:
    """Compares current OI against the last snapshot for this ticker.

    Same "first_run" pattern as onchain.get_eth_flow_delta(): on the first
    call for a ticker, save the baseline and report no delta yet — the real
    delta is available from the next refresh cycle onward.
    """
    curr_oi = await _fetch_open_interest(exchange, ticker)
    now = time.time()

    if curr_oi is None:
        return {"delta_pct": 0.0, "direction": "unknown", "note": "fetch_failed"}

    baseline = _oi_baseline.get(ticker)
    if baseline is None or baseline.get("oi") is None:
        _oi_baseline[ticker] = {"oi": curr_oi, "ts": now}
        _save_oi_baseline_throttled()
        return {"delta_pct": 0.0, "direction": "neutral", "note": "first_run"}

    prev_oi = baseline["oi"]
    delta_pct = (curr_oi - prev_oi) / prev_oi if prev_oi else 0.0

    _oi_baseline[ticker] = {"oi": curr_oi, "ts": now}
    _save_oi_baseline_throttled()

    if delta_pct > _cfg.OI_DELTA_THRESHOLD:
        direction = "rising"
    elif delta_pct < -_cfg.OI_DELTA_THRESHOLD:
        direction = "falling"
    else:
        direction = "flat"

    return {"delta_pct": round(delta_pct, 4), "direction": direction, "note": "ok"}


# =====================================================================
# 🧠  DERIVATIVES BIAS — per ticker
# =====================================================================

async def get_derivatives_bias(exchange, ticker: str) -> Optional[Dict]:
    """
    Main entry point. Returns the same bias dict shape as
    onchain.get_onchain_bias() — bias_long/short, tp_mult/sl_mult (long and
    short), lev_delta, summary — so bot.py can combine the two with
    derivatives.combine_biases() before passing a single merged dict into
    check_signals(), same as before.

    Returns None if derivatives data isn't applicable (spot mode) or
    disabled.
    """
    if not _cfg.DERIVATIVES_ENABLED or _cfg.MARKET_MODE != "futures":
        return None

    cached = _cache.get(ticker)
    if cached and (time.time() - cached[0]) < _cfg.DERIVATIVES_CACHE_TTL:
        return cached[1]

    funding_rate, oi_delta = await asyncio.gather(
        _fetch_funding_rate(exchange, ticker),
        _get_oi_delta(exchange, ticker),
        return_exceptions=True,
    )
    if isinstance(funding_rate, Exception):
        funding_rate = None
    if isinstance(oi_delta, Exception):
        oi_delta = {"delta_pct": 0.0, "direction": "unknown", "note": "error"}

    bias_long = 0
    bias_short = 0
    lev_delta = 0
    reasons = []

    # ── Funding rate — contrarian: positive funding = crowd long, paying
    # shorts = bearish tilt; negative = crowd short = bullish tilt. ──
    if funding_rate is not None:
        abs_fr = abs(funding_rate)
        threshold = _cfg.FUNDING_RATE_EXTREME_THRESHOLD
        if abs_fr >= threshold:
            # Scale contribution with how far past the threshold we are,
            # capped the same way onchain's flow_pts is (weak/normal/large).
            strength_mult = min(2.0, abs_fr / threshold)
            pts = int(round(6 * strength_mult))
            if funding_rate > 0:
                bias_short += pts
                bias_long -= pts
                reasons.append(f"Funding +{funding_rate*100:.3f}% (crowd long, contrarian short) 🔴")
            else:
                bias_long += pts
                bias_short -= pts
                reasons.append(f"Funding {funding_rate*100:.3f}% (crowd short, contrarian long) 🟢")

    # ── Open interest — feeds leverage confidence, not direction (OI alone
    # doesn't tell us which way the market is leaning, only how much
    # conviction is building or unwinding). ──
    if oi_delta.get("note") == "ok":
        direction = oi_delta["direction"]
        # 🆕 Scale the lev_delta contribution with how far past the
        # threshold the OI move actually is — previously a 3% OI change and
        # a 30% OI change both contributed a flat +/-1, same as funding
        # rate's own strength_mult treatment above. "rising"/"falling" only
        # fire past OI_DELTA_THRESHOLD already, so this ratio is always
        # >= 1.0 here; capped at 2.0 so one input can't dominate lev_delta
        # on its own (combine_biases() then caps the combined total at +/-2
        # regardless).
        if direction in ("rising", "falling"):
            oi_strength_mult = min(2.0, abs(oi_delta["delta_pct"]) / _cfg.OI_DELTA_THRESHOLD)
            oi_lev_contribution = max(1, int(round(oi_strength_mult)))
            if direction == "rising":
                lev_delta += oi_lev_contribution
                reasons.append(f"OI rising {oi_delta['delta_pct']*100:+.1f}% (x{oi_strength_mult:.1f}, new positioning building) 📈")
            else:
                lev_delta -= oi_lev_contribution
                reasons.append(f"OI falling {oi_delta['delta_pct']*100:+.1f}% (x{oi_strength_mult:.1f}, positions unwinding) 📉")

    bias_long = max(-15, min(15, bias_long))
    bias_short = max(-15, min(15, bias_short))

    tp_mult_long = 1.05 if bias_long > bias_short else (0.95 if bias_long < bias_short else 1.0)
    tp_mult_short = 1.05 if bias_short > bias_long else (0.95 if bias_short < bias_long else 1.0)

    result = {
        "bias_long": bias_long,
        "bias_short": bias_short,
        "tp_mult_long": round(tp_mult_long, 3),
        "tp_mult_short": round(tp_mult_short, 3),
        "sl_mult_long": 1.0,
        "sl_mult_short": 1.0,
        "lev_delta": lev_delta,
        "summary": " | ".join(reasons) if reasons else "📉 Derivatives neutral.",
        "funding_rate": funding_rate,
        "oi_delta_pct": oi_delta.get("delta_pct", 0.0),
        "oi_direction": oi_delta.get("direction", "unknown"),
    }

    _cache[ticker] = (time.time(), result)
    logger.info(f"[DERIVATIVES] {ticker}: bias_long={bias_long:+d} short={bias_short:+d} | lev_delta={lev_delta:+d} | {result['summary']}")
    return result


# 🆕 Hard cap for the combined (onchain x derivatives) TP/SL multiplier —
# see the comment in combine_biases(). Symmetric around 1.0 for the
# discount side (0.95 * 0.95 ~= 0.9025, clamped up to 1/COMBINED_MULT_CAP).
COMBINED_MULT_CAP = 1.20


def _clamp_mult(mult: float) -> float:
    lo = 1.0 / COMBINED_MULT_CAP
    return max(lo, min(COMBINED_MULT_CAP, mult))


def combine_biases(onchain_bias: Optional[Dict], derivatives_bias: Optional[Dict]) -> Optional[Dict]:
    """
    Merges the on-chain bias (global, one per scan cycle) with the
    derivatives bias (per-ticker) into a single dict of the same shape, so
    every downstream consumer (apply_onchain_with_safety, chart_data's
    market pulse, embeds) keeps reading one bias input, unaware that it may
    now be composed of two independent sources.

    Either input can be None (feature disabled, spot mode, fetch failed) —
    in that case the other is returned as-is, or None if both are.
    """
    if onchain_bias is None and derivatives_bias is None:
        return None
    if onchain_bias is None:
        return derivatives_bias
    if derivatives_bias is None:
        return onchain_bias

    combined = dict(onchain_bias)  # start from onchain_bias's extra fields (fear_and_greed, flow, etc.)
    combined["bias_long"] = max(-20, min(20, onchain_bias.get("bias_long", 0) + derivatives_bias.get("bias_long", 0)))
    combined["bias_short"] = max(-20, min(20, onchain_bias.get("bias_short", 0) + derivatives_bias.get("bias_short", 0)))
    # 🆕 On-chain and derivatives TP/SL multipliers are two independent
    # sources multiplied together, so they can compound past what either
    # one alone was calibrated for (e.g. 1.10 x 1.05 = 1.155, 15.5% beyond
    # the base TP). apply_onchain_with_safety() still checks R:R downstream,
    # but capping the combined multiplier here keeps a single "everything
    # agrees" scenario from pushing TP/SL to an extreme no individual
    # source would produce on its own — same reasoning as lev_delta's cap
    # just below.
    combined["tp_mult_long"] = round(_clamp_mult(onchain_bias.get("tp_mult_long", 1.0) * derivatives_bias.get("tp_mult_long", 1.0)), 3)
    combined["tp_mult_short"] = round(_clamp_mult(onchain_bias.get("tp_mult_short", 1.0) * derivatives_bias.get("tp_mult_short", 1.0)), 3)
    combined["sl_mult_long"] = round(_clamp_mult(onchain_bias.get("sl_mult_long", 1.0) * derivatives_bias.get("sl_mult_long", 1.0)), 3)
    combined["sl_mult_short"] = round(_clamp_mult(onchain_bias.get("sl_mult_short", 1.0) * derivatives_bias.get("sl_mult_short", 1.0)), 3)
    combined["lev_delta"] = max(-2, min(2, onchain_bias.get("lev_delta", 0) + derivatives_bias.get("lev_delta", 0)))
    onchain_summary = onchain_bias.get("summary", "")
    derivatives_summary = derivatives_bias.get("summary", "")
    combined["summary"] = " | ".join(s for s in (onchain_summary, derivatives_summary) if s and "neutral" not in s.lower())
    if not combined["summary"]:
        combined["summary"] = "📊 On-chain + derivatives neutral."
    combined["derivatives"] = {
        "funding_rate": derivatives_bias.get("funding_rate"),
        "oi_delta_pct": derivatives_bias.get("oi_delta_pct"),
        "oi_direction": derivatives_bias.get("oi_direction"),
    }
    return combined


def format_derivatives_report(bias: Optional[Dict]) -> str:
    """Formats a derivatives bias dict for Discord (!onchain report footer / !derivatives)."""
    if not bias:
        return "📉 Derivatives data not available (spot mode, disabled, or not yet fetched)."
    fr = bias.get("funding_rate")
    oi_pct = bias.get("oi_delta_pct", 0.0)
    oi_dir = bias.get("oi_direction", "unknown")
    lines = ["**📉 Derivatives (Futures)**", ""]
    lines.append(f"**Funding Rate:** `{fr*100:+.4f}%`" if fr is not None else "**Funding Rate:** unavailable")
    lines.append(f"**Open Interest:** {oi_dir} (`{oi_pct*100:+.1f}%` since last check)")
    lines.append("")
    lines.append(f"**Summary:** {bias.get('summary', '—')}")
    return "\n".join(lines)
