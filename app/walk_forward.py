"""
Rolling walk-forward check of the filter auto-tuner, plus an audit of the filter sets
that are already pinned (`!walkfwd`, Settings -> "Walk-forward check", POST /api/walkfwd).

Why this exists: `!autofilter` already picks a filter set on the oldest 60% of history
and confirms it on the newest 40%. That is ONE out-of-sample window, so a lucky window
can still wave a set through - and nothing looks at a pinned set again once it is live.
This module answers two different questions with the same trigger table the tuner uses
(one dry-run backtest per pair/timeframe, every raw trigger recorded with the filters
that reject it, trades rebuilt by signals.replay_filter_set):

1. WALK-FORWARD (is the tuning *policy* any good?)
   The history is cut into expanding-train / rolling-test folds. For every fold the
   tuner's own rule is applied to the training part only (best of the 256 on/off sets by
   net PnL, adopted only if it beats the current set by more than the noise), and the
   set it would have chosen is then traded on the NEXT block of bars it has never seen.
   Summed over the folds this is the out-of-sample result of "re-tune regularly", set
   against three fixed alternatives: keep the current set, everything on, everything off.
   It also reports how stable the choice is from fold to fold (a set that changes every
   fold is fitting noise).

2. PINNED-SET AUDIT (is a set that is already live still working?)
   For every pinned pair/timeframe: the pinned set vs the set it replaced (or the global
   toggles) on the bars AFTER it was pinned - the only data the pinning decision never
   saw - and on the newest slice of history.

Everything here is a dry run: nothing is written, no setting is touched.
"""

import asyncio
import itertools
import logging
import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import ccxt

import config as _cfg
from sim_pool import SimPool
import signals as _sig

logger = logging.getLogger(__name__)

FILTER_KEYS = _sig.FILTER_KEYS


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
def _combo(flags: Dict[str, bool]) -> Tuple[bool, ...]:
    return tuple(bool(flags.get(k)) for k in FILTER_KEYS)


def _flags(combo: Tuple[bool, ...]) -> Dict[str, bool]:
    return dict(zip(FILTER_KEYS, combo))


def _window(trades: List[Dict], lo: Optional[int], hi: Optional[int]) -> List[Dict]:
    """Trades ENTERED in [lo, hi) (None = unbounded)."""
    return [t for t in trades
            if (lo is None or t["idx"] >= lo) and (hi is None or t["idx"] < hi)]


def _since_ts(trades: List[Dict], ts_ms: int) -> List[Dict]:
    return [t for t in trades if t.get("ts") is not None and int(t["ts"]) >= ts_ms]


def make_folds(first_idx: int, last_idx: int, folds: int, initial_fraction: float) -> List[Tuple[int, int]]:
    """[(test_start, test_end)] with the training window always [first_idx, test_start).

    The first `initial_fraction` of the usable bars is only ever training data; the rest is
    cut into `folds` equal test blocks, each one tested after training on everything before
    it (expanding window). The last block runs through last_idx."""
    span = max(last_idx - first_idx, 1)
    out: List[Tuple[int, int]] = []
    for k in range(folds):
        a = first_idx + int(span * (initial_fraction + (1.0 - initial_fraction) * k / folds))
        b = first_idx + int(span * (initial_fraction + (1.0 - initial_fraction) * (k + 1) / folds))
        if k == folds - 1:
            b = last_idx + 1
        out.append((a, b))
    return out


def _gain(base: List[Dict], other: List[Dict], fee: float) -> Dict[str, float]:
    g, noise = _sig._diff_gain_noise(base, other, fee)
    return {"gain": round(g, 4), "noise": round(noise, 4)}


def _is_better(gain: float, noise: float, k: float) -> bool:
    return gain > 0 and gain > k * noise


# ---------------------------------------------------------------------
# 1. walk-forward of the filter-set tuner
# ---------------------------------------------------------------------
def walk_forward_filter_sets(triggers: List[Dict], tf: str, current: Dict[str, bool],
                             first_idx: int, last_idx: int, fee_pct: float,
                             folds: int, initial_fraction: float,
                             min_train: int, min_test: int,
                             min_gain_se: float, test_gain_se: float,
                             bar_ts: Optional[List[int]] = None) -> Dict:
    """Rolling walk-forward of choose_filter_set()'s rule for ONE pair/timeframe.

    For every fold the best of the 2^len(FILTER_KEYS) sets by net PnL on the training part
    (>= min_train trades, ties -> more filters on) replaces `current` only if its training gain
    beats min_gain_se x noise - the same acceptance rule the live tuner uses - and the
    resulting set is traded on the fold's untouched test block. See the module docstring.

    Returns {"folds": [...], "aggregate": {...}, "stability": {...}, "verdict", "reason"}.
    """
    cur_combo = _combo(current)
    sets: Dict[Tuple[bool, ...], List[Dict]] = {}
    for combo in itertools.product((False, True), repeat=len(FILTER_KEYS)):
        sets[combo] = _sig.replay_filter_set(triggers, _flags(combo), tf)
    cur_trades = sets[cur_combo]
    all_on, all_off = sets[tuple(True for _ in FILTER_KEYS)], sets[tuple(False for _ in FILTER_KEYS)]

    out_folds: List[Dict] = []
    oos = {"policy": [], "current": [], "all_on": [], "all_off": []}
    picked_counts: Dict[Tuple[bool, ...], int] = {}
    picked_on_share = {k: 0 for k in FILTER_KEYS}
    n_scored = 0
    is_gain_sum = 0.0
    oos_gain_switched = 0.0
    switched_gains: List[float] = []

    for (a, b) in make_folds(first_idx, last_idx, folds, initial_fraction):
        cur_tr = _window(cur_trades, None, a)
        best: Optional[Tuple[float, int, Tuple[bool, ...], List[Dict]]] = None
        for combo, trades in sets.items():
            tr = _window(trades, None, a)
            if len(tr) < min_train:
                continue
            total = sum(_sig._net_pnls(tr, fee_pct))
            key = (round(total, 9), sum(combo))
            if best is None or key > (round(best[0], 9), best[1]):
                best = (total, sum(combo), combo, tr)

        fold: Dict[str, Any] = {
            "test_from_idx": a, "test_to_idx": b,
            "test_from_ts": int(bar_ts[a]) if bar_ts and 0 <= a < len(bar_ts) else None,
            "test_to_ts": int(bar_ts[min(b, len(bar_ts)) - 1]) if bar_ts and b > 0 else None,
            "picked": None, "chosen": dict(current), "switched": False, "reason": "",
        }
        policy_combo = cur_combo
        if best is None:
            fold["reason"] = f"no filter set reaches {min_train} trades in the training window"
        else:
            _tot, _n_on, b_combo, b_tr = best
            fold["picked"] = _flags(b_combo)
            picked_counts[b_combo] = picked_counts.get(b_combo, 0) + 1
            n_scored += 1
            for k, v in zip(FILTER_KEYS, b_combo):
                picked_on_share[k] += 1 if v else 0
            if b_combo == cur_combo:
                fold["reason"] = "the current set was already the best on the training window"
            else:
                g = _gain(cur_tr, b_tr, fee_pct)
                fold["train_gain"] = g
                if _is_better(g["gain"], g["noise"], min_gain_se):
                    policy_combo = b_combo
                    fold["chosen"] = _flags(b_combo)
                    fold["switched"] = True
                    fold["reason"] = f"training gain {g['gain']:+.2f}% beats the noise ({g['noise']:.2f}%)"
                    is_gain_sum += g["gain"]
                else:
                    fold["reason"] = (f"training gain {g['gain']:+.2f}% is within noise "
                                      f"(needs > {min_gain_se * g['noise']:.2f}%) - kept the current set")

        pol_te = _window(sets[policy_combo], a, b)
        cur_te = _window(cur_trades, a, b)
        on_te, off_te = _window(all_on, a, b), _window(all_off, a, b)
        oos["policy"] += pol_te
        oos["current"] += cur_te
        oos["all_on"] += on_te
        oos["all_off"] += off_te
        fold["test"] = {
            "policy": _sig._window_stats(pol_te, fee_pct), "current": _sig._window_stats(cur_te, fee_pct),
            "all_on": _sig._window_stats(on_te, fee_pct), "all_off": _sig._window_stats(off_te, fee_pct),
        }
        fold["thin"] = len(pol_te) < min_test
        if fold["switched"]:
            tg = _gain(cur_te, pol_te, fee_pct)
            fold["test_gain"] = tg
            switched_gains.append(tg["gain"])
            oos_gain_switched += tg["gain"]
        out_folds.append(fold)

    n_switched = sum(1 for f in out_folds if f["switched"])
    agg_gain = _gain(oos["current"], oos["policy"], fee_pct)
    aggregate = {
        "policy": _sig._window_stats(oos["policy"], fee_pct),
        "current": _sig._window_stats(oos["current"], fee_pct),
        "all_on": _sig._window_stats(oos["all_on"], fee_pct),
        "all_off": _sig._window_stats(oos["all_off"], fee_pct),
        "policy_vs_current": agg_gain,
        "folds": len(out_folds), "folds_switched": n_switched,
        "folds_switched_positive": sum(1 for g in switched_gains if g > 0),
        "in_sample_gain": round(is_gain_sum, 4),            # what the tuner saw when it switched
        "out_of_sample_gain": round(oos_gain_switched, 4),  # what it actually got on the next block
    }

    modal_combo, modal_n = (max(picked_counts.items(), key=lambda kv: kv[1]) if picked_counts else (None, 0))
    stability = {
        "folds_scored": n_scored,
        "distinct_picks": len(picked_counts),
        "modal_pick": _flags(modal_combo) if modal_combo else None,
        "modal_share": round(modal_n / n_scored, 2) if n_scored else None,
        "filter_on_share": {k: round(v / n_scored, 2) for k, v in picked_on_share.items()} if n_scored else {},
    }

    verdict, reason = _walk_forward_verdict(aggregate, stability, test_gain_se, min_test)
    return {"folds": out_folds, "aggregate": aggregate, "stability": stability,
            "verdict": verdict, "reason": reason}


def _walk_forward_verdict(agg: Dict, stab: Dict, test_gain_se: float, min_test: int) -> Tuple[str, str]:
    n_sw, n_f = agg["folds_switched"], agg["folds"]
    if stab["folds_scored"] == 0:
        return "no_data", "no fold had enough trades in its training window to pick a set"
    if n_sw == 0:
        return "stable", (f"re-tuning would not have changed the current set in any of the {n_f} folds - "
                          f"nothing beat it by more than the noise on data it was allowed to see")
    g = agg["policy_vs_current"]
    n_oos = agg["policy"]["n"]
    pos = agg["folds_switched_positive"]
    if n_oos < min_test:
        return "inconclusive", f"only {n_oos} out-of-sample trades in total (needs {min_test})"
    if _is_better(g["gain"], g["noise"], test_gain_se) and pos * 2 > n_sw:
        min_sw = _cfg.WALKFWD_MIN_SWITCHED_FOLDS
        if n_sw < min_sw:
            # one lucky block is not evidence that re-tuning keeps its edge
            return "inconclusive", (f"re-tuning gained {g['gain']:+.2f}% out-of-sample, but it switched in only "
                                    f"{n_sw} of {n_f} fold(s) - needs {min_sw} to be called robust")
        return "robust", (f"re-tuning gained {g['gain']:+.2f}% out-of-sample over keeping the current set, "
                          f"better in {pos} of {n_sw} folds where it switched")
    if g["gain"] <= 0:
        return "not_robust", (f"re-tuning LOST {g['gain']:+.2f}% out-of-sample vs keeping the current set "
                              f"(it switched in {n_sw} of {n_f} folds, better in only {pos}) - the in-sample "
                              f"gains do not carry over")
    return "inconclusive", (f"re-tuning gained {g['gain']:+.2f}% out-of-sample but that is within noise "
                            f"({g['noise']:.2f}%) or rests on {pos} of {n_sw} folds")


# ---------------------------------------------------------------------
# 2. audit of already-pinned filter sets
# ---------------------------------------------------------------------
def audit_pinned_set(triggers: List[Dict], tf: str, pinned: Dict[str, bool], reference: Dict[str, bool],
                     pinned_at: Optional[int], first_idx: int, last_idx: int, fee_pct: float,
                     recent_fraction: float, min_trades: int, test_gain_se: float) -> Dict:
    """The pinned set vs `reference` (the set it replaced, or the global toggles) on
      - "since_pin": trades entered after it was pinned (`pinned_at`, unix seconds) - data the
        pinning decision could not have seen;
      - "recent": the newest `recent_fraction` of the usable bars (a pin made from the whole
        history has seen these too, so this window is a sanity check, not a clean test).
    Status per window: holding / worse / no_difference / too_early."""
    pin_trades = _sig.replay_filter_set(triggers, pinned, tf)
    ref_trades = _sig.replay_filter_set(triggers, reference, tf)
    recent_from = last_idx - int((last_idx - first_idx) * recent_fraction)

    def window(name: str, p: List[Dict], r: List[Dict]) -> Dict:
        g = _gain(r, p, fee_pct)
        if len(p) < min_trades:
            status = "too_early"
        elif _is_better(g["gain"], g["noise"], test_gain_se):
            status = "holding"
        elif g["gain"] < 0 and -g["gain"] > test_gain_se * g["noise"]:
            status = "worse"
        else:
            status = "no_difference"
        return {"window": name, "pinned": _sig._window_stats(p, fee_pct),
                "reference": _sig._window_stats(r, fee_pct), "gain": g, "status": status}

    out: Dict[str, Any] = {"pinned": dict(pinned), "reference": dict(reference), "pinned_at": pinned_at}
    if pinned_at:
        out["since_pin"] = window("since_pin", _since_ts(pin_trades, int(pinned_at) * 1000),
                                  _since_ts(ref_trades, int(pinned_at) * 1000))
    else:
        out["since_pin"] = None
    out["recent"] = window("recent", _window(pin_trades, recent_from, None), _window(ref_trades, recent_from, None))
    return out


# ---------------------------------------------------------------------
# runner (background job, same contract as the other simulations)
# ---------------------------------------------------------------------
async def run_walk_forward(exchange: ccxt.Exchange, tickers: List[str], tfs: List[str], num_bars: int = 6000,
                           progress: Optional[Callable[[int, int, str], None]] = None,
                           workers: Optional[int] = None, folds: Optional[int] = None,
                           fee_pct: Optional[float] = None) -> Dict:
    """One trigger-table backtest per pair/timeframe (shared with the tuner's method), then
    walk_forward_filter_sets() for every pair/timeframe and audit_pinned_set() for the ones
    that are pinned. Dry run: nothing is written, no setting changes.

    Returns {"fee_pct","folds","initial_fraction","bar_coverage","skipped",
             "results": {ticker: {tf: {"walk_forward": {...}, "audit": {...} | None,
                                       "current": flags, "current_pinned": bool}}}}"""
    fee = _cfg.AUTOTUNE_FEE_PCT if fee_pct is None else fee_pct
    n_folds = max(2, min(12, int(folds if folds else _cfg.WALKFWD_FOLDS)))
    init_frac = _cfg.WALKFWD_INITIAL_TRAIN_FRACTION
    counters = {"done": 0, "total": len(tickers) * len(tfs)}
    bar_coverage: List[Dict] = []
    skipped: List[Dict] = []
    submitted: List[tuple] = []
    off = {k: False for k in FILTER_KEYS}

    def _on_done(label: str):
        def cb(_fut):
            counters["done"] += 1
            _sig._report_progress(progress, counters["done"], counters["total"], f"finished {label}")
        return cb

    _sig._report_progress(progress, 0, counters["total"], "starting")
    async with SimPool(workers) as pool:
        for t in tickers:
            for f in tfs:
                _sig._report_progress(progress, counters["done"], counters["total"], f"fetching {t} {f}")
                try:
                    inputs = await asyncio.to_thread(_sig._prefetch_backtest_inputs, exchange, t, f, num_bars, False)
                except Exception as e:
                    logger.error(f"run_walk_forward: fetch error for {t} {f}: {e}")
                    skipped.append({"ticker": t, "tf": f, "reason": f"data fetch failed: {e}"})
                    counters["total"] -= 1
                    continue
                bar_coverage.append({"ticker": t, "tf": f, "requested": num_bars, "got": inputs["bars_fetched"]})
                if inputs["df"] is None:
                    skipped.append({"ticker": t, "tf": f,
                                    "reason": f"not enough history (got {inputs['bars_fetched']} bars)"})
                    counters["total"] -= 1
                    continue
                fut = pool.submit({
                    "ticker": t, "tf": f, "num_bars": num_bars, "tracks": _cfg.TRACKS,
                    "tp1_sl_fraction": None, "dry_run": True, "df": inputs["df"],
                    "compute_confidence": False, "benchmark_df": None, "htf_df": inputs["htf_df"],
                    "filter_overrides": off, "collect_stats": True, "trigger_table": True,
                })
                fut.add_done_callback(_on_done(f"{t} {f}"))
                submitted.append((t, f, inputs["df"], fut))
        outcomes = await asyncio.gather(*(item[3] for item in submitted), return_exceptions=True)

    overrides = _cfg.get_filter_overrides()
    results: Dict[str, Dict] = {}
    for (t, f, df, _fut), outcome in zip(submitted, outcomes):
        if isinstance(outcome, BaseException):
            logger.error(f"run_walk_forward: backtest error for {t} {f}: {outcome!r}")
            skipped.append({"ticker": t, "tf": f, "reason": f"backtest failed: {outcome!r}"})
            continue
        _count, history, _bars = outcome
        triggers = (history.pop("__stats__", None) or {}).get("trades", [])
        n_bars = len(df)
        first_idx, last_idx = 50, n_bars - _cfg.MAX_HOLD_BARS
        if last_idx - first_idx < 200:
            skipped.append({"ticker": t, "tf": f, "reason": f"not enough history (got {n_bars} bars)"})
            continue
        current = _sig.current_filter_flags(t, f)
        pinned = overrides["active"].get(t, {}).get(f)
        meta = overrides["meta"].get(t, {}).get(f) or {}
        bar_ts = [int(x) for x in df["timestamp"].tolist()]
        try:
            wf = walk_forward_filter_sets(
                triggers, f, current, first_idx, last_idx, fee, n_folds, init_frac,
                _cfg.AUTOTUNE_MIN_TRAIN_TRADES, _cfg.AUTOTUNE_MIN_TEST_TRADES,
                _cfg.AUTOTUNE_MIN_GAIN_SE, _cfg.AUTOTUNE_MIN_TEST_GAIN_SE, bar_ts=bar_ts)
            audit = None
            if pinned is not None:
                prev = overrides["previous"].get(t, {}).get(f)
                reference = prev if prev is not None else _cfg.global_filter_flags()
                audit = audit_pinned_set(
                    triggers, f, pinned, reference, meta.get("at") or overrides.get("applied_at"),
                    first_idx, last_idx, fee, _cfg.WALKFWD_RECENT_FRACTION,
                    _cfg.AUTOTUNE_MIN_TEST_TRADES, _cfg.AUTOTUNE_MIN_TEST_GAIN_SE)
                audit["reference_kind"] = "previous pin" if prev is not None else "global toggles"
                audit["manual"] = bool(meta.get("manual"))
        except Exception as e:
            logger.error(f"run_walk_forward: analysis failed for {t} {f}: {e}", exc_info=True)
            skipped.append({"ticker": t, "tf": f, "reason": f"analysis failed: {e}"})
            continue
        results.setdefault(t, {})[f] = {
            "walk_forward": wf, "audit": audit, "current": current,
            "current_pinned": pinned is not None, "bars": n_bars,
            "first_ts": bar_ts[0] if bar_ts else None, "last_ts": bar_ts[-1] if bar_ts else None,
            "generated_at": int(time.time()),
        }
    return {"fee_pct": fee, "folds": n_folds, "initial_fraction": init_frac,
            "bar_coverage": bar_coverage, "skipped": skipped, "results": results}
