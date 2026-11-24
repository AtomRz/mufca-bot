"""
autotune_checks.py - the checks a filter-set proposal of the auto-tuner has to pass before it is applied.

`!autofilter` / "Auto-tune filters" picks the best of the 256 on/off sets on the older 60% of the
history and confirms it on the newest 40% (signals.choose_filter_set). Two other tools used to be
run by hand afterwards: the per-filter "what if it were switched" table of `!autopsy` and the rolling
`!walkfwd`. verify_choice() runs them on the SAME trigger table, so one run decides:

  1. TUNER      the proposal beat the current set on the train window beyond noise and held up on
                the test window (choose_filter_set, unchanged - this module only runs on proposals).
  2. FLIPS      every filter the proposal changes is flipped ALONE on the current set and replayed
                (same triggers, one position per track): the flip must make more money in BOTH the
                early and the late window. A change that fails this is dropped; the proposal is
                trimmed to the flips that passed, and the trimmed set must itself beat the current
                one in both windows.
  3. WALK-FORWARD  the rolling walk-forward of the tuning policy for this pair/timeframe
                (walk_forward.walk_forward_filter_sets): is re-tuning this pair/timeframe known to
                carry out of sample? strict = verdict "robust" is required, lenient = anything but
                "not_robust" passes.

Only when all three pass is `decision` "apply" and `final` the set to pin (the current set plus the
flips that passed). Otherwise `decision` is "keep" and `blocked_by` names the first failed gate.
The proposal itself is always reported (`proposed`) so a blocked change can still be looked at.

Pure functions of the trigger table and numbers: no network, no files, no config writes.
"""

from typing import Dict, List, Optional

import config as _cfg
import signals as _sig

FILTER_KEYS = _sig.FILTER_KEYS

WF_STRICT = "robust"
WF_LENIENT = "not_worse"


def _net(rows: List[Dict], fee_pct: float) -> float:
    return float(sum(_sig._net_pnls(rows, fee_pct)))


def _delta(alt: List[Dict], base: List[Dict], fee_pct: float, cutoff_idx: int) -> Dict[str, float]:
    """Net-PnL change of `alt` against `base`, whole history and per window (early = entered
    before cutoff_idx), and the number of extra trades."""
    split = lambda rows: ([t for t in rows if t["idx"] < cutoff_idx],  # noqa: E731
                          [t for t in rows if t["idx"] >= cutoff_idx])
    a_e, a_l = split(alt)
    b_e, b_l = split(base)
    return {
        "delta": round(_net(alt, fee_pct) - _net(base, fee_pct), 4),
        "early": round(_net(a_e, fee_pct) - _net(b_e, fee_pct), 4),
        "late": round(_net(a_l, fee_pct) - _net(b_l, fee_pct), 4),
        "extra_trades": len(alt) - len(base),
    }


def _verdict(d: Dict[str, float]) -> str:
    if d["early"] > 0 and d["late"] > 0:
        return "helps"
    if d["early"] < 0 and d["late"] < 0:
        return "hurts"
    if d["extra_trades"] == 0 and abs(d["delta"]) < 1e-9:
        return "no_effect"
    return "mixed"


def flip_effects(triggers: List[Dict], tf: str, current: Dict[str, bool], proposed: Dict[str, bool],
                 fee_pct: float, cutoff_idx: int) -> List[Dict]:
    """For every filter the proposal changes: the current set with ONLY that filter flipped,
    against the current set. verdict: helps (both windows better) / hurts (both worse) /
    mixed (the windows disagree) / no_effect."""
    base = _sig.replay_filter_set(triggers, current, tf)
    out: List[Dict] = []
    for k in FILTER_KEYS:
        if bool(proposed.get(k)) == bool(current.get(k)):
            continue
        flipped = {**current, k: bool(proposed.get(k))}
        d = _delta(_sig.replay_filter_set(triggers, flipped, tf), base, fee_pct, cutoff_idx)
        out.append({"filter": k, "to": bool(proposed.get(k)), **d, "verdict": _verdict(d)})
    out.sort(key=lambda r: -r["delta"])
    return out


def walk_forward_gate(wf: Dict, mode: str) -> Dict:
    """Does the walk-forward verdict allow a change? strict: only "robust"; lenient: anything
    except "not_robust" and "no_data"."""
    v = wf["verdict"]
    ok = v == "robust" if mode == WF_STRICT else v not in ("not_robust", "no_data")
    agg = wf["aggregate"]
    return {
        "passed": bool(ok), "mode": mode, "verdict": v, "reason": wf["reason"],
        "folds": agg["folds"], "folds_switched": agg["folds_switched"],
        "folds_switched_positive": agg["folds_switched_positive"],
        "in_sample_gain": agg["in_sample_gain"], "out_of_sample_gain": agg["out_of_sample_gain"],
        "policy_vs_current": agg["policy_vs_current"],
        "modal_share": wf["stability"].get("modal_share"),
    }


def verify_choice(triggers: List[Dict], tf: str, current: Dict[str, bool], res: Dict, cutoff_idx: int,
                  fee_pct: float, first_idx: int, last_idx: int, wf_mode: str = WF_STRICT) -> Dict:
    """Runs gates 2 and 3 on a tuner proposal. `res` = choose_filter_set() result with changed=True
    (its "chosen" is the proposal). Returns
      {"proposed", "tuner": {"passed", "reason"}, "flips": [...], "accepted": [filters],
       "dropped": [filters], "trimmed": {...} | None, "walk_forward": {...},
       "decision": "apply" | "keep", "blocked_by": None | "flips" | "trimmed" | "walk_forward",
       "reason", "final": flags, "final_stats": {"train","test","all"} | None}"""
    from walk_forward import walk_forward_filter_sets  # signals <-> walk_forward import each other

    proposed = dict(res["chosen"])
    out: Dict = {
        "proposed": proposed,
        "tuner": {"passed": True, "reason": res.get("reason", "")},
        "flips": [], "accepted": [], "dropped": [], "trimmed": None,
        "walk_forward": None, "decision": "keep", "blocked_by": None, "reason": "",
        "final": dict(current), "final_stats": None,
    }

    # gate 2: every changed filter, alone
    flips = flip_effects(triggers, tf, current, proposed, fee_pct, cutoff_idx)
    out["flips"] = flips
    accepted = [f["filter"] for f in flips if f["verdict"] == "helps"]
    out["accepted"] = accepted
    out["dropped"] = [f["filter"] for f in flips if f["verdict"] != "helps"]
    if not accepted:
        out["blocked_by"] = "flips"
        out["reason"] = ("none of the proposed changes helps in both windows on its own ("
                         + ", ".join(f"{f['filter']}: {f['verdict']}" for f in flips) + ")")
        return out

    final = dict(current)
    for f in flips:
        if f["filter"] in accepted:
            final[f["filter"]] = f["to"]
    base_rows = _sig.replay_filter_set(triggers, current, tf)
    final_rows = _sig.replay_filter_set(triggers, final, tf)
    if out["dropped"]:
        d = _delta(final_rows, base_rows, fee_pct, cutoff_idx)
        out["trimmed"] = {"filters": accepted, **d, "verdict": _verdict(d)}
        if out["trimmed"]["verdict"] != "helps":
            out["blocked_by"] = "trimmed"
            out["reason"] = (f"after dropping {', '.join(out['dropped'])} the remaining change "
                             f"({', '.join(accepted)}) no longer helps in both windows together")
            return out

    # gate 3: walk-forward of the tuning policy for this pair/timeframe
    wf = walk_forward_filter_sets(
        triggers, tf, current, first_idx, last_idx, fee_pct, max(2, min(12, int(_cfg.WALKFWD_FOLDS))),
        _cfg.WALKFWD_INITIAL_TRAIN_FRACTION, _cfg.AUTOTUNE_MIN_TRAIN_TRADES, _cfg.AUTOTUNE_MIN_TEST_TRADES,
        _cfg.AUTOTUNE_MIN_GAIN_SE, _cfg.AUTOTUNE_MIN_TEST_GAIN_SE)
    gate = walk_forward_gate(wf, wf_mode)
    out["walk_forward"] = gate
    if not gate["passed"]:
        out["blocked_by"] = "walk_forward"
        out["reason"] = f"walk-forward is {gate['verdict']}: {gate['reason']}"
        return out

    split = lambda rows: ([t for t in rows if t["idx"] < cutoff_idx],  # noqa: E731
                          [t for t in rows if t["idx"] >= cutoff_idx])
    tr, te = split(final_rows)
    out["final"] = final
    out["final_stats"] = {"train": _sig._window_stats(tr, fee_pct), "test": _sig._window_stats(te, fee_pct),
                          "all": _sig._window_stats(final_rows, fee_pct)}
    out["decision"] = "apply"
    out["reason"] = ("all checks passed" + (f" (dropped {', '.join(out['dropped'])})" if out["dropped"] else ""))
    return out
