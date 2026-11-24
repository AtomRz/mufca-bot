"""
missedsim.py - do the reversals the bot never fired on share a STRUCTURE?

autopsy.py counts the swings (ATR ZigZag) that no signal caught and splits them into "no
trigger / a filter / track busy". The biggest bucket is "no trigger": the indicators never
agreed, so the question is no longer about tuning a filter but about whether a NEW kind of
entry could exist. This module answers a narrower, testable question first:

  1. Describe, for every confirmed swing, the market state SNAP_LAG bars after the turn
     (the earliest moment an entry is knowable) using only data up to that bar: how long
     the preceding trend lasted and how far it ran, the volatility percentile and its
     direction, volume (climax / dry-up), a sweep of the range extreme, position against
     the volume-profile value area and POC, MFI / Andean divergence, a break of the last
     minor swing (a CHoCH-like break), a fair-value gap, and the trading session.
  2. Describe the SAME state on a stride sample of ordinary bars (both sides) - the control.
     A state that is common among missed turns but just as common everywhere is no signal.
  3. For every (feature, value): the turn lift against the control, and what a plain
     bracket trade (stop and target in ATRs) entered on the control bars with that state
     would have earned. A state is a candidate only if it holds in BOTH the early and the late
     part of the history; pairs of states are tried only for the strongest singles, to keep
     the number of tests (and the chance hits) down.
  4. cross_consistency(): a state that holds on every pair of one timeframe.

Everything here is a pure function of arrays and lists (no network, no config reads, no
files). signals.run_missedsim() feeds it the trigger-table backtest and the per-bar series
of backtest_history(collect_series=True).

It is a MEASUREMENT. Dozens of states are tested, so a few will look good by chance - the
result reports how many were tested. "Nothing holds" is a valid and expected answer: it means
no new signal type is justified by these features.

Feature values are written from the turn's point of view ("with" = the direction of the
turn), so rises and falls pool together.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np

import autopsy

WARMUP = 200              # bars before the first sampled bar (trailing windows need them)
RANGE_LOOKBACK = 100      # range-extreme / volume-profile window
TREND_RUN_MAX = 400       # cap of the "bars against the FRAMA" scan
STATE_MIN_N = 30          # control bars with a state before it is tested at all
TURN_LIFT_MIN = 1.25      # turn rate in the state vs the base rate (both windows)
TURN_MIN_LEGS = 5         # swings with the state, per window
TRADE_MIN_N = 20          # control trades with the state, per window
MIN_Z = 2.0               # ~95% two-sided, on the turn rate over the whole history
MIN_T = 1.64              # ~95% one-sided, on the average bracket PnL of the state (overlap-adjusted)
CONTROL_TARGET = 3000     # control bars (per side) the stride aims for
BRACKET_SL_ATR = 1.5      # the hypothetical entry: stop and target at the same distance,
BRACKET_TP_ATR = 1.5      # so a state with no edge earns about -fee and wins about half
PAIR_FROM_TOP = 6         # pair combinations are built from this many best single states

# The indicator states of autopsy.snapshot() that are reused here; its filter-decided
# features are NOT skipped (a missed turn with no trigger has no filter involved).
SNAP_FEATURES = ("frama_stretch", "mfi_zone", "andean_age", "ut_state", "hurst", "chop", "regime",
                 "htf", "candle")
NEW_FEATURES = ("trend_run", "prior_move", "atr_pctile", "atr_trend", "vol_now", "vol_climax",
                "range_pos", "va_pos", "poc_dist", "mfi_div", "andean_div", "choch", "fvg", "session")
ALL_FEATURES = NEW_FEATURES + SNAP_FEATURES

FEATURE_HELP = {
    "trend_run": "bars the price has been on the wrong side of the FRAMA for this turn (the trend it reverses)",
    "prior_move": "how far price ran against the turn over the last 30 bars (ATR units)",
    "atr_pctile": "ATR percentile over the last 200 bars",
    "atr_trend": "ATR now vs 10 bars ago (contracting / flat / expanding)",
    "vol_now": "volume of the last 2 bars vs the 20 bars before them",
    "vol_climax": "a volume spike (>2x) in the last 5 bars",
    "range_pos": "last 5 bars vs the 100-bar range extreme on the turn's side: swept it / within 1 ATR / far",
    "va_pos": "close vs the 15-85% volume-weighted price band of the last 100 bars",
    "poc_dist": "distance of the close from the volume POC of the last 100 bars (ATR units)",
    "mfi_div": "MFI diverges from price at the extreme (higher low / lower high against a prior extreme)",
    "andean_div": "Andean oscillator diverges from price the same way",
    "choch": "close broke the last minor swing against the old trend (CHoCH-like)",
    "fvg": "a fair-value gap in the turn's direction in the last 5 bars",
    "session": "weekend / Asia / Europe / US by the bar's UTC open",
}


# ---------------------------------------------------------------------------
# Trailing features (only data up to bar i)
# ---------------------------------------------------------------------------
def _bucket(x: float, edges: Tuple[float, ...], labels: Tuple[str, ...]) -> str:
    for e, lab in zip(edges, labels):
        if x < e:
            return lab
    return labels[-1]


def _div(price_ext_now: float, price_ext_prev: float, ind_now: float, ind_prev: float, side: int) -> str:
    """Bullish (side +1): price made an equal/lower low, the indicator a higher low. Bearish: the mirror."""
    if not all(np.isfinite(v) for v in (price_ext_now, price_ext_prev, ind_now, ind_prev)):
        return "na"
    if side > 0:
        return "div" if price_ext_now <= price_ext_prev and ind_now > ind_prev else "none"
    return "div" if price_ext_now >= price_ext_prev and ind_now < ind_prev else "none"


def structure_features(S: Dict, i: int, side: int) -> Dict[str, str]:
    """New categorical features of the market at bar i for a turn of `side` (+1 up / -1 down),
    from bars <= i only. A value the data cannot give is "na"."""
    n = len(S["close"])
    i = int(min(max(i, 1), n - 1))
    close = np.asarray(S["close"], dtype=float)
    high = np.asarray(S["high"], dtype=float)
    low = np.asarray(S["low"], dtype=float)
    atr = float(S["atr"][i])
    out: Dict[str, str] = {k: "na" for k in NEW_FEATURES}
    if i < 40 or not np.isfinite(atr) or atr <= 0:
        return out

    # trend_run: consecutive bars (ending at i) with the FRAMA direction against the turn
    fdir = S["frama_dir"]
    run = 0
    for k in range(i, max(-1, i - TREND_RUN_MAX), -1):
        d = fdir[k]
        if np.isfinite(d) and int(d) * side < 0:
            run += 1
        else:
            break
    out["trend_run"] = "0" if run == 0 else _bucket(run, (11, 31), ("1-10", "11-30", ">30"))

    # prior_move: ran against the turn over the last 30 bars
    w = slice(max(0, i - 30), i + 1)
    if side > 0:
        move = (float(np.max(high[w])) - float(np.min(low[max(0, i - 5):i + 1]))) / atr
    else:
        move = (float(np.max(high[max(0, i - 5):i + 1])) - float(np.min(low[w]))) / atr
    out["prior_move"] = _bucket(move, (3.0, 6.0), ("<3", "3-6", ">6"))

    # volatility
    atr_w = np.asarray(S["atr"][max(0, i - 199):i + 1], dtype=float)
    atr_w = atr_w[np.isfinite(atr_w)]
    if len(atr_w) >= 50:
        pct = float((atr_w < atr).mean())
        out["atr_pctile"] = "low(<25)" if pct < 0.25 else "high(>75)" if pct > 0.75 else "mid"
    a10 = float(S["atr"][i - 10])
    if np.isfinite(a10) and a10 > 0:
        r = atr / a10
        out["atr_trend"] = "contracting" if r < 0.9 else "expanding" if r > 1.1 else "flat"

    # volume
    vol = S.get("volume")
    if vol is not None:
        v = np.asarray(vol, dtype=float)
        base = v[max(0, i - 21):i - 1]
        base = base[np.isfinite(base)]
        if len(base) >= 10 and float(np.mean(base)) > 0:
            now = float(np.nanmean(v[i - 1:i + 1])) / float(np.mean(base))
            out["vol_now"] = "dry(<0.8)" if now < 0.8 else "high(>1.5)" if now > 1.5 else "normal"
            out["vol_climax"] = "spike" if float(np.nanmax(v[max(0, i - 4):i + 1])) / float(np.mean(base)) > 2.0 else "none"

    # range extreme on the turn's side: swept / near / far
    lo_i = max(0, i - RANGE_LOOKBACK)
    if i - 5 > lo_i + 10:
        if side > 0:
            prior = float(np.min(low[lo_i:i - 4]))
            now_ext = float(np.min(low[i - 4:i + 1]))
            d = (now_ext - prior) / atr
        else:
            prior = float(np.max(high[lo_i:i - 4]))
            now_ext = float(np.max(high[i - 4:i + 1]))
            d = (prior - now_ext) / atr
        out["range_pos"] = "swept" if d <= 0 else "near(<1)" if d < 1 else "far"

    # volume profile proxies: weighted price band and POC of the last 100 bars
    if vol is not None and i - RANGE_LOOKBACK >= 0:
        px = close[i - RANGE_LOOKBACK + 1:i + 1]
        wv = np.asarray(vol, dtype=float)[i - RANGE_LOOKBACK + 1:i + 1]
        ok = np.isfinite(px) & np.isfinite(wv) & (wv > 0)
        if ok.sum() >= 30:
            px, wv = px[ok], wv[ok]
            order = np.argsort(px)
            cum = np.cumsum(wv[order]) / wv.sum()
            val = float(px[order][np.searchsorted(cum, 0.15)])
            vah = float(px[order][min(len(px) - 1, np.searchsorted(cum, 0.85))])
            c = float(close[i])
            # from the turn's side: below the band on a rise (or above on a fall) = stretched away from value
            stretched = (c < val) if side > 0 else (c > vah)
            beyond_with = (c > vah) if side > 0 else (c < val)
            out["va_pos"] = "stretched_against" if stretched else "beyond_with" if beyond_with else "inside"
            hist, edges = np.histogram(px, bins=24, weights=wv)
            poc = float((edges[int(np.argmax(hist))] + edges[int(np.argmax(hist)) + 1]) / 2)
            out["poc_dist"] = _bucket(abs(c - poc) / atr, (1.0, 3.0), ("<1", "1-3", ">3"))

    # divergences against the previous extreme (6..60 bars back)
    a, b = max(0, i - 60), i - 5
    if b - a >= 8:
        if side > 0:
            j = a + int(np.argmin(low[a:b])); k = max(0, i - 4) + int(np.argmin(low[max(0, i - 4):i + 1]))
            pj, pk = float(low[j]), float(low[k])
        else:
            j = a + int(np.argmax(high[a:b])); k = max(0, i - 4) + int(np.argmax(high[max(0, i - 4):i + 1]))
            pj, pk = float(high[j]), float(high[k])
        out["mfi_div"] = _div(pk, pj, float(S["mfi"][k]), float(S["mfi"][j]), side)
        out["andean_div"] = _div(pk, pj, float(S["and_osc"][k]), float(S["and_osc"][j]), side)

    # CHoCH-like: close beyond the extreme of the bars just before the last 3
    if i >= 14:
        if side > 0:
            out["choch"] = "broke" if close[i] > float(np.max(high[i - 13:i - 2])) else "intact"
        else:
            out["choch"] = "broke" if close[i] < float(np.min(low[i - 13:i - 2])) else "intact"

    # fair-value gap in the turn's direction among the last 5 bars (3-bar pattern k, k+1, k+2)
    gap = False
    for k in range(max(0, i - 4), i - 1):
        if side > 0 and low[k + 2] > high[k]:
            gap = True
        if side < 0 and high[k + 2] < low[k]:
            gap = True
    out["fvg"] = "gap" if gap else "none"

    # session by the UTC open of the bar
    try:
        ts = int(S["ts"][i]) // 1000
        wd = (ts // 86400 + 3) % 7          # 1970-01-01 was a Thursday -> Monday = 0
        hr = (ts // 3600) % 24
        out["session"] = "weekend" if wd >= 5 else "asia" if hr < 8 else "europe" if hr < 16 else "us"
    except (TypeError, ValueError, OverflowError):
        pass
    return out


def full_features(S: Dict, i: int, side: int, atr_min: float, atr_max: float, chop_thr: float) -> Dict[str, str]:
    snap = autopsy.snapshot(S, i, side, atr_min, atr_max, chop_thr)
    out = structure_features(S, i, side)
    out.update({k: snap.get(k, "na") for k in SNAP_FEATURES})
    return out


# ---------------------------------------------------------------------------
# The hypothetical bracket trade (control bars)
# ---------------------------------------------------------------------------
def bracket_outcome(S: Dict, i: int, side: int, max_hold: int,
                    sl_atr: float = BRACKET_SL_ATR, tp_atr: float = BRACKET_TP_ATR) -> Optional[float]:
    """PnL in % of entry of a trade opened at the close of bar i: stop sl_atr x ATR and target
    tp_atr x ATR away, closed at the close after max_hold bars. A bar touching both counts as
    the stop (the conservative reading). None when the bars are not there."""
    n = len(S["close"])
    entry, atr = float(S["close"][i]), float(S["atr"][i])
    if entry <= 0 or not np.isfinite(atr) or atr <= 0 or i + 1 >= n:
        return None
    stop = sl_atr * atr / entry * 100.0
    tgt = tp_atr * atr / entry * 100.0
    last = min(i + max_hold, n - 1)
    for j in range(i + 1, last + 1):
        h, l = float(S["high"][j]), float(S["low"][j])
        fav = ((h - entry) if side > 0 else (entry - l)) / entry * 100.0
        adv = ((entry - l) if side > 0 else (h - entry)) / entry * 100.0
        if adv >= stop:
            return -stop
        if fav >= tgt:
            return tgt
    c = float(S["close"][last])
    return ((c - entry) if side > 0 else (entry - c)) / entry * 100.0


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def _z(x: float, n_pop: float, p0: float) -> float:
    var = n_pop * p0 * (1.0 - p0)
    return (x - n_pop * p0) / var ** 0.5 if var > 0 else 0.0


def _t_stat(pnl: List[float], stride: int, max_hold: int) -> float:
    """t-statistic of the mean bracket PnL. Control trades overlap in time (a trade lasts up to
    max_hold bars, the control bars are `stride` apart), so the effective sample is n x stride /
    max_hold, not n."""
    n = len(pnl)
    if n < 3:
        return 0.0
    a = np.asarray(pnl, dtype=float)
    sd = float(a.std(ddof=1))
    n_eff = max(1.0, n * stride / max(1, max_hold))
    return float(a.mean() / (sd / n_eff ** 0.5)) if sd > 0 else 0.0


def _match(feats: Dict[str, str], state: Tuple[Tuple[str, str], ...]) -> bool:
    return all(feats.get(f) == v for f, v in state)


def _state_label(state: Tuple[Tuple[str, str], ...]) -> Tuple[str, str]:
    return " & ".join(f for f, _ in state), " & ".join(v for _, v in state)


def evaluate_state(state, legs: List[Dict], ctrl: List[Dict], stride: int, base: Dict, cutoff_idx: int,
                   fee_pct: float, max_hold: int = 20) -> Optional[Dict]:
    """One (feature=value[, feature=value]) state against the control sample.
    legs: classified swings with "feats" and "status"; ctrl: {"idx","feats","pnl"} control bars."""
    c_all = [c for c in ctrl if _match(c["feats"], state)]
    if len(c_all) < STATE_MIN_N:
        return None
    l_all = [g for g in legs if _match(g["feats"], state)]

    def part(rows_l, rows_c, key):
        n_pop = max(1.0, len(rows_c) * stride)
        rate = len(rows_l) / n_pop
        b = base[key]["turn_rate"]
        pnl = [c["pnl"] - fee_pct for c in rows_c]
        return {"legs": len(rows_l), "ctrl": len(rows_c), "turn_rate": rate,
                "lift": rate / b if b > 0 else 0.0,
                "z": _z(len(rows_l), n_pop, b) if b > 0 else 0.0,
                "avg_net": sum(pnl) / len(pnl) if pnl else 0.0,
                "win": sum(1 for p in pnl if p > 0) / len(pnl) if pnl else 0.0,
                "t": _t_stat(pnl, stride, max_hold)}

    tot = part(l_all, c_all, "all")
    ear = part([g for g in l_all if g["pivot_idx"] < cutoff_idx], [c for c in c_all if c["idx"] < cutoff_idx], "early")
    lat = part([g for g in l_all if g["pivot_idx"] >= cutoff_idx], [c for c in c_all if c["idx"] >= cutoff_idx], "late")
    none_legs = [g for g in l_all if g["status"] == "none"]

    holds_turn = bool(ear["legs"] >= TURN_MIN_LEGS and lat["legs"] >= TURN_MIN_LEGS
                      and ear["lift"] >= TURN_LIFT_MIN and lat["lift"] >= TURN_LIFT_MIN and tot["z"] >= MIN_Z)
    holds_trade = bool(ear["ctrl"] >= TRADE_MIN_N and lat["ctrl"] >= TRADE_MIN_N
                       and ear["avg_net"] > 0 and lat["avg_net"] > 0
                       and tot["avg_net"] > base["all"]["avg_net"] and tot["t"] >= MIN_T)
    grade = "both" if holds_turn and holds_trade else "turn" if holds_turn else "trade" if holds_trade else "none"
    feature, value = _state_label(state)
    rnd = lambda d: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}  # noqa: E731
    return {
        "feature": feature, "value": value, "n_legs": len(l_all), "n_none": len(none_legs),
        "none_share": round(len(none_legs) / len(l_all), 4) if l_all else 0.0,
        "none_leg_pct": round(sum(g["leg_pct"] for g in none_legs), 2),
        "all": rnd(tot), "early": rnd(ear), "late": rnd(lat),
        "holds_turn": holds_turn, "holds_trade": holds_trade, "grade": grade,
        "holds": grade == "both",
    }


def _baseline(legs: List[Dict], ctrl: List[Dict], stride: int, cutoff_idx: int, fee_pct: float) -> Dict:
    def one(rows_l, rows_c):
        n_pop = max(1.0, len(rows_c) * stride)
        pnl = [c["pnl"] - fee_pct for c in rows_c]
        return {"turn_rate": len(rows_l) / n_pop, "ctrl": len(rows_c), "legs": len(rows_l),
                "avg_net": sum(pnl) / len(pnl) if pnl else 0.0,
                "win": sum(1 for p in pnl if p > 0) / len(pnl) if pnl else 0.0}
    return {
        "all": one(legs, ctrl),
        "early": one([g for g in legs if g["pivot_idx"] < cutoff_idx], [c for c in ctrl if c["idx"] < cutoff_idx]),
        "late": one([g for g in legs if g["pivot_idx"] >= cutoff_idx], [c for c in ctrl if c["idx"] >= cutoff_idx]),
    }


def rank_states(legs: List[Dict], ctrl: List[Dict], stride: int, cutoff_idx: int, fee_pct: float,
                features: Tuple[str, ...] = ALL_FEATURES, pair_from_top: int = PAIR_FROM_TOP,
                max_hold: int = 20) -> Dict:
    """Every single (feature, value) state seen often enough on the control bars, then the pairs
    among the `pair_from_top` strongest singles (by turn lift over the whole history)."""
    base = _baseline(legs, ctrl, stride, cutoff_idx, fee_pct)
    if not legs or not ctrl or base["all"]["turn_rate"] <= 0:
        return {"baseline": base, "n_tested": 0, "candidates": []}
    singles: List[Dict] = []
    for f in features:
        values = sorted({c["feats"].get(f, "na") for c in ctrl} - {"na"})
        for v in values:
            r = evaluate_state(((f, v),), legs, ctrl, stride, base, cutoff_idx, fee_pct, max_hold)
            if r is not None:
                singles.append(r)
    top = sorted((s for s in singles if s["all"]["lift"] > 1.0), key=lambda s: -s["all"]["lift"])[:pair_from_top]
    pairs: List[Dict] = []
    seen = set()
    for a in range(len(top)):
        for b in range(a + 1, len(top)):
            fa, fb = top[a]["feature"], top[b]["feature"]
            if fa == fb:
                continue
            state = ((fa, top[a]["value"]), (fb, top[b]["value"]))
            if state in seen:
                continue
            seen.add(state)
            r = evaluate_state(state, legs, ctrl, stride, base, cutoff_idx, fee_pct, max_hold)
            if r is not None:
                pairs.append(r)
    allc = singles + pairs
    order = {"both": 0, "turn": 1, "trade": 2, "none": 3}
    allc.sort(key=lambda c: (order[c["grade"]], -c["all"]["lift"], -c["n_none"]))
    return {"baseline": base, "n_tested": len(allc), "n_singles": len(singles), "n_pairs": len(pairs),
            "candidates": allc}


# ---------------------------------------------------------------------------
# One pair / timeframe
# ---------------------------------------------------------------------------
def analyze_pair(S: Dict, triggers: List[Dict], taken: List[Dict], enabled_filters: List[str],
                 fee_pct: float, cutoff_idx: int, atr_min: float, atr_max: float, chop_thr: float,
                 pivot_atr_mult: float = 3.0, max_hold: int = 20, top: int = 12,
                 control_target: int = CONTROL_TARGET) -> Dict:
    """The full missed-reversal structure study of one pair/timeframe.
    `triggers` / `taken` are the trigger-table rows of autopsy.analyze_pair()."""
    n = len(S["close"])
    pivots = autopsy.zigzag(S["high"], S["low"], S["atr"], pivot_atr_mult)
    legs = autopsy.classify_legs(autopsy.swing_legs(pivots, S["high"], S["low"]), taken, triggers, enabled_filters)
    last_ok = n - max_hold - 2
    first_ok = WARMUP
    # only swings whose snapshot bar lies in the sampled range count, so the base rate matches
    legs = [g for g in legs if first_ok <= g["pivot_idx"] + autopsy.SNAP_LAG <= last_ok]
    for g in legs:
        g["feats"] = full_features(S, g["pivot_idx"] + autopsy.SNAP_LAG, g["side"], atr_min, atr_max, chop_thr)
    counts = {k: sum(1 for g in legs if g["status"] == k) for k in ("caught", "filtered", "busy", "none")}

    # control: a stride sample of ordinary bars, both sides, with the bracket trade's outcome
    eligible = max(0, last_ok - first_ok + 1)
    stride = max(1, eligible // max(1, control_target))
    ctrl: List[Dict] = []
    for i in range(first_ok, last_ok + 1, stride):
        for side in (1, -1):
            pnl = bracket_outcome(S, i, side, max_hold)
            if pnl is None:
                continue
            ctrl.append({"idx": i, "side": side, "pnl": pnl,
                         "feats": full_features(S, i, side, atr_min, atr_max, chop_thr)})
    # one control row stands for `stride` bars of ONE side; a leg is one (bar, side) event
    ranked = rank_states(legs, ctrl, stride, cutoff_idx, fee_pct, max_hold=max_hold)
    cands = ranked["candidates"]
    grades = {g: sum(1 for c in cands if c["grade"] == g) for g in ("both", "turn", "trade")}
    return {
        "n_bars": int(n), "pivot_atr_mult": pivot_atr_mult, "max_hold": int(max_hold), "stride": int(stride),
        "bracket": {"sl_atr": BRACKET_SL_ATR, "tp_atr": BRACKET_TP_ATR},
        "legs": {"total": len(legs), **counts},
        "baseline": {k: {kk: round(vv, 5) if isinstance(vv, float) else vv for kk, vv in v.items()}
                     for k, v in ranked["baseline"].items()},
        "n_tested": ranked["n_tested"], "n_singles": ranked.get("n_singles", 0), "n_pairs": ranked.get("n_pairs", 0),
        "grades": grades,
        "verdict": "candidates" if grades["both"] else "nothing_holds",
        "candidates": cands[:max(top, grades["both"])],
        "n_candidates_total": len(cands),
    }


def cross_consistency(results: Dict[str, Dict[str, Dict]]) -> List[Dict]:
    """States whose grade is "both" (turn structure AND bracket expectancy, early and late) on
    EVERY analysed pair of one timeframe - needs two or more pairs. {ticker: {tf: analyze_pair()}}."""
    by_tf: Dict[str, Dict[str, Dict]] = {}
    for t, per_tf in results.items():
        for tf, r in per_tf.items():
            by_tf.setdefault(tf, {})[t] = r
    out: List[Dict] = []
    for tf, per_t in by_tf.items():
        if len(per_t) < 2:
            continue
        sets = []
        for r in per_t.values():
            sets.append({(c["feature"], c["value"]) for c in r["candidates"] if c["grade"] == "both"})
        common = set.intersection(*sets) if sets else set()
        for feature, value in sorted(common):
            out.append({"tf": tf, "feature": feature, "value": value, "pairs": sorted(per_t)})
    return out
