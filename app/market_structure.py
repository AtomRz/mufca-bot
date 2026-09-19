"""
MUFCA v4.1 - Market Structure Engine

Single source of truth for:
- Pivot support/resistance
- Demand/supply zones
- Volume Profile (POC / Value Area)
- Price location and zone interaction

No signal/TP policy lives here. Consumers only read the snapshot.
All source comments and strings are ASCII-only.
"""

import threading
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict

import numpy as np
import pandas as pd

import config as _cfg
from utils import round_price


# =====================================================================
# Helpers
# =====================================================================

def _safe_float(value, default=0.0):
    try:
        value = float(value)
        return value if np.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _pct_distance(a: float, b: float) -> float:
    if abs(b) <= 1e-12:
        return 0.0
    return abs(a - b) / abs(b) * 100.0


def _cluster_levels(
    levels: List[float],
    max_n: int,
    ref_price: float,
    tol: float = 0.005,
) -> List[Tuple[float, int]]:
    if not levels:
        return []
    levels = sorted(float(x) for x in levels if np.isfinite(x))
    if not levels:
        return []

    clustered: List[Tuple[float, int]] = []
    used = [False] * len(levels)
    for i, level in enumerate(levels):
        if used[i]:
            continue
        used[i] = True
        cluster = [level]
        for j in range(i + 1, len(levels)):
            if used[j]:
                continue
            if abs(levels[j] - level) / (abs(level) + 1e-8) < tol:
                cluster.append(levels[j])
                used[j] = True
        clustered.append((float(np.mean(cluster)), len(cluster)))

    if len(clustered) > max_n:
        clustered.sort(key=lambda x: abs(x[0] - ref_price))
        clustered = clustered[:max_n]
    return sorted(clustered, key=lambda x: x[0])


def _levels_to_dicts(pairs: List[Tuple[float, int]], ref_price: float) -> List[Dict]:
    return [
        {
            "price": round_price(price),
            "touches": int(touches),
            "distance_pct": round(_pct_distance(price, ref_price), 3),
        }
        for price, touches in pairs
    ]


def _atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(max(2, int(period)), min_periods=max(2, int(period))).mean()


# =====================================================================
# Pivot Support / Resistance
# =====================================================================

def calc_support_resistance(
    df: pd.DataFrame,
    pivot_window: int = 10,
    max_levels: int = 4,
) -> Dict:
    """Return legacy S/R plus touch-enriched level dictionaries."""
    if len(df) < max(5, pivot_window * 2 + 2):
        return {
            "support": [],
            "resistance": [],
            "pivot": [],
            "support_levels": [],
            "resistance_levels": [],
        }

    close = df["close"]
    high = df["high"]
    low = df["low"]
    last_close = _safe_float(close.iloc[-2] if len(close) >= 2 else close.iloc[-1])
    w = max(1, int(pivot_window))
    supports: List[float] = []
    resistances: List[float] = []

    for i in range(w, len(df) - w):
        hi_window = high.iloc[i - w:i + w + 1]
        lo_window = low.iloc[i - w:i + w + 1]
        if high.iloc[i] >= hi_window.max():
            resistances.append(_safe_float(high.iloc[i]))
        if low.iloc[i] <= lo_window.min():
            supports.append(_safe_float(low.iloc[i]))

    supports = [x for x in supports if x < last_close and _pct_distance(x, last_close) <= 12.0]
    resistances = [x for x in resistances if x > last_close and _pct_distance(x, last_close) <= 12.0]

    supports_ct = _cluster_levels(supports, max_levels, last_close)
    resistances_ct = _cluster_levels(resistances, max_levels, last_close)

    return {
        "support": [price for price, _ in supports_ct],
        "resistance": [price for price, _ in resistances_ct],
        "pivot": [],
        "support_levels": _levels_to_dicts(supports_ct, last_close),
        "resistance_levels": _levels_to_dicts(resistances_ct, last_close),
    }


# =====================================================================
# Demand / Supply Zone Engine
# =====================================================================

def _base_mask(
    df: pd.DataFrame,
    atr: pd.Series,
    i: int,
    base_bars: int,
    max_base_atr: float,
) -> bool:
    start = max(0, i - base_bars + 1)
    segment = df.iloc[start:i + 1]
    if len(segment) < 2:
        return False
    width = float(segment["high"].max() - segment["low"].min())
    atr_i = _safe_float(atr.iloc[i], 0.0)
    if atr_i <= 0:
        return False
    return width <= atr_i * max_base_atr


def _zone_overlap(a_low, a_high, b_low, b_high) -> bool:
    return min(a_high, b_high) >= max(a_low, b_low)


def _merge_zones(zones: List[Dict], max_zones: int) -> List[Dict]:
    if not zones:
        return []
    zones = sorted(zones, key=lambda z: (-z["score"], z["low"]))
    kept: List[Dict] = []
    for zone in zones:
        merged = False
        for existing in kept:
            mid_a = (zone["low"] + zone["high"]) / 2.0
            mid_b = (existing["low"] + existing["high"]) / 2.0
            scale = max(abs(mid_a), abs(mid_b), 1e-12)
            overlap = _zone_overlap(
                zone["low"], zone["high"], existing["low"], existing["high"]
            )
            close = abs(mid_a - mid_b) / scale <= 0.006
            if overlap or close:
                new_low = min(existing["low"], zone["low"])
                new_high = max(existing["high"], zone["high"])
                existing["low"] = new_low
                existing["high"] = new_high
                existing["score"] = max(existing["score"], zone["score"])
                existing["touches"] = max(existing["touches"], zone["touches"])
                existing["age"] = min(existing["age"], zone["age"])
                existing["fresh"] = existing["fresh"] and zone["fresh"]
                existing["retests"] = max(existing["retests"], zone["retests"])
                existing["volume_ratio"] = max(existing["volume_ratio"], zone["volume_ratio"])
                existing["displacement_atr"] = max(existing["displacement_atr"], zone["displacement_atr"])
                merged = True
                break
        if not merged:
            kept.append(dict(zone))
        if len(kept) >= max_zones * 3:
            break
    kept.sort(key=lambda z: (-z["score"], z["distance_pct"]))
    return kept[:max_zones]


def _zone_state(
    zone_low: float,
    zone_high: float,
    side: str,
    confirmed: pd.DataFrame,
    created_idx: int,
) -> Tuple[str, int, int]:
    """Classify a zone using bars after creation only."""
    if created_idx >= len(confirmed) - 1:
        return "fresh", 0, 0

    later = confirmed.iloc[created_idx + 1:]
    retests = 0
    broken = False
    for row in later.itertuples():
        high = _safe_float(row.high)
        low = _safe_float(row.low)
        if side == "demand":
            if low <= zone_low:
                broken = True
            if low <= zone_high and high >= zone_low:
                retests += 1
            if _safe_float(row.close) < zone_low:
                broken = True
        else:
            if high >= zone_high:
                broken = True
            if high >= zone_low and low <= zone_high:
                retests += 1
            if _safe_float(row.close) > zone_high:
                broken = True

    if broken:
        return "broken", retests, len(later)
    if retests == 0:
        return "fresh", 0, len(later)
    if retests == 1:
        return "tested", 1, len(later)
    return "weakened", retests, len(later)


def detect_demand_supply_zones(
    df: pd.DataFrame,
    atr_period: int = 14,
    lookback: int = 300,
    base_bars: int = 4,
    impulse_bars: int = 3,
    max_zones: int = 5,
    max_base_atr: float = 1.6,
    min_displacement_atr: float = 1.1,
    min_volume_ratio: float = 1.15,
) -> Dict[str, List[Dict]]:
    """Detect fresh and tested demand/supply zones from confirmed OHLCV bars.

    A zone is formed by a compact base followed by directional displacement.
    The detector never uses the currently forming candle. Zones are scored from
    structure, displacement, volume, freshness, retests, and distance.
    """
    empty = {"demand": [], "supply": []}
    if df is None or len(df) < max(30, atr_period + base_bars + impulse_bars + 5):
        return empty

    confirmed = df.iloc[:-1].copy() if len(df) >= 2 else df.copy()
    if len(confirmed) < 30:
        return empty
    confirmed = confirmed.tail(max(30, int(lookback))).reset_index(drop=True)
    atr = _atr_series(confirmed, atr_period)
    volume_ma = confirmed["volume"].rolling(20, min_periods=5).mean()
    ref_price = _safe_float(confirmed["close"].iloc[-1])

    candidates = {"demand": [], "supply": []}
    start = max(base_bars, atr_period + base_bars)
    end = len(confirmed) - impulse_bars

    for i in range(start, end):
        atr_i = _safe_float(atr.iloc[i], 0.0)
        if atr_i <= 0:
            continue
        if not _base_mask(confirmed, atr, i, base_bars, max_base_atr):
            continue

        base = confirmed.iloc[i - base_bars + 1:i + 1]
        base_low = _safe_float(base["low"].min())
        base_high = _safe_float(base["high"].max())
        base_range = base_high - base_low
        if base_range <= 0:
            continue

        after = confirmed.iloc[i + 1:i + 1 + impulse_bars]
        up_move = _safe_float(after["high"].max()) - _safe_float(base_high)
        down_move = _safe_float(base_low) - _safe_float(after["low"].min())
        vol_ratio = _safe_float(
            confirmed["volume"].iloc[i + 1:i + 1 + impulse_bars].mean()
            / max(_safe_float(volume_ma.iloc[i], 1.0), 1e-12),
            0.0,
        )

        def make_zone(side: str, displacement: float):
            if displacement / atr_i < min_displacement_atr:
                return
            if vol_ratio < min_volume_ratio:
                return
            state, retests, age = _zone_state(
                base_low, base_high, side, confirmed, i
            )
            if state == "broken":
                return

            distance_pct = _pct_distance(
                ref_price,
                base_high if side == "demand" else base_low,
            )
            if distance_pct > 12.0:
                return

            displacement_atr = displacement / atr_i
            freshness_bonus = 15.0 if state == "fresh" else 8.0 if state == "tested" else 2.0
            retest_penalty = min(18.0, retests * 5.0)
            distance_bonus = max(0.0, 12.0 - min(12.0, distance_pct))
            displacement_score = min(25.0, displacement_atr * 10.0)
            volume_score = min(18.0, max(0.0, (vol_ratio - 1.0) * 18.0))
            base_quality = max(0.0, 10.0 - (base_range / atr_i) * 4.0)
            score = max(
                0.0,
                min(
                    100.0,
                    20.0
                    + freshness_bonus
                    + distance_bonus
                    + displacement_score
                    + volume_score
                    + base_quality
                    - retest_penalty,
                ),
            )

            zone = {
                "low": round_price(base_low),
                "high": round_price(base_high),
                "mid": round_price((base_low + base_high) / 2.0),
                "score": round(score, 2),
                "state": state,
                "fresh": state == "fresh",
                "touches": max(1, retests + 1),
                "retests": int(retests),
                "age": int(age),
                "distance_pct": round(distance_pct, 3),
                "volume_ratio": round(vol_ratio, 3),
                "displacement_atr": round(displacement_atr, 3),
                "base_atr": round(base_range / atr_i, 3),
                "created_bar": int(i),
                "source": "base_displacement",
            }
            candidates[side].append(zone)

        if up_move >= down_move:
            make_zone("demand", up_move)
        if down_move > up_move:
            make_zone("supply", down_move)

    return {
        "demand": _merge_zones(candidates["demand"], max_zones),
        "supply": _merge_zones(candidates["supply"], max_zones),
    }


def _nearest_zone(zones: List[Dict], price: float, side: str) -> Optional[Dict]:
    if not zones:
        return None
    if side == "demand":
        candidates = [z for z in zones if z["high"] <= price * 1.002]
    else:
        candidates = [z for z in zones if z["low"] >= price * 0.998]
    if not candidates:
        candidates = zones
    return min(candidates, key=lambda z: abs(z["mid"] - price))


def evaluate_zone_context(price: float, zones: Dict[str, List[Dict]]) -> Dict:
    demand = None
    supply = None
    for z in zones.get("demand", []):
        if z["low"] <= price <= z["high"]:
            demand = z
            break
    for z in zones.get("supply", []):
        if z["low"] <= price <= z["high"]:
            supply = z
            break

    nearest_demand = _nearest_zone(zones.get("demand", []), price, "demand")
    nearest_supply = _nearest_zone(zones.get("supply", []), price, "supply")

    return {
        "in_demand": demand is not None,
        "in_supply": supply is not None,
        "demand": demand,
        "supply": supply,
        "nearest_demand": nearest_demand,
        "nearest_supply": nearest_supply,
        "demand_distance_pct": round(_pct_distance(price, nearest_demand["mid"]), 3) if nearest_demand else None,
        "supply_distance_pct": round(_pct_distance(price, nearest_supply["mid"]), 3) if nearest_supply else None,
    }


# =====================================================================
# Volume Profile
# =====================================================================

def calc_volume_profile(
    df: pd.DataFrame,
    bins: int = 50,
    value_area_pct: float = 0.70,
) -> Dict:
    empty = {"poc": None, "vah": None, "val": None, "bins": []}
    if len(df) < 10 or bins < 2:
        return empty

    price_min = _safe_float(df["low"].min())
    price_max = _safe_float(df["high"].max())
    if price_max <= price_min:
        return empty

    bin_width = (price_max - price_min) / int(bins)
    volume_by_bin = np.zeros(int(bins), dtype=float)

    for row in df.itertuples():
        low = _safe_float(row.low)
        high = _safe_float(row.high)
        vol = _safe_float(row.volume)
        if vol <= 0:
            continue
        if high <= low:
            idx = min(max(int((low - price_min) / bin_width), 0), bins - 1)
            volume_by_bin[idx] += vol
            continue
        first_bin = min(max(int((low - price_min) / bin_width), 0), bins - 1)
        last_bin = min(max(int((high - price_min) / bin_width), 0), bins - 1)
        count = last_bin - first_bin + 1
        volume_by_bin[first_bin:last_bin + 1] += vol / count

    total_volume = float(volume_by_bin.sum())
    if total_volume <= 0:
        return empty

    poc_idx = int(np.argmax(volume_by_bin))
    poc_price = price_min + (poc_idx + 0.5) * bin_width
    target = total_volume * min(max(float(value_area_pct), 0.1), 0.95)
    covered = float(volume_by_bin[poc_idx])
    lo_idx = hi_idx = poc_idx

    while covered < target and (lo_idx > 0 or hi_idx < bins - 1):
        below = float(volume_by_bin[lo_idx - 1]) if lo_idx > 0 else -1.0
        above = float(volume_by_bin[hi_idx + 1]) if hi_idx < bins - 1 else -1.0
        if above >= below:
            hi_idx += 1
            covered += float(volume_by_bin[hi_idx])
        else:
            lo_idx -= 1
            covered += float(volume_by_bin[lo_idx])

    val_price = price_min + lo_idx * bin_width
    vah_price = price_min + (hi_idx + 1) * bin_width
    bins_out = [
        {
            "price": round_price(price_min + (i + 0.5) * bin_width),
            "volume": round(float(volume_by_bin[i]), 4),
        }
        for i in range(bins)
    ]
    return {
        "poc": round_price(poc_price),
        "vah": round_price(vah_price),
        "val": round_price(val_price),
        "bins": bins_out,
    }


def classify_price_location(price: float, poc: Optional[float], vah: Optional[float], val: Optional[float]) -> str:
    if vah is None or val is None:
        return "unknown"
    if price > vah:
        return "above_vah"
    if price < val:
        return "below_val"
    return "in_value_area"


# =====================================================================
# Snapshot and cache
# =====================================================================

@dataclass(frozen=True)
class MarketStructure:
    ticker: str
    timeframe: str
    bar_time: int
    last_close: float
    poc: Optional[float]
    vah: Optional[float]
    val: Optional[float]
    price_location: str
    support_levels: List[Dict] = field(default_factory=list)
    resistance_levels: List[Dict] = field(default_factory=list)
    demand_zones: List[Dict] = field(default_factory=list)
    supply_zones: List[Dict] = field(default_factory=list)
    zone_context: Dict = field(default_factory=dict)


_ms_cache: Dict[Tuple[str, str], Tuple[int, "MarketStructure"]] = {}
_ms_cache_lock = threading.Lock()


def clear_market_structure_cache():
    global _ms_cache
    with _ms_cache_lock:
        _ms_cache = {}


def get_market_structure(
    df: pd.DataFrame,
    ticker: str,
    timeframe: str,
    sr_df: Optional[pd.DataFrame] = None,
) -> MarketStructure:
    """Return one point-in-time snapshot based on the last closed candle."""
    if len(df) < 30:
        raise ValueError("get_market_structure: df needs at least 30 rows")

    bar_time = int(df["timestamp"].iloc[-2])
    cache_key = (ticker, timeframe)
    with _ms_cache_lock:
        cached = _ms_cache.get(cache_key)
        if cached is not None and cached[0] == bar_time:
            return cached[1]

    last_close = _safe_float(df["close"].iloc[-2])
    confirmed_df = df.iloc[:-1]

    vp = {"poc": None, "vah": None, "val": None, "bins": []}
    if getattr(_cfg, "VP_ENABLED", False):
        vp_window = confirmed_df.tail(min(_cfg.VP_LOOKBACK, len(confirmed_df)))
        vp = calc_volume_profile(
            vp_window,
            bins=_cfg.VP_BINS,
            value_area_pct=_cfg.VP_VALUE_AREA_PCT,
        )

    sr_source = sr_df if sr_df is not None and len(sr_df) > len(df) else df
    sr = calc_support_resistance(
        sr_source,
        pivot_window=_cfg.SR_PIVOT_WINDOW,
        max_levels=_cfg.SR_MAX_LEVELS,
    )

    zones = detect_demand_supply_zones(
        df,
        atr_period=getattr(_cfg, "ZONE_ATR_PERIOD", 14),
        lookback=getattr(_cfg, "ZONE_LOOKBACK", 300),
        base_bars=getattr(_cfg, "ZONE_BASE_BARS", 4),
        impulse_bars=getattr(_cfg, "ZONE_IMPULSE_BARS", 3),
        max_zones=getattr(_cfg, "ZONE_MAX_ZONES", 5),
        max_base_atr=getattr(_cfg, "ZONE_MAX_BASE_ATR", 1.6),
        min_displacement_atr=getattr(_cfg, "ZONE_MIN_DISPLACEMENT_ATR", 1.1),
        min_volume_ratio=getattr(_cfg, "ZONE_MIN_VOLUME_RATIO", 1.15),
    )
    zone_context = evaluate_zone_context(last_close, zones)

    snapshot = MarketStructure(
        ticker=ticker,
        timeframe=timeframe,
        bar_time=bar_time,
        last_close=last_close,
        poc=vp.get("poc"),
        vah=vp.get("vah"),
        val=vp.get("val"),
        price_location=classify_price_location(last_close, vp.get("poc"), vp.get("vah"), vp.get("val")),
        support_levels=sr.get("support_levels", []),
        resistance_levels=sr.get("resistance_levels", []),
        demand_zones=zones.get("demand", []),
        supply_zones=zones.get("supply", []),
        zone_context=zone_context,
    )

    with _ms_cache_lock:
        _ms_cache[cache_key] = (bar_time, snapshot)
    return snapshot
