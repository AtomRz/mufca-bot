"""
Backups of signals_history.json.

One backup = two small files in config.HISTORY_BACKUP_DIR:
    <id>.json       {"format": 1, "meta": {...}, "history": {...}}  (the data)
    <id>.meta.json  just the meta (so listing never has to parse the data)

Kinds:
    manual        made by the user (Backup tab / !backup) - never deleted automatically
    auto          periodic / on start (skipped when nothing changed since the last backup)
    pre-restore, pre-fill, pre-backfill, pre-tag, pre-reset, pre-delete
                  safety copies taken right before an operation that changes history
Everything except "manual" is rotated: only the newest HISTORY_BACKUP_KEEP_AUTO stay.

Restore never touches OPEN trades: a trade that is open right now stays exactly as
it is (the scanner holds it in memory), and an "open" record that only exists in
the backup is not brought back (its trade has long been closed or lost). A safety
copy ("pre-restore") of the current history is always taken first, so a restore
can itself be undone.
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config as _cfg
import state
import history_tools

logger = logging.getLogger(__name__)

KINDS = ("manual", "auto", "pre-restore", "pre-fill", "pre-backfill", "pre-tag", "pre-dedupe", "pre-sort", "pre-reset", "pre-delete")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,100}$")
_FORMAT = 1


class BackupError(Exception):
    """A problem the caller can show to the user as-is."""


# ── paths ─────────────────────────────────────────────────────────────
def _dir() -> str:
    os.makedirs(_cfg.HISTORY_BACKUP_DIR, exist_ok=True)
    return _cfg.HISTORY_BACKUP_DIR


def _data_path(backup_id: str) -> str:
    if not isinstance(backup_id, str) or not _ID_RE.match(backup_id):
        raise BackupError("Invalid backup id")
    return os.path.join(_dir(), f"{backup_id}.json")


def _meta_path(backup_id: str) -> str:
    return _data_path(backup_id)[:-len(".json")] + ".meta.json"


def _write_atomic(path: str, payload: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ── helpers ───────────────────────────────────────────────────────────
def _snapshot_current() -> Dict:
    """A private copy of the history as it is now. Retries when a worker thread
    changes the live dict in the middle of the copy."""
    last: Optional[Exception] = None
    for _ in range(5):
        try:
            return copy.deepcopy(state.load_signals_history())
        except RuntimeError as e:  # "dictionary changed size during iteration"
            last = e
            time.sleep(0.05)
    raise BackupError(f"could not take a consistent snapshot of the history: {last}")


def _fingerprint(history: Dict) -> str:
    raw = json.dumps(history, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _counts(history: Dict) -> Dict[str, Any]:
    total = closed = opened = 0
    by_source: Dict[str, int] = {}
    pairs = set()
    for ticker, tf, _side, rec in history_tools._iter_records(history):
        total += 1
        pairs.add(f"{ticker} {tf}")
        if rec.get("exit_type") == "open":
            opened += 1
        elif rec.get("exit_type") not in (None, "unfilled"):
            closed += 1
        src = history_tools.classify_record_source(rec)[0]
        by_source[src] = by_source.get(src, 0) + 1
    return {"records": total, "closed": closed, "open": opened, "pairs": sorted(pairs), "by_source": by_source}


def current_counts() -> Dict[str, Any]:
    """The same counts a backup's meta carries, for the history as it is now."""
    return _counts(state.load_signals_history())


def _new_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{kind}-{secrets.token_hex(2)}"


# ── public API ────────────────────────────────────────────────────────
def create_backup(kind: str = "manual", label: str = "", skip_if_unchanged: bool = False,
                  history: Optional[Dict] = None) -> Dict[str, Any]:
    """Writes a backup of the history (the current one unless `history` is given)
    and returns its meta. With skip_if_unchanged, returns the newest existing
    backup (meta + "skipped": True) when it already holds exactly this history."""
    if kind not in KINDS:
        raise BackupError(f"Unknown backup kind {kind!r}")
    snap = history if history is not None else _snapshot_current()
    fp = _fingerprint(snap)
    if skip_if_unchanged:
        existing = list_backups()
        if existing and existing[0].get("sha256") == fp:
            return {**existing[0], "skipped": True}

    backup_id = _new_id(kind)
    meta = {
        "id": backup_id,
        "kind": kind,
        "label": (label or "").strip()[:200],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sha256": fp,
        **_counts(snap),
    }
    _write_atomic(_data_path(backup_id), {"format": _FORMAT, "meta": meta, "history": snap})
    meta["size_bytes"] = os.path.getsize(_data_path(backup_id))
    _write_atomic(_meta_path(backup_id), meta)
    logger.info(f"[BACKUP] {kind} backup {backup_id} written ({meta['records']} records, {meta['size_bytes']} bytes)")
    _rotate()
    return meta


def safe_pre_backup(kind: str, label: str) -> Optional[str]:
    """Safety copy before a destructive operation that was already confirmed by
    the user. Never raises: returns the backup id, or None when it failed (the
    caller then tells the user instead of silently carrying on)."""
    try:
        return create_backup(kind, label)["id"]
    except Exception as e:
        logger.error(f"[BACKUP] pre-operation backup ({kind}) failed: {e}", exc_info=True)
        return None


def list_backups() -> List[Dict[str, Any]]:
    """Meta of every backup, newest first."""
    out: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(_dir()))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name.endswith(".meta.json"):
            continue
        bid = name[:-len(".json")]
        if not _ID_RE.match(bid):
            continue
        meta = None
        try:
            with open(_meta_path(bid), "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            try:  # sidecar lost: rebuild it from the data file
                with open(_data_path(bid), "r", encoding="utf-8") as f:
                    meta = json.load(f).get("meta")
                if isinstance(meta, dict):
                    meta["size_bytes"] = os.path.getsize(_data_path(bid))
                    _write_atomic(_meta_path(bid), meta)
            except (OSError, ValueError):
                meta = None
        if isinstance(meta, dict) and meta.get("id") == bid:
            out.append(meta)
    out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return out


def load_backup(backup_id: str) -> Dict[str, Any]:
    path = _data_path(backup_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise BackupError("Backup not found")
    except (OSError, ValueError) as e:
        raise BackupError(f"Backup file is unreadable: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("history"), dict):
        raise BackupError("Backup file has an unexpected format")
    return data


def read_backup_bytes(backup_id: str) -> bytes:
    path = _data_path(backup_id)
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        raise BackupError("Backup not found")


def delete_backup(backup_id: str) -> None:
    path = _data_path(backup_id)
    if not os.path.exists(path):
        raise BackupError("Backup not found")
    os.remove(path)
    try:
        os.remove(_meta_path(backup_id))
    except FileNotFoundError:
        pass
    logger.info(f"[BACKUP] deleted {backup_id}")


def _rotate() -> None:
    keep = max(1, int(_cfg.HISTORY_BACKUP_KEEP_AUTO))
    rotating = [m for m in list_backups() if m.get("kind") != "manual"]
    for meta in rotating[keep:]:
        try:
            delete_backup(meta["id"])
        except BackupError:
            pass


def backup_detail(backup_id: str) -> Dict[str, Any]:
    """Per pair/timeframe/side: records in the backup vs in the history now."""
    data = load_backup(backup_id)
    old, cur = data["history"], state.load_signals_history()

    def table(h: Dict) -> Dict[tuple, Dict[str, int]]:
        cells: Dict[tuple, Dict[str, int]] = {}
        for ticker, tf, side, rec in history_tools._iter_records(h):
            c = cells.setdefault((ticker, tf, side), {"n": 0, "live": 0, "backtest": 0})
            c["n"] += 1
            src = history_tools.classify_record_source(rec)[0]
            if src == "live":
                c["live"] += 1
            elif src in history_tools.BACKTEST_SOURCES:
                c["backtest"] += 1
        return cells

    a, b = table(old), table(cur)
    rows = []
    for key in sorted(set(a) | set(b)):
        x, y = a.get(key, {"n": 0, "live": 0, "backtest": 0}), b.get(key, {"n": 0, "live": 0, "backtest": 0})
        rows.append({
            "ticker": key[0], "tf": key[1], "side": key[2],
            "backup": x["n"], "now": y["n"],
            "backup_live": x["live"], "now_live": y["live"],
            "backup_backtest": x["backtest"], "now_backtest": y["backtest"],
        })
    return {"meta": data.get("meta"), "rows": rows}


def restore_backup(backup_id: str, ticker: Optional[str] = None, tf: Optional[str] = None) -> Dict[str, Any]:
    """Replaces the stored history with the backup's (everything, or only
    `ticker` / `ticker`+`tf`). Open trades stay as they are now. A safety copy of
    the current history is written first."""
    data = load_backup(backup_id)
    old: Dict = data["history"]
    if tf and not ticker:
        raise BackupError("tf needs a ticker")

    pre = create_backup("pre-restore", f"before restoring {backup_id}")
    cur = state.load_signals_history()
    result = copy.deepcopy(cur)

    if ticker:
        if tf:
            slots = {(ticker, tf)}
        else:
            slots = {(ticker, f) for f in (set((old.get(ticker) or {}).keys()) | set((cur.get(ticker) or {}).keys()))}
    else:
        slots = {(t, f) for h in (old, cur) for t, tfs in h.items() if isinstance(tfs, dict) for f in tfs}

    restored = kept_open = 0
    for t, f in sorted(slots):
        old_slot = (old.get(t) or {}).get(f) or {}
        cur_slot = (cur.get(t) or {}).get(f) or {}
        if not old_slot and not cur_slot:
            continue
        new_slot: Dict[str, List[Dict]] = {}
        for side in ("long", "short"):
            from_backup = [copy.deepcopy(r) for r in (old_slot.get(side) or [])
                           if isinstance(r, dict) and r.get("exit_type") != "open"]
            open_now = [copy.deepcopy(r) for r in (cur_slot.get(side) or [])
                        if isinstance(r, dict) and r.get("exit_type") == "open"]
            new_slot[side] = from_backup + open_now
            restored += len(from_backup)
            kept_open += len(open_now)
        if any(new_slot.values()):
            result.setdefault(t, {})[f] = new_slot
        else:
            result.get(t, {}).pop(f, None)
            if t in result and not result[t]:
                del result[t]

    state.save_signals_history(result)
    logger.info(f"[BACKUP] restored {backup_id} (scope={ticker or 'all'} {tf or ''}): {restored} records, {kept_open} open kept")
    return {"restored_from": backup_id, "scope": {"ticker": ticker, "tf": tf},
            "records": restored, "open_kept": kept_open, "pre_restore_backup": pre["id"]}


# ── periodic automatic backups ────────────────────────────────────────
_auto_task: Optional["asyncio.Task"] = None


async def _auto_loop() -> None:
    while True:
        await asyncio.sleep(max(1, _cfg.HISTORY_BACKUP_INTERVAL_HOURS) * 3600)
        try:
            create_backup("auto", "periodic", skip_if_unchanged=True)
        except Exception as e:
            logger.error(f"[BACKUP] periodic backup failed: {e}", exc_info=True)


def start_auto_backups() -> None:
    """One backup now (skipped when unchanged), then one every
    HISTORY_BACKUP_INTERVAL_HOURS. Safe to call more than once."""
    global _auto_task
    try:
        create_backup("auto", "on start", skip_if_unchanged=True)
    except Exception as e:
        logger.error(f"[BACKUP] start-up backup failed: {e}", exc_info=True)
    if _auto_task is None or _auto_task.done():
        _auto_task = asyncio.create_task(_auto_loop())
