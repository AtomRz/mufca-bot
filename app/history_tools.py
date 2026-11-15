"""
History tools: where a record came from, how a fresh backtest compares with the
stored history, and a merge that only ever ADDS records.

Used by the web "Backup" tab (/api/history/*) and the Discord !btaudit / !btfill
commands. Pure functions - no network, no file I/O - so they can be tested
without an exchange.

Record origins ("source"):
    live               the real scanner (add_signal_record)
    sim                synthetic (!sim) - never judged, never counted
    backtest           the first/bootstrap backtest (startup_sequence, !reset)
    backtest_fill      a signal the audit found missing INSIDE the stored window
    backtest_backfill  a signal older than the oldest stored record, added by the audit
    unknown            cannot be told (unparseable timestamp)

Older records carry no "source" key. classify_record_source() tells them apart
from what they contain:
  - the real scanner stamps its records with timestamp = datetime.now(), which
    has seconds and microseconds; a backtest stamps the OPEN of the signal bar,
    which is a whole minute (seconds == 0, microsecond == 0);
  - only live records ever carry tp1_hit / raw_mfe_pct / tp_raw / cap_* /
    synthetic, and only they can still be "open".
"""

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from state import _parse_ts, sort_records_chronological


def parse_record_ts(value: Any) -> Optional[datetime]:
    """Timestamp of a stored record as an aware UTC datetime (ISO strings and epoch
    seconds / milliseconds, also as digit strings - see state._parse_ts)."""
    return _parse_ts(value)

SOURCES = ("live", "sim", "backtest", "backtest_fill", "backtest_backfill", "unknown")
BACKTEST_SOURCES = ("backtest", "backtest_fill", "backtest_backfill")

# keys the real scanner writes and a backtest never does
_LIVE_ONLY_KEYS = (
    "tp1_hit", "raw_mfe_pct", "raw_mae_pct", "raw_tracking_bars_remaining",
    "tp_raw", "cap_reason", "cap_level", "synthetic",
)

_TF_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def tf_to_ms(tf: str) -> Optional[int]:
    """'15m' / '1h' / '4h' / '1d' -> milliseconds; None when it can't be read."""
    try:
        return int(tf[:-1]) * _TF_UNIT_MS[tf[-1].lower()]
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def record_ts_ms(rec: Dict[str, Any]) -> Optional[int]:
    dt = parse_record_ts(rec.get("timestamp"))
    return int(dt.timestamp() * 1000) if dt is not None else None


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def classify_record_source(rec: Dict[str, Any]) -> Tuple[str, str]:
    """(source, basis) of one history record. `basis` says how it was decided:
    tag | synthetic | open | live_fields | bar_aligned | off_grid | unparseable."""
    src = rec.get("source")
    if isinstance(src, str) and src:
        return src, "tag"
    if rec.get("synthetic"):
        return "sim", "synthetic"
    if rec.get("exit_type") == "open":
        return "live", "open"
    if any(k in rec for k in _LIVE_ONLY_KEYS):
        return "live", "live_fields"
    dt = parse_record_ts(rec.get("timestamp"))
    if dt is None:
        return "unknown", "unparseable"
    if dt.second == 0 and dt.microsecond == 0:
        return "backtest", "bar_aligned"
    return "live", "off_grid"


def _iter_records(history: Dict) -> Iterable[Tuple[str, str, str, Dict]]:
    for ticker, tfs in history.items():
        if not isinstance(tfs, dict) or ticker.startswith("__"):
            continue
        for tf, sides in tfs.items():
            if not isinstance(sides, dict):
                continue
            for side in ("long", "short"):
                for rec in sides.get(side, []) or []:
                    if isinstance(rec, dict):
                        yield ticker, tf, side, rec


def source_summary(history: Dict) -> Dict[str, Any]:
    """Counts by origin: overall, per pair/timeframe/side, and how each was decided."""
    total: Dict[str, int] = {}
    by_basis: Dict[str, int] = {}
    slots: Dict[Tuple[str, str, str], Dict[str, int]] = {}
    untagged = 0
    for ticker, tf, side, rec in _iter_records(history):
        src, basis = classify_record_source(rec)
        total[src] = total.get(src, 0) + 1
        by_basis[basis] = by_basis.get(basis, 0) + 1
        if basis != "tag":
            untagged += 1
        cell = slots.setdefault((ticker, tf, side), {})
        cell[src] = cell.get(src, 0) + 1
    rows = [
        {"ticker": t, "tf": f, "side": s, "counts": c, "n": sum(c.values())}
        for (t, f, s), c in sorted(slots.items())
    ]
    return {"total": total, "by_basis": by_basis, "untagged": untagged, "rows": rows}


def tag_sources(history: Dict) -> Dict[str, int]:
    """Writes the inferred "source" into every record that has none (in place).
    Records that cannot be classified stay untouched. Returns counts."""
    tagged: Dict[str, int] = {}
    unknown = 0
    for _t, _f, _s, rec in _iter_records(history):
        if isinstance(rec.get("source"), str) and rec["source"]:
            continue
        src, _basis = classify_record_source(rec)
        if src == "unknown":
            unknown += 1
            continue
        rec["source"] = src
        tagged[src] = tagged.get(src, 0) + 1
    return {"tagged": sum(tagged.values()), "by_source": tagged, "unknown": unknown}


# =====================================================================
# 🔍  BACKTEST vs STORED HISTORY
# =====================================================================
def _match(bt: List[Dict], hist: List[Tuple[int, Dict]], window_ms: int):
    """Greedy one-to-one matching of two time-sorted lists. A backtest signal at
    bar-open b matches a stored record stamped in [b, b + window_ms): a backtest
    record carries b itself, a live one the moment the closed bar was noticed.
    Returns (pairs [(bt_i, hist_j)], bt_unmatched [i], hist_unmatched [j])."""
    pairs: List[Tuple[int, int]] = []
    bt_un: List[int] = []
    hist_un: List[int] = []
    j = 0
    for i, trade in enumerate(bt):
        b = trade["ts"]
        while j < len(hist) and hist[j][0] < b:
            hist_un.append(j)
            j += 1
        if j < len(hist) and hist[j][0] < b + window_ms:
            pairs.append((i, j))
            j += 1
        else:
            bt_un.append(i)
    hist_un.extend(range(j, len(hist)))
    return pairs, bt_un, hist_un


def _brief(ts: int, rec: Dict, **extra) -> Dict[str, Any]:
    return {
        "ts": ms_to_iso(ts),
        "track": rec.get("track", "a"),
        "entry": rec.get("entry"),
        "exit_type": rec.get("exit_type"),
        "moved_pct": rec.get("moved_pct"),
        **extra,
    }


def reconcile_slot(
    ticker: str,
    tf: str,
    bt_trades: List[Dict],
    history: Dict,
    coverage: Optional[Dict[str, int]] = None,
    tolerance_bars: int = 2,
    sample: int = 40,
) -> Dict[str, Any]:
    """Compares one fresh backtest (bt_trades = backtest_history(...,
    collect_records=True) "trades") with the stored history of ticker/tf.

    Per side the stored window starts at the oldest stored record (a stored list is
    trimmed from the old end, so nothing older can be judged):
      matched   backtest signal that has a stored record
      missed    backtest signal INSIDE the window without one  -> fill candidates
      older     backtest signal before the window              -> backfill candidates
      extra     stored record (not sim) the backtest does not reproduce, split into
                  live_only       real scanner records - expected: live has things a
                                  backtest lacks (spread/derivatives gates, intrabar
                                  checks, the exact moment of entry)
                  stale_backtest  backtest-origin records the CURRENT logic would not
                                  produce any more
                  outside         outside what the backtest evaluated - not judgeable
      changed   matched backtest-origin records whose outcome differs now

    Nothing here writes anything. The same dict also carries the records to insert
    ("_fill" / "_backfill", full records) - strip them with public_report() before
    sending the report anywhere."""
    tf_ms = tf_to_ms(tf) or 3_600_000
    window_ms = max(1, tolerance_bars) * tf_ms
    slot = (history.get(ticker) or {}).get(tf) or {}
    cov = coverage or {}
    out: Dict[str, Any] = {"ticker": ticker, "tf": tf, "rows": [], "missed": [], "older": [],
                           "extra": [], "changed": [], "_fill": {}, "_backfill": {}}

    tracks = sorted({t.get("track", "a") for t in bt_trades} |
                    {r.get("track", "a") for side in ("long", "short") for r in slot.get(side, []) or []})

    for side in ("long", "short"):
        stored = [r for r in (slot.get(side) or []) if isinstance(r, dict)]
        real = [(record_ts_ms(r), r) for r in stored if classify_record_source(r)[0] != "sim"]
        real = sorted([(ts, r) for ts, r in real if ts is not None], key=lambda x: x[0])
        window_start = real[0][0] if real else None
        fill: List[Dict] = []
        backfill: List[Dict] = []

        for track in tracks:
            bt = sorted((t for t in bt_trades if t["side"] == side and t.get("track", "a") == track),
                        key=lambda t: t["ts"])
            hist = [(ts, r) for ts, r in real if r.get("track", "a") == track]
            pairs, bt_un, hist_un = _match(bt, hist, window_ms)

            older, missed = [], []
            for i in bt_un:
                b = bt[i]["ts"]
                if window_start is None or b + window_ms <= window_start:
                    older.append(bt[i])
                else:
                    missed.append(bt[i])

            eval_from = cov.get("a_eval_from_ts" if track == "a" else "eval_from_ts")
            eval_to = cov.get("eval_to_ts")
            live_only = stale = outside = 0
            for j in hist_un:
                ts, r = hist[j]
                src = classify_record_source(r)[0]
                if (eval_from is not None and ts < eval_from) or (eval_to is not None and ts > eval_to + tf_ms):
                    outside += 1
                    continue
                if src in BACKTEST_SOURCES:
                    stale += 1
                else:
                    live_only += 1
                if len(out["extra"]) < sample:
                    out["extra"].append(_brief(ts, r, side=side, source=src))

            changed = 0
            for i, j in pairs:
                r = hist[j][1]
                if classify_record_source(r)[0] in BACKTEST_SOURCES and r.get("exit_type") not in (None, "open"):
                    if r.get("exit_type") != bt[i].get("exit_type"):
                        changed += 1
                        if len(out["changed"]) < sample:
                            out["changed"].append({**_brief(hist[j][0], r, side=side),
                                                   "now": bt[i].get("exit_type"), "now_pnl": bt[i].get("moved_pct")})

            for tr in missed:
                if len(out["missed"]) < sample:
                    out["missed"].append(_brief(tr["ts"], tr["rec"], side=side))
            fill.extend(missed)
            backfill.extend(older)
            out["rows"].append({
                "side": side, "track": track, "backtest": len(bt), "stored": len(hist),
                "matched": len(pairs), "missed": len(missed), "older": len(older),
                "live_only": live_only, "stale_backtest": stale, "outside": outside, "changed": changed,
            })

        out["older"].append({"side": side, "n": len(backfill),
                             "from": ms_to_iso(min(t["ts"] for t in backfill)) if backfill else None,
                             "to": ms_to_iso(max(t["ts"] for t in backfill)) if backfill else None})
        out["window_start_" + side] = ms_to_iso(window_start) if window_start is not None else None
        out["stored_total_" + side] = len(stored)
        out["_fill"][side] = fill
        out["_backfill"][side] = backfill
    return out


def public_report(slot_report: Dict[str, Any]) -> Dict[str, Any]:
    """The report without the records meant for insertion."""
    return {k: v for k, v in slot_report.items() if not k.startswith("_")}


# =====================================================================
# ➕  ADD-ONLY MERGE
# =====================================================================
def _insert_chronological(records: List[Dict], rec: Dict) -> None:
    new_ts = record_ts_ms(rec)
    if new_ts is None:
        records.append(rec)
        return
    for i, cur in enumerate(records):
        ts = record_ts_ms(cur)
        if ts is not None and ts > new_ts:
            records.insert(i, rec)
            return
    records.append(rec)


def merge_missing(history: Dict, slot_report: Dict[str, Any], keep: int,
                  do_fill: bool, do_backfill: bool) -> Dict[str, Any]:
    """Adds the backtest signals the report found missing (fill = inside the stored
    window, backfill = older than it) to `history` IN PLACE. Add-only: no stored
    record is ever removed or changed. A side list may not grow past `keep`;
    when there is not enough room the newest signals go in first (fill before
    backfill) and the rest is counted in "no_room". Records are re-checked against
    `history` as it is NOW (it may have changed since the audit ran), so a signal
    that has meanwhile been stored is not added twice."""
    ticker, tf = slot_report["ticker"], slot_report["tf"]
    tf_ms = tf_to_ms(tf) or 3_600_000
    result = {"ticker": ticker, "tf": tf, "filled": 0, "backfilled": 0, "no_room": 0, "already_there": 0}
    for side in ("long", "short"):
        lst = history.setdefault(ticker, {}).setdefault(tf, {"long": [], "short": []}).setdefault(side, [])
        existing_ts = [(r.get("track", "a"), record_ts_ms(r)) for r in lst
                       if isinstance(r, dict) and classify_record_source(r)[0] != "sim"]

        def already_stored(tr: Dict) -> bool:
            b = tr["ts"]
            return any(trk == tr.get("track", "a") and ts is not None and b <= ts < b + 2 * tf_ms
                       for trk, ts in existing_ts)

        plan: List[Tuple[str, Dict]] = []
        if do_fill:
            plan += [("backtest_fill", t) for t in sorted(slot_report["_fill"].get(side, []), key=lambda t: -t["ts"])]
        if do_backfill:
            plan += [("backtest_backfill", t) for t in sorted(slot_report["_backfill"].get(side, []), key=lambda t: -t["ts"])]
        room = max(0, keep - len(lst))
        for source, tr in plan:
            if already_stored(tr):
                result["already_there"] += 1
                continue
            if room <= 0:
                result["no_room"] += 1
                continue
            rec = dict(tr["rec"])
            rec["source"] = source
            _insert_chronological(lst, rec)
            existing_ts.append((rec.get("track", "a"), record_ts_ms(rec)))
            room -= 1
            result["filled" if source == "backtest_fill" else "backfilled"] += 1
    return result


# =====================================================================
# 🧹  DUPLICATES ADDED BY AN AUDIT
# =====================================================================
ADDED_SOURCES = ("backtest_fill", "backtest_backfill")


def _duplicate_positions(history: Dict) -> List[Tuple[str, str, str, int]]:
    """(ticker, tf, side, index) of every record an audit ADDED (backtest_fill /
    backtest_backfill) although the history already held the same signal: another
    record of the same track, not itself an audit addition, stamped in
    [b, b + 2 bars) of the added record's bar open b - the same rule
    merge_missing() uses to decide a signal is already stored."""
    out: List[Tuple[str, str, str, int]] = []
    for ticker, tfs in history.items():
        if not isinstance(tfs, dict) or ticker.startswith("__"):
            continue
        for tf, sides in tfs.items():
            if not isinstance(sides, dict):
                continue
            tf_ms = tf_to_ms(tf) or 3_600_000
            for side in ("long", "short"):
                recs = sides.get(side) or []
                originals: Dict[str, List[int]] = {}
                for r in recs:
                    if not isinstance(r, dict):
                        continue
                    src = classify_record_source(r)[0]
                    ts = record_ts_ms(r)
                    if src in ADDED_SOURCES or src == "sim" or ts is None:
                        continue
                    originals.setdefault(r.get("track", "a"), []).append(ts)
                for i, r in enumerate(recs):
                    if not isinstance(r, dict) or r.get("source") not in ADDED_SOURCES:
                        continue
                    b = record_ts_ms(r)
                    if b is None:
                        continue
                    if any(b <= ts < b + 2 * tf_ms for ts in originals.get(r.get("track", "a"), [])):
                        out.append((ticker, tf, side, i))
    return out


def count_added_duplicates(history: Dict) -> Dict[str, Any]:
    """Preview for remove_added_duplicates(): how many, and where. Writes nothing."""
    dups = _duplicate_positions(history)
    per: Dict[Tuple[str, str, str], int] = {}
    for t, f, s, _i in dups:
        per[(t, f, s)] = per.get((t, f, s), 0) + 1
    return {"duplicates": len(dups),
            "rows": [{"ticker": t, "tf": f, "side": s, "n": n} for (t, f, s), n in sorted(per.items())]}


def remove_added_duplicates(history: Dict) -> int:
    """Deletes (in place) the audit-added copies found by count_added_duplicates().
    The ORIGINAL record always stays; only records tagged backtest_fill /
    backtest_backfill are ever removed. Returns how many were removed."""
    dups = _duplicate_positions(history)
    by_slot: Dict[Tuple[str, str, str], set] = {}
    for t, f, s, i in dups:
        by_slot.setdefault((t, f, s), set()).add(i)
    for (t, f, s), idxs in by_slot.items():
        lst = history[t][f][s]
        history[t][f][s] = [r for i, r in enumerate(lst) if i not in idxs]
    return len(dups)


# =====================================================================
# 🗓️  CHRONOLOGICAL ORDER
# =====================================================================

def _unsorted_slots(history: Dict) -> List[Tuple[str, str, str]]:
    out = []
    for t, tfs in history.items():
        for f, sides in (tfs or {}).items():
            for side, lst in (sides or {}).items():
                if isinstance(lst, list) and len(lst) > 1:
                    probe = list(lst)
                    if sort_records_chronological(probe):
                        out.append((t, f, side))
    return out


def count_unsorted(history: Dict) -> Dict[str, Any]:
    """Preview for sort_history(): which side lists are not in date order. Writes nothing."""
    slots = _unsorted_slots(history)
    return {"slots": len(slots),
            "rows": [{"ticker": t, "tf": f, "side": s, "n": len(history[t][f][s])} for t, f, s in slots]}


def sort_history(history: Dict) -> int:
    """Puts every side list in date order (oldest first), IN PLACE. Nothing is added or
    removed, only reordered. Returns how many side lists changed."""
    n = 0
    for t, f, s in _unsorted_slots(history):
        sort_records_chronological(history[t][f][s])
        n += 1
    return n
