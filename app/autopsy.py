"""
autopsy.py - what did the indicators show when a signal failed, or a reversal was missed?

The indicator optimizer asks "which numbers would have made more money" and, on most
pairs, the honest answer is "indistinguishable from noise". This module asks a different
question of the same backtest history:

  1. LOSING signals - for every trade the bot would have taken, describe the state of
     FRAMA / MFI / Andean / UT Bot / Hurst / CHOP / regime / HTF on the signal bar, and
     find the states that lose more often than the average trade (and that lose in BOTH
     the early and the late part of the history, not just one of them).
  2. MISSED reversals - find every significant swing (ATR-based ZigZag) and check whether a
     signal in the right direction caught its start. For the ones that were missed, say
     why (no trigger fired / a filter rejected it / the track was busy) and describe the
     indicator state a few bars after the turn - the moment a signal could have fired.
  3. Compare: which states are over-represented among the missed turns and the losers.

Everything here is a pure function of arrays and lists (no network, no config reads, no
files), so it can be tested on synthetic data. signals.run_autopsy() feeds it the
trigger-table backtest and the per-bar series from backtest_history(collect_series=True).

It is a MEASUREMENT: a state that "loses more" is a candidate rule to forward-test, not a
finding - dozens of (feature, value) pairs are tested, so some will look good by chance.
The result says how many were tested for that reason.

Feature values are written from the trade's point of view ("with" = agrees with the
trade's side), so long and short trades and rising and falling reversals pool together.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

# Bars after a pivot at which the indicators are read for a MISSED reversal: the earliest
# moment a signal could realistically have fired (a pivot is only knowable afterwards).
SNAP_LAG = 2
# Indicator events (UT Bot flips) younger than this many bars count as "fresh".
FRESH_BARS = 3
UT_LOOKBACK = 20
ANDEAN_AGE_MID = 10

FEATURES = (
    "frama_dir", "frama_slope", "frama_stretch", "mfi_zone", "mfi_dir",
    "andean", "andean_age", "ut_state", "hurst", "chop", "regime", "htf", "candle",
)

FEATURE_HELP = {
    "frama_dir": "FRAMA trend direction vs the trade side",
    "frama_slope": "FRAMA line slope vs the trade side",
    "frama_stretch": "how far price already is from FRAMA in the trade's direction (ATR units)",
    "mfi_zone": "MFI vs its K-Means levels: chasing = already stretched the trade's way, fading = stretched against it",
    "mfi_dir": "MFI direction vs the trade side",
    "andean": "Andean oscillator vs its signal line (bull/bear) vs the trade side",
    "andean_age": "bars since the Andean oscillator last crossed its signal line",
    "ut_state": "last UT Bot flip: direction vs the trade side and how old",
    "hurst": "Hurst exponent bucket",
    "chop": "CHOP index bucket (high = choppy)",
    "regime": "volatility regime from ATR%",
    "htf": "higher-timeframe bias vs the trade side",
    "candle": "signal bar's body direction vs the trade side",
}


# ---------------------------------------------------------------------------
# ZigZag
# ---------------------------------------------------------------------------
def zigzag(high, low, atr, mult: float) -> List[Tuple[int, str]]:
    """Confirmed swing pivots [(bar_index, "low"|"high"), ...] in time order.

    A swing is confirmed once price has moved `mult` x ATR (ATR of the extreme bar) away
    from the extreme in the other direction. The last, still-running swing is not
    reported (it is not confirmed yet). NaN ATR never confirms anything."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    atr = np.asarray(atr, dtype=float)
    n = len(high)
    pivots: List[Tuple[int, str]] = []
    if n < 3 or mult <= 0:
        return pivots
    hi_i = lo_i = 0
    trend = 0
    for i in range(1, n):
        if trend == 0:
            if high[i] > high[hi_i]:
                hi_i = i
            if low[i] < low[lo_i]:
                lo_i = i
            if hi_i > lo_i and high[hi_i] - low[lo_i] >= mult * atr[lo_i]:
                pivots.append((lo_i, "low"))
                trend = 1
            elif lo_i > hi_i and high[hi_i] - low[lo_i] >= mult * atr[hi_i]:
                pivots.append((hi_i, "high"))
                trend = -1
        elif trend == 1:
            if high[i] >= high[hi_i]:
                hi_i = i
            elif high[hi_i] - low[i] >= mult * atr[hi_i]:
                pivots.append((hi_i, "high"))
                trend = -1
                lo_i = i
        else:
            if low[i] <= low[lo_i]:
                lo_i = i
            elif high[i] - low[lo_i] >= mult * atr[lo_i]:
                pivots.append((lo_i, "low"))
                trend = 1
                hi_i = i
    return pivots


def swing_legs(pivots: List[Tuple[int, str]], high, low) -> List[Dict]:
    """One dict per CONFIRMED leg (pivot -> next pivot): the tradable move.
    A leg that starts at a low is a rise (side=+1, a long opportunity), one that starts
    at a high is a fall (side=-1)."""
    legs: List[Dict] = []
    for k in range(len(pivots) - 1):
        i, kind = pivots[k]
        j, _ = pivots[k + 1]
        p0 = float(low[i]) if kind == "low" else float(high[i])
        p1 = float(high[j]) if kind == "low" else float(low[j])
        if p0 <= 0:
            continue
        legs.append({
            "pivot_idx": int(i), "end_idx": int(j), "bars": int(j - i),
            "side": 1 if kind == "low" else -1,
            "leg_pct": round(abs(p1 - p0) / p0 * 100.0, 3),
        })
    return legs


# ---------------------------------------------------------------------------
# Indicator snapshot
# ---------------------------------------------------------------------------
def _sgn(x: float, eps: float = 0.0) -> int:
    return 1 if x > eps else -1 if x < -eps else 0


def _rel(v: int) -> str:
    return "with" if v > 0 else "against" if v < 0 else "flat"


def _bars_since_true(arr, i: int, limit: int) -> Optional[int]:
    """Bars back from i (0 = bar i itself) to the most recent True within `limit`, else None."""
    lo = max(0, i - limit)
    for k in range(i, lo - 1, -1):
        if arr[k]:
            return i - k
    return None


def _bars_since_relation_flip(osc, sig, i: int, limit: int) -> Optional[int]:
    """Bars since sign(osc - sig) last changed, looking back at most `limit` bars."""
    cur = _sgn(osc[i] - sig[i])
    if cur == 0 or not np.isfinite(osc[i]) or not np.isfinite(sig[i]):
        return None
    for k in range(1, limit + 1):
        j = i - k
        if j < 0:
            return None
        d = osc[j] - sig[j]
        if not np.isfinite(d):
            return None
        if _sgn(d) != cur:
            return k - 1
    return limit


def snapshot(S: Dict, i: int, side: int, atr_min: float, atr_max: float, chop_thr: float) -> Dict[str, str]:
    """Categorical description of the indicators on bar i, from a trade of `side`
    (+1 long, -1 short). `S` is the per-bar series dict of backtest_history(collect_series=True).
    A value the data cannot give (warm-up NaN) is reported as "na"."""
    i = int(min(max(i, 1), len(S["close"]) - 1))
    out: Dict[str, str] = {}
    close, atr = float(S["close"][i]), float(S["atr"][i])

    # FRAMA
    out["frama_dir"] = _rel(int(S["frama_dir"][i]) * side) if np.isfinite(S["frama_dir"][i]) else "na"
    fr, fr_prev = float(S["frama"][i]), float(S["frama"][i - 1])
    out["frama_slope"] = _rel(_sgn(fr - fr_prev) * side) if np.isfinite(fr) and np.isfinite(fr_prev) else "na"
    if np.isfinite(fr) and np.isfinite(atr) and atr > 0:
        d = (close - fr) / atr * side
        out["frama_stretch"] = "behind" if d < 0 else "near(0-1)" if d < 1 else "ext(1-2)" if d < 2 else "far(>2)"
    else:
        out["frama_stretch"] = "na"

    # MFI
    mfi, os_, ob_ = float(S["mfi"][i]), float(S["mfi_os"][i]), float(S["mfi_ob"][i])
    if np.isfinite(mfi) and np.isfinite(os_) and np.isfinite(ob_):
        zone = "OS" if mfi < os_ else "OB" if mfi > ob_ else "MID"
        # long: overbought = chasing, oversold = fading (buying the dip); short: the mirror
        out["mfi_zone"] = "mid" if zone == "MID" else ("chasing" if (zone == "OB") == (side > 0) else "fading")
    else:
        out["mfi_zone"] = "na"
    mfi_prev = float(S["mfi"][i - 1])
    out["mfi_dir"] = _rel(_sgn(mfi - mfi_prev) * side) if np.isfinite(mfi) and np.isfinite(mfi_prev) else "na"

    # Andean
    osc, sg = float(S["and_osc"][i]), float(S["and_sig"][i])
    if np.isfinite(osc) and np.isfinite(sg):
        out["andean"] = _rel(_sgn(osc - sg) * side)
        age = _bars_since_relation_flip(S["and_osc"], S["and_sig"], i, ANDEAN_AGE_MID + 1)
        out["andean_age"] = "na" if age is None else (
            f"fresh(<={FRESH_BARS})" if age <= FRESH_BARS else f"mid(<={ANDEAN_AGE_MID})" if age <= ANDEAN_AGE_MID
            else f"old(>{ANDEAN_AGE_MID})")
    else:
        out["andean"] = out["andean_age"] = "na"

    # UT Bot: the most recent flip of either direction
    b_buy = _bars_since_true(S["ut_buy"], i, UT_LOOKBACK)
    b_sell = _bars_since_true(S["ut_sell"], i, UT_LOOKBACK)
    if b_buy is None and b_sell is None:
        out["ut_state"] = "none"
    else:
        if b_sell is None or (b_buy is not None and b_buy <= b_sell):
            direction, age = 1, b_buy
        else:
            direction, age = -1, b_sell
        out["ut_state"] = f"{_rel(direction * side)}_{'fresh' if age <= FRESH_BARS else 'old'}"

    # Hurst / CHOP / regime
    h = float(S["hurst"][i])
    out["hurst"] = "na" if not np.isfinite(h) else "<0.45" if h < 0.45 else "0.45-0.55" if h <= 0.55 else ">0.55"
    c = float(S["chop"][i])
    out["chop"] = "na" if not np.isfinite(c) else "low(<38.2)" if c < 38.2 else (
        "mid" if c < chop_thr else f"high(>={chop_thr:g})")
    ap = float(S["atr_pct"][i])
    out["regime"] = "na" if not np.isfinite(ap) else "CHAOS" if ap > atr_max else "TREND" if ap > atr_min * 1.5 else "NORMAL"

    # HTF and the bar itself
    hb = float(S["htf_bias"][i])
    out["htf"] = "none" if hb == 0 or not np.isfinite(hb) else _rel(_sgn(hb) * side)
    body = float(S["close"][i]) - float(S["open"][i])
    out["candle"] = _rel(_sgn(body) * side)
    return out


# ---------------------------------------------------------------------------
# Losing trades
# ---------------------------------------------------------------------------
def _net(t: Dict, fee_pct: float) -> float:
    return float(t["moved_pct"]) - fee_pct


def _win(rows: List[Dict], key: str = "net") -> Tuple[int, float]:
    return len(rows), round(sum(r[key] for r in rows), 4)


# An enabled filter already decides its own feature: every taken trade passed it (so the
# losers table would only ever see the passing value) and every trigger it rejected is
# "missed because of it" by construction (so the missed table would just echo the filter).
# Those features are left out of both tables while the filter is on.
FILTER_FEATURE = {
    "frama": "frama_dir", "slope": "frama_slope", "chop": "chop", "htf": "htf", "hurst": "hurst", "atr": "regime",
}


# Evidence needed for a ✅ (on top of "holds in both the early and the late window").
LOSER_MIN_N = 15          # trades in the state
MIN_Z_ONE_SIDED = 1.64    # loss rate above the average one, ~95% one-sided
MISSED_MIN_LIFT = 1.15    # missed share vs the average missed share
MIN_Z_TWO_SIDED = 2.0     # ~95% two-sided


def _z(p: float, base: float, n: int) -> float:
    """z-score of an observed rate p over n trials against the base rate."""
    if n <= 0 or not (0.0 < base < 1.0):
        return 0.0
    return (p - base) / ((base * (1.0 - base) / n) ** 0.5)


def features_to_test(enabled_filters: List[str]) -> Tuple[str, ...]:
    skip = {FILTER_FEATURE[f] for f in enabled_filters if f in FILTER_FEATURE}
    return tuple(f for f in FEATURES if f not in skip)


def loser_feature_table(rows: List[Dict], cutoff_idx: int, min_n: int = 8, min_split: int = 4,
                        features: Tuple[str, ...] = FEATURES) -> Dict:
    """`rows`: one per taken trade {"idx", "net", "snap": {feature: value}}.
    For every (feature, value) seen at least `min_n` times: how often it lost against the
    overall loss rate, and what skipping that state would have changed - on the early
    window (idx < cutoff_idx) and the late one separately. `holds` = skipping it would
    have helped in BOTH windows with at least `min_split` trades each."""
    n = len(rows)
    if n == 0:
        return {"base_loss_rate": 0.0, "n_tested": 0, "candidates": []}
    base = sum(1 for r in rows if r["net"] < 0) / n
    cands: List[Dict] = []
    tested = 0
    for f in features:
        values = sorted({r["snap"].get(f, "na") for r in rows})
        for v in values:
            if v == "na":
                continue
            sub = [r for r in rows if r["snap"].get(f) == v]
            if len(sub) < min_n or len(sub) == n:
                continue
            tested += 1
            losers = sum(1 for r in sub if r["net"] < 0)
            tr = [r for r in sub if r["idx"] < cutoff_idx]
            te = [r for r in sub if r["idx"] >= cutoff_idx]
            n_tr, tot_tr = _win(tr)
            n_te, tot_te = _win(te)
            n_all, tot_all = _win(sub)
            z = _z(losers / n_all, base, n_all)
            cands.append({
                "feature": f, "value": v, "n": n_all,
                "loss_rate": round(losers / n_all, 4),
                "lift": round((losers / n_all) / base, 3) if base > 0 else 0.0,
                "avg_net": round(tot_all / n_all, 4),
                "skip_gain": round(-tot_all, 4),  # total net PnL removed by skipping it (>0 = it lost money)
                "early": {"n": n_tr, "net": tot_tr}, "late": {"n": n_te, "net": tot_te},
                "z": round(z, 2),
                # a ✅ needs (a) the subset to lose money in BOTH windows, (b) enough trades overall
                # and (c) a loss rate clearly above the average one - 9 trades never qualify
                "holds": bool(n_tr >= min_split and n_te >= min_split and tot_tr < 0 and tot_te < 0
                              and n_all >= LOSER_MIN_N and z >= MIN_Z_ONE_SIDED),
            })
    cands.sort(key=lambda c: (not c["holds"], -c["skip_gain"]))
    return {"base_loss_rate": round(base, 4), "n_tested": tested, "candidates": cands}


# ---------------------------------------------------------------------------
# Missed reversals
# ---------------------------------------------------------------------------
def classify_legs(legs: List[Dict], taken: List[Dict], triggers: List[Dict], enabled_filters: List[str],
                  catch_pre: int = 2) -> List[Dict]:
    """Adds "status" to every leg:
      caught   - a trade the bot takes, in the leg's direction, was entered between
                 `catch_pre` bars before the turn and the first third of the leg;
      filtered - no such trade, but a raw trigger fired in that window and an enabled
                 filter rejected it ("blockers" lists them);
      busy     - a trigger fired, nothing rejected it, but the track was occupied/cooling down;
      none     - no raw trigger in the window at all (the indicators never agreed).
    `taken` / `triggers` are trigger-table rows ({"idx","track","side": "long"|"short","fails"})."""
    taken_by_side: Dict[int, List[int]] = {1: [], -1: []}
    for t in taken:
        taken_by_side[1 if t["side"] == "long" else -1].append(int(t["idx"]))
    trig_by_side: Dict[int, List[Dict]] = {1: [], -1: []}
    for t in triggers:
        trig_by_side[1 if t["side"] == "long" else -1].append(t)

    out: List[Dict] = []
    for lg in legs:
        lo = lg["pivot_idx"] - catch_pre
        hi = lg["pivot_idx"] + max(4, lg["bars"] // 3)
        d = dict(lg)
        d["window"] = (lo, hi)
        if any(lo <= i <= hi for i in taken_by_side[lg["side"]]):
            d["status"], d["blockers"] = "caught", []
        else:
            cand = [t for t in trig_by_side[lg["side"]] if lo <= int(t["idx"]) <= hi]
            if not cand:
                d["status"], d["blockers"] = "none", []
            else:
                # the trigger that came closest to being taken = fewest enabled filters against it
                best = min(cand, key=lambda t: len([f for f in t["fails"] if f in enabled_filters]))
                blockers = [f for f in best["fails"] if f in enabled_filters]
                d["status"], d["blockers"] = ("filtered", blockers) if blockers else ("busy", [])
        out.append(d)
    return out


def missed_feature_table(legs: List[Dict], cutoff_idx: int, min_n: int = 8, min_split: int = 3,
                         features: Tuple[str, ...] = FEATURES) -> Dict:
    """`legs`: classified legs with "snap" (indicator state SNAP_LAG bars after the turn).
    For every (feature, value): how many turns were caught vs missed, and the missed share
    against the overall missed share. `holds` = over-represented among the missed turns in
    both the early and the late window."""
    n = len(legs)
    if n == 0:
        return {"base_missed_share": 0.0, "n_tested": 0, "candidates": []}
    missed_all = sum(1 for g in legs if g["status"] != "caught")
    base = missed_all / n

    def share(rows):
        return (sum(1 for g in rows if g["status"] != "caught") / len(rows)) if rows else 0.0

    cands: List[Dict] = []
    tested = 0
    for f in features:
        for v in sorted({g["snap"].get(f, "na") for g in legs}):
            if v == "na":
                continue
            sub = [g for g in legs if g["snap"].get(f) == v]
            if len(sub) < min_n or len(sub) == n:
                continue
            tested += 1
            m = sum(1 for g in sub if g["status"] != "caught")
            early = [g for g in sub if g["pivot_idx"] < cutoff_idx]
            late = [g for g in sub if g["pivot_idx"] >= cutoff_idx]
            early_all = [g for g in legs if g["pivot_idx"] < cutoff_idx]
            late_all = [g for g in legs if g["pivot_idx"] >= cutoff_idx]
            lift = (m / len(sub)) / base if base > 0 else 0.0
            z = _z(m / len(sub), base, len(sub))
            # a ✅ needs the state to be over-represented in BOTH windows AND to stand clearly above
            # the average missed share (lift and z), not just to be a hair above it
            holds = bool(len(early) >= min_split and len(late) >= min_split
                         and share(early) > share(early_all) and share(late) > share(late_all)
                         and lift >= MISSED_MIN_LIFT and z >= MIN_Z_TWO_SIDED)
            cands.append({
                "feature": f, "value": v, "n": len(sub), "caught": len(sub) - m, "missed": m,
                "missed_share": round(m / len(sub), 4), "z": round(z, 2),
                "lift": round(lift, 3),
                "missed_leg_pct": round(sum(g["leg_pct"] for g in sub if g["status"] != "caught"), 2),
                "holds": holds,
            })
    cands.sort(key=lambda c: (not c["holds"], -c["lift"], -c["missed_leg_pct"]))
    return {"base_missed_share": round(base, 4), "n_tested": tested, "candidates": cands}


# ---------------------------------------------------------------------------
# What would switching one filter off have done?
# ---------------------------------------------------------------------------
def _summary(rows: List[Dict], fee_pct: float, cutoff_idx: int) -> Dict:
    early = [r for r in rows if int(r["idx"]) < cutoff_idx]
    late = [r for r in rows if int(r["idx"]) >= cutoff_idx]
    net = lambda rs: sum(_net(r, fee_pct) for r in rs)  # noqa: E731
    return {"n": len(rows), "net": net(rows), "early_n": len(early), "early_net": net(early),
            "late_n": len(late), "late_net": net(late)}


def filter_counterfactuals(flags: Dict[str, bool], enabled_filters: List[str], replay, base_rows: List[Dict],
                           fee_pct: float, cutoff_idx: int) -> List[Dict]:
    """For every enabled filter: replay the same triggers with ONLY that filter off
    (`replay(flags) -> taken rows`, the one-position-at-a-time rule included, so a newly
    allowed trade can displace a later one) and report the change against the current set -
    in trades and in net PnL, early and late window separately.

    verdict: "off_helps"   - switching it off would have made more in BOTH windows,
             "keeps_value" - it would have made less in BOTH windows (the filter earns its keep),
             "mixed"       - the two windows disagree, "no_effect" - no trade changes."""
    base = _summary(base_rows, fee_pct, cutoff_idx)
    out: List[Dict] = []
    for f in enabled_filters:
        alt = _summary(replay({**flags, f: False}), fee_pct, cutoff_idx)
        d_early, d_late = alt["early_net"] - base["early_net"], alt["late_net"] - base["late_net"]
        if alt["n"] == base["n"] and abs(alt["net"] - base["net"]) < 1e-9:
            verdict = "no_effect"
        elif d_early > 0 and d_late > 0:
            verdict = "off_helps"
        elif d_early < 0 and d_late < 0:
            verdict = "keeps_value"
        else:
            verdict = "mixed"
        out.append({
            "filter": f, "extra_trades": alt["n"] - base["n"], "net_off": round(alt["net"], 3),
            "delta": round(alt["net"] - base["net"], 3),
            "early": {"extra": alt["early_n"] - base["early_n"], "delta": round(d_early, 3)},
            "late": {"extra": alt["late_n"] - base["late_n"], "delta": round(d_late, 3)},
            "verdict": verdict,
        })
    out.sort(key=lambda c: -c["delta"])
    return out


# ---------------------------------------------------------------------------
# Exit side: what happened to the trades after they were opened
# ---------------------------------------------------------------------------
EXIT_TYPES = ("tp", "sl_after_tp1", "sl", "cancelled")
SL_TIGHTEN_FACTORS = (0.75, 0.5)
SL_EXACT_FACTORS = (1.5, 1.25, 0.75, 0.5)  # the replay handles wider stops too
FAST_STOP_BARS = 3


def _share(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def replay_trade(S: Dict, t: Dict, k: float, tp1_frac: float, max_hold: int,
                 tp1_first: bool = True) -> Optional[Dict]:
    """Re-plays one trade bar by bar on the real highs/lows with the stop at k x its original
    distance, following the backtest's own rules (signals.backtest_history): TP1 moves the stop
    to entry + tp1_frac x (TP1 - entry) and banks half; the bar that touches TP1 does not check
    the moved stop / TP2 (except the SAME_BAR_TP1_POLICY=tp1_first case: TP1 and the stop in
    one bar -> TP1 is credited and the position closes at the moved stop); after max_hold bars
    the position is closed at the close. All in % of entry, from the trade's point of view.
    tp1_first=False resolves a bar that touches both TP1 and the stop as a plain stop.
    Returns {"pnl", "exit_type", "tp1", "ambiguous"} (ambiguous = a bar touched both the
    stop and TP1 before TP1 was credited) or None when the bars are not there."""
    i = int(t["idx"])
    n = len(S["close"])
    entry = float(S["close"][i])
    if entry <= 0 or i + 1 >= n:
        return None
    long_ = t["side"] == "long"
    sl, tp1, tp2 = k * float(t["sl_pct"]), float(t["tp1_pct"]), float(t["tp2_pct"])
    cur = sl                      # adverse-% level of the stop (negative once it is past entry)
    moved = -tp1_frac * tp1       # adverse-% level of the stop after TP1
    reached = False
    ambiguous = False
    last = min(i + max_hold, n - 1)
    for j in range(i + 1, last + 1):
        h, l = float(S["high"][j]), float(S["low"][j])
        fav = ((h - entry) if long_ else (entry - l)) / entry * 100
        adv = ((entry - l) if long_ else (h - entry)) / entry * 100
        if not reached:
            if fav >= tp1 and fav < tp2 and adv >= cur:
                ambiguous = True
                if tp1_first:
                    return {"pnl": (tp1 + (-moved)) / 2, "exit_type": "sl_after_tp1", "tp1": True,
                            "ambiguous": True}
                return {"pnl": -cur, "exit_type": "sl", "tp1": False, "ambiguous": True}
            if adv >= cur:
                return {"pnl": -cur, "exit_type": "sl", "tp1": False, "ambiguous": ambiguous}
            if fav >= tp2:
                return {"pnl": (tp1 + tp2) / 2, "exit_type": "tp", "tp1": True, "ambiguous": ambiguous}
            if fav >= tp1:
                reached, cur = True, moved
                continue
        else:
            if adv >= cur:
                return {"pnl": (tp1 - cur) / 2, "exit_type": "sl_after_tp1", "tp1": True,
                        "ambiguous": ambiguous}
            if fav >= tp2:
                return {"pnl": (tp1 + tp2) / 2, "exit_type": "tp", "tp1": True, "ambiguous": ambiguous}
    c = float(S["close"][last])
    rem = ((c - entry) if long_ else (entry - c)) / entry * 100
    return {"pnl": (tp1 + rem) / 2 if reached else rem, "exit_type": "cancelled", "tp1": reached,
            "ambiguous": ambiguous}


def tighter_stop_exact(taken: List[Dict], S: Dict, fee_pct: float, cutoff_idx: int,
                       tp1_frac: float, max_hold: int, factors=SL_EXACT_FACTORS) -> Dict:
    """The tighter-stop what-if with the real bar order (no worst-case range): every taken trade
    is re-played with the stop at k x its distance. `check` re-plays at k=1 and compares with
    what the backtest recorded, so a replay that does not reproduce the backtest shows itself
    (check.mismatch > 0 => do not trust the numbers). Same trades are assumed: an earlier exit
    could free the slot for a signal that was skipped, which is not modelled."""
    rows = [t for t in taken if t.get("sl_pct") and t.get("tp1_pct") and t.get("tp2_pct")
            and t.get("moved_pct") is not None]
    base = {}
    mismatch = 0
    for t in rows:
        r = replay_trade(S, t, 1.0, tp1_frac, max_hold)
        base[id(t)] = r
        if r is None or r["exit_type"] != t.get("exit_type") or abs(r["pnl"] - float(t["moved_pct"])) > 0.01:
            mismatch += 1
    early = lambda t: int(t["idx"]) < cutoff_idx  # noqa: E731
    out = []
    for k in factors:
        res = {"factor": k, "bot_rule": {"all": 0.0, "early": 0.0, "late": 0.0},
               "strict": {"all": 0.0, "early": 0.0, "late": 0.0},
               "saved": 0, "cut_winners": 0, "changed": 0, "ambiguous": 0, "n": 0,
               "net_after": 0.0}
        for t in rows:
            if base[id(t)] is None:
                continue
            old = float(t["moved_pct"])
            a = replay_trade(S, t, k, tp1_frac, max_hold, True)
            b = replay_trade(S, t, k, tp1_frac, max_hold, False)
            if a is None or b is None:
                continue
            res["n"] += 1
            w = "early" if early(t) else "late"
            for key, r in (("bot_rule", a), ("strict", b)):
                d = r["pnl"] - old
                res[key]["all"] += d
                res[key][w] += d
            res["net_after"] += a["pnl"] - fee_pct
            if abs(a["pnl"] - old) > 1e-9:
                res["changed"] += 1
                if a["pnl"] > old:
                    res["saved"] += 1
                else:
                    res["cut_winners"] += 1
            if a["ambiguous"]:
                res["ambiguous"] += 1
        for key in ("bot_rule", "strict"):
            res[key] = {w: round(v, 2) for w, v in res[key].items()}
        res["net_after"] = round(res["net_after"], 2)
        out.append(res)
    return {"check": {"n": len(rows), "mismatch": mismatch}, "factors": out}


def exit_autopsy(taken: List[Dict], fee_pct: float, cutoff_idx: int,
                 S: Optional[Dict] = None, tp1_frac: Optional[float] = None,
                 max_hold: Optional[int] = None) -> Dict:
    """How the trades the bot takes ended, from the per-trade exit fields of the trigger
    table (mfe/mae = the way price moved up to the exit, raw_mfe = over the whole holding
    horizon, sl_pct/tp1_pct/tp2_pct = the original stop and targets, all in % of entry).

      mix        - trades and net PnL by exit type;
      never_tp1  - the pure losers (stopped out before TP1): how far toward TP1 they got
                   (near_miss = at least half way), how fast they were stopped, and whether
                   price reached TP1/TP2 AFTER the stop ("shake-out", an upper bound: the
                   path after the stop is not replayed);
      winners_heat - of the trades that reached TP1, how deep they dipped against the
                   original SL (how much room a tighter stop would leave them);
      tighter_sl - what the stop at k x its distance would have done. Trades that never
                   reached TP1 are exact (their whole path is before the exit); for trades
                   that reached TP1 the order of the dip and TP1 is unknown, so the table
                   gives the exact part and a worst case (every deep dip assumed pre-TP1).
    Returns {"available": False} for rows from a backtest without the exit fields."""
    rows = [t for t in taken if t.get("sl_pct") and t.get("tp1_pct") and t.get("mfe") is not None]
    if not rows:
        return {"available": False}
    net = lambda t: float(t["moved_pct"]) - fee_pct  # noqa: E731
    early = lambda t: int(t["idx"]) < cutoff_idx      # noqa: E731

    mix = {}
    for et in EXIT_TYPES:
        sub = [t for t in rows if t.get("exit_type") == et]
        mix[et] = {"n": len(sub), "net": round(sum(net(t) for t in sub), 3),
                   "avg": round(sum(net(t) for t in sub) / len(sub), 3) if sub else 0.0}

    pure = [t for t in rows if t.get("exit_type") == "sl" and not t.get("tp1_reached")]
    prog = [float(t["mfe"]) / float(t["tp1_pct"]) for t in pure]
    buckets = {"<25%": 0, "25-50%": 0, "50-75%": 0, "75-100%": 0}
    for p in prog:
        buckets["<25%" if p < 0.25 else "25-50%" if p < 0.5 else "50-75%" if p < 0.75 else "75-100%"] += 1
    near = [t for t, p in zip(pure, prog) if p >= 0.5]
    fast = [t for t in pure if int(t.get("bars_held", 0)) <= FAST_STOP_BARS]
    shake1 = [t for t in pure if t.get("raw_mfe") is not None and float(t["raw_mfe"]) >= float(t["tp1_pct"])]
    shake2 = [t for t in pure if t.get("raw_mfe") is not None and t.get("tp2_pct")
              and float(t["raw_mfe"]) >= float(t["tp2_pct"])]
    bars = sorted(int(t.get("bars_held", 0)) for t in pure)

    def split(sub):
        e = [t for t in sub if early(t)]
        return {"early": _share(len(e), len([t for t in pure if early(t)])),
                "late": _share(len(sub) - len(e), len([t for t in pure if not early(t)]))}

    never = {
        "n": len(pure), "share_of_trades": _share(len(pure), len(rows)),
        "net": round(sum(net(t) for t in pure), 3),
        "avg_sl_pct": round(sum(float(t["sl_pct"]) for t in pure) / len(pure), 3) if pure else 0.0,
        "progress_to_tp1": buckets,
        "near_miss": {"n": len(near), "share": _share(len(near), len(pure)), "by_window": split(near)},
        "fast_stop": {"n": len(fast), "share": _share(len(fast), len(pure)), "bars": FAST_STOP_BARS},
        "median_bars": bars[len(bars) // 2] if bars else 0,
        "shakeout_tp1": {"n": len(shake1), "share": _share(len(shake1), len(pure)), "by_window": split(shake1)},
        "shakeout_tp2": {"n": len(shake2), "share": _share(len(shake2), len(pure))},
    }

    reached = [t for t in rows if t.get("tp1_reached")]
    dips = sorted(float(t["mae"]) / float(t["sl_pct"]) for t in reached)
    heat = {
        "n": len(reached),
        "dip_ge_50": _share(sum(1 for d in dips if d >= 0.5), len(dips)),
        "dip_ge_75": _share(sum(1 for d in dips if d >= 0.75), len(dips)),
        "median_dip": round(dips[len(dips) // 2], 3) if dips else 0.0,
    }

    tighter = []
    for k in SL_TIGHTEN_FACTORS:
        exact = {"all": 0.0, "early": 0.0, "late": 0.0}
        worst = {"all": 0.0, "early": 0.0, "late": 0.0}
        stopped = deep = 0
        for t in rows:
            lvl = k * float(t["sl_pct"])
            if float(t["mae"]) < lvl:
                continue
            delta = -lvl - float(t["moved_pct"])  # new PnL (stopped at the tighter level) minus the old one
            bucket = exact if not t.get("tp1_reached") else worst
            if not t.get("tp1_reached"):
                stopped += 1
            else:
                deep += 1
            bucket["all"] += delta
            bucket["early" if early(t) else "late"] += delta
        tighter.append({
            "factor": k, "stopped_never_tp1": stopped, "deep_dips_after_tp1": deep,
            "exact": {w: round(v, 2) for w, v in exact.items()},
            "worst_extra": {w: round(v, 2) for w, v in worst.items()},
            "worst_case": {w: round(exact[w] + worst[w], 2) for w in exact},
        })

    exact = (tighter_stop_exact(taken, S, fee_pct, cutoff_idx, tp1_frac, max_hold)
             if S is not None and tp1_frac is not None and max_hold else None)
    return {"available": True, "n": len(rows), "mix": mix, "never_tp1": never, "winners_heat": heat,
            "tighter_sl": tighter, "tighter_exact": exact}


# ---------------------------------------------------------------------------
# One pair / timeframe
# ---------------------------------------------------------------------------
def _ts_str(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def analyze_pair(S: Dict, triggers: List[Dict], taken: List[Dict], enabled_filters: List[str],
                 fee_pct: float, cutoff_idx: int, atr_min: float, atr_max: float, chop_thr: float,
                 pivot_atr_mult: float = 3.0, top: int = 8, examples: int = 5,
                 flags: Optional[Dict[str, bool]] = None, replay=None,
                 tp1_frac: Optional[float] = None, max_hold: Optional[int] = None) -> Dict:
    """The full autopsy of one pair/timeframe. `S` = per-bar series, `triggers` = every raw
    trigger of the trigger-table backtest, `taken` = the subset the live filter set takes.
    `flags` + `replay` (flags -> taken rows) enable the per-filter "what if it were off"
    table; without them that table is empty."""
    feats = features_to_test(enabled_filters)
    # ---- losing / winning signals
    rows = []
    for t in taken:
        i = int(t["idx"])
        side = 1 if t["side"] == "long" else -1
        rows.append({
            "idx": i, "net": _net(t, fee_pct), "track": t["track"], "side": t["side"],
            "exit_type": t.get("exit_type", ""), "ts": _ts_str(S["ts"][i]),
            "snap": snapshot(S, i, side, atr_min, atr_max, chop_thr),
        })
    n_loss = sum(1 for r in rows if r["net"] < 0)
    losers_tbl = loser_feature_table(rows, cutoff_idx, features=feats)
    worst = sorted(rows, key=lambda r: r["net"])[:examples]

    # ---- missed reversals
    pivots = zigzag(S["high"], S["low"], S["atr"], pivot_atr_mult)
    legs = classify_legs(swing_legs(pivots, S["high"], S["low"]), taken, triggers, enabled_filters)
    for g in legs:
        g["snap"] = snapshot(S, g["pivot_idx"] + SNAP_LAG, g["side"], atr_min, atr_max, chop_thr)
    missed = [g for g in legs if g["status"] != "caught"]
    reasons: Dict[str, int] = {}
    for g in missed:
        key = "no trigger" if g["status"] == "none" else "track busy" if g["status"] == "busy" else (
            "filter: " + "+".join(g["blockers"]))
        reasons[key] = reasons.get(key, 0) + 1
    missed_tbl = missed_feature_table(legs, cutoff_idx, features=feats)
    filter_cf = (filter_counterfactuals(flags, enabled_filters, replay, taken, fee_pct, cutoff_idx)
                 if flags is not None and replay is not None else [])
    biggest = sorted(missed, key=lambda g: -g["leg_pct"])[:examples]

    return {
        "n_bars": int(len(S["close"])),
        "trades": {
            "n": len(rows), "losers": n_loss, "winners": len(rows) - n_loss,
            "avg_net": round(sum(r["net"] for r in rows) / len(rows), 4) if rows else 0.0,
            "total_net": round(sum(r["net"] for r in rows), 4),
        },
        "legs": {
            "pivot_atr_mult": pivot_atr_mult, "total": len(legs),
            "caught": len(legs) - len(missed), "missed": len(missed),
            "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        },
        "filter_cf": filter_cf,
        "exits": exit_autopsy(taken, fee_pct, cutoff_idx, S=S, tp1_frac=tp1_frac, max_hold=max_hold),
        "skipped_features": sorted(set(FEATURES) - set(feats)),
        "loser_features": {**losers_tbl, "candidates": losers_tbl["candidates"][:top]},
        "missed_features": {**missed_tbl, "candidates": missed_tbl["candidates"][:top]},
        "examples": {
            "worst_trades": [{"ts": r["ts"], "track": r["track"], "side": r["side"], "net": round(r["net"], 3),
                              "exit_type": r["exit_type"], "snap": r["snap"]} for r in worst],
            "biggest_missed": [{"ts": _ts_str(S["ts"][g["pivot_idx"]]), "side": "long" if g["side"] > 0 else "short",
                                "leg_pct": g["leg_pct"], "bars": g["bars"], "status": g["status"],
                                "blockers": g["blockers"], "snap": g["snap"]} for g in biggest],
        },
    }
