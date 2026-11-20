"""Does confirming a trade with market structure pay off in PnL?

The Components analysis (state.analyze_confidence_components) showed that long trades with
volume_profile=+5 / relative_strength=+5 had a larger raw_mfe. raw_mfe is how far price went,
not what the trade earned with the real TP/SL, and the table is cut into many small buckets.
This module asks the question that matters for a possible entry filter, directly in net PnL:

    "If only the trades that pass rule R had been taken, would the result be better?"

Pure read-only computation over the stored history (records that carry
confidence_components; fill the backtest ones with !compfill first). Nothing here changes
trading, history or settings.

For every rule it reports kept vs rejected trades (n, total net PnL, average, win rate),
the average difference with a bootstrap 95% interval, the same split by time (older half /
newer half) and by pair - an effect that only exists in one half or one pair is a
coincidence of the sample, not a rule. Trades of different pairs share one market phase
and A/U trades often share a bar, so the intervals are still optimistic; read them as a
floor on the uncertainty, not a guarantee.
"""
from __future__ import annotations

import random
from typing import Any, Callable, Dict, List, Optional, Tuple

import config as _cfg

BENCHMARK = "BTC/USDT"  # relative_strength has no reading for it (its 0 means n/a)

# rule name -> predicate over (relative_strength, volume_profile, ticker); values are already
# relative to the trade side (+5 = the structure supports THIS side), see signals._rs_vp_components
RULES: Dict[str, Callable[[Optional[float], Optional[float], str], bool]] = {
    "vp+5": lambda rs, vp, t: vp == 5,
    "rs>=0": lambda rs, vp, t: (rs is not None and rs >= 0) or t == BENCHMARK,
    "rs+5": lambda rs, vp, t: rs == 5,
    "vp+5 or rs+5": lambda rs, vp, t: vp == 5 or rs == 5,
    "vp+5 and rs+5": lambda rs, vp, t: vp == 5 and rs == 5,
}

BOOT_ITERS = 2000
MIN_SIDE_N = 15  # a kept/rejected group smaller than this is reported but never called "supported"


def _stats(pn: List[float]) -> Dict[str, Any]:
    n = len(pn)
    return {
        "n": n,
        "total": round(sum(pn), 4),
        "avg": round(sum(pn) / n, 4) if n else None,
        "win_rate": round(sum(1 for x in pn if x > 0) / n, 4) if n else None,
    }


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _boot_diff(kept: List[float], rej: List[float], seed: int) -> Optional[List[float]]:
    """95% bootstrap interval of mean(kept) - mean(rejected); None if either group is empty."""
    if not kept or not rej:
        return None
    rnd = random.Random(seed)
    nk, nr = len(kept), len(rej)
    diffs = []
    for _ in range(BOOT_ITERS):
        mk = sum(kept[rnd.randrange(nk)] for _ in range(nk)) / nk
        mr = sum(rej[rnd.randrange(nr)] for _ in range(nr)) / nr
        diffs.append(mk - mr)
    diffs.sort()
    return [round(diffs[int(0.025 * BOOT_ITERS)], 4), round(diffs[int(0.975 * BOOT_ITERS) - 1], 4)]


def analyze_structure_confirm(history: Optional[Dict] = None, side: str = "long", origin: str = "all",
                              regime: Optional[str] = None, fee_pct: Optional[float] = None) -> Dict[str, Any]:
    """See the module docstring. side: "long" | "short"; regime: None (any) or e.g. "TREND"."""
    import history_tools
    import state

    if side not in ("long", "short"):
        raise ValueError("side must be 'long' or 'short'")
    origin = state.normalize_origin(origin)
    if history is None:
        history = state.load_signals_history()
    fee = _cfg.AUTOTUNE_FEE_PCT if fee_pct is None else float(fee_pct)

    rows: List[Dict[str, Any]] = []
    skipped = {"cancelled": 0, "no_components": 0, "other_regime": 0, "no_time": 0}
    for ticker, tf, rec_side, rec in state._iter_closed_records(history, origin):
        if rec_side != side:
            continue
        if rec.get("exit_type") == "cancelled":
            skipped["cancelled"] += 1  # never a real fill
            continue
        comps = rec.get("confidence_components")
        if not comps or "volume_profile" not in comps:
            skipped["no_components"] += 1
            continue
        if regime and rec.get("regime") != regime:
            skipped["other_regime"] += 1
            continue
        ts = history_tools.record_ts_ms(rec)
        if ts is None:
            skipped["no_time"] += 1
            continue
        try:
            net = float(rec.get("moved_pct", 0.0)) - fee
        except (TypeError, ValueError):
            continue
        rows.append({"pair": ticker, "ts": ts, "net": net,
                     "rs": comps.get("relative_strength"), "vp": comps.get("volume_profile")})

    rows.sort(key=lambda r: r["ts"])
    split_ts = rows[len(rows) // 2]["ts"] if rows else None
    out: Dict[str, Any] = {
        "side": side, "origin": origin, "regime": regime or "any", "fee_pct": fee,
        "n": len(rows), "skipped": skipped, "split_ts": split_ts,
        "baseline": _stats([r["net"] for r in rows]),
        "rules": [],
    }

    for k, (name, pred) in enumerate(RULES.items()):
        kept = [r for r in rows if pred(r["rs"], r["vp"], r["pair"])]
        rej = [r for r in rows if not pred(r["rs"], r["vp"], r["pair"])]
        kp, rp = [r["net"] for r in kept], [r["net"] for r in rej]
        mk, mr = _mean(kp), _mean(rp)
        diff = None if mk is None or mr is None else round(mk - mr, 4)
        ci = _boot_diff(kp, rp, seed=1000 + k)

        halves = []
        for part in ("older", "newer"):
            sel = (lambda r: r["ts"] < split_ts) if part == "older" else (lambda r: r["ts"] >= split_ts)
            hk = [r["net"] for r in kept if sel(r)]
            hr = [r["net"] for r in rej if sel(r)]
            a, b = _mean(hk), _mean(hr)
            halves.append({"half": part, "kept_n": len(hk), "rej_n": len(hr),
                           "kept_avg": None if a is None else round(a, 4),
                           "rej_avg": None if b is None else round(b, 4),
                           "diff": None if a is None or b is None else round(a - b, 4)})

        pair_diffs: Dict[str, float] = {}
        for pair in sorted({r["pair"] for r in rows}):
            pk = [r["net"] for r in kept if r["pair"] == pair]
            pr = [r["net"] for r in rej if r["pair"] == pair]
            if len(pk) >= 5 and len(pr) >= 5:  # a smaller group says nothing about the pair
                pair_diffs[pair] = round(_mean(pk) - _mean(pr), 4)
        pairs_pos = sum(1 for d in pair_diffs.values() if d > 0)

        halves_ok = all(h["diff"] is not None and h["diff"] > 0 for h in halves)
        enough = len(kp) >= MIN_SIDE_N and len(rp) >= MIN_SIDE_N
        if not enough:
            verdict = "too_few"
        elif ci is not None and ci[0] > 0 and halves_ok and (not pair_diffs or pairs_pos * 2 > len(pair_diffs)):
            verdict = "supported"
        elif diff is not None and diff > 0 and (halves_ok or (ci is not None and ci[0] > 0)):
            verdict = "mixed"
        else:
            verdict = "not_supported"

        out["rules"].append({
            "rule": name,
            "kept": _stats(kp), "rejected": _stats(rp),
            "kept_share": round(len(kp) / len(rows), 4) if rows else None,
            "diff_avg": diff, "ci95": ci,
            "halves": halves,
            "pairs": pair_diffs, "pairs_pos": pairs_pos, "pairs_n": len(pair_diffs),
            "verdict": verdict,
        })
    return out
