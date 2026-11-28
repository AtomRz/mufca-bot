"""
SQLite storage layer (stdlib sqlite3 only - nothing to install, one file in DATA_DIR).

What lives here:
    signals   the signal history, one row per record (was signals_history.json)
    kv        small named JSON documents, e.g. the open-position snapshot
              (was bot_state_snapshot.json)

What deliberately stays in JSON: the ~40 small hand-editable config files in
DATA_DIR (hurst_window.json, entry_block.json, ...). They are read by eye and
edited by hand, and nothing ever queries them.

The rest of the bot still sees the same nested dict
    history[ticker][tf]["long" | "short"] = [record, ...]
so state.py keeps its in-memory cache and every `rec["exit_type"] = ...` style
mutation keeps working. This module only changes where that dict is persisted.

SQL injection rules (every function in this file follows them):
    1. Values are ALWAYS bound parameters (`?`). No f-strings / concatenation / %
       formatting ever builds SQL text from data.
    2. Identifiers (table / column names) never come from callers. Where a caller
       picks a column or direction (query_signals order_by), the choice is mapped
       through a fixed whitelist and the SQL text only ever contains the mapped
       constant.
    3. ticker / tf / side / track / exit_type are validated against a strict
       pattern before they touch the database, so garbage from the web API or a
       Discord command is rejected early instead of being stored.
"""

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import config as _cfg

logger = logging.getLogger(__name__)

DB_FILE = os.path.join(_cfg.DATA_DIR, "mufca.db")
SCHEMA_VERSION = 1

# ── validation (rule 3) ───────────────────────────────────────────────
_TICKER_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,40}$")
_TF_RE = re.compile(r"^[0-9]{1,3}[smhdwM]$")
_TRACK_RE = re.compile(r"^[a-z0-9_]{1,20}$")
_EXIT_RE = re.compile(r"^[a-z0-9_]{1,30}$")
_KV_NAME_RE = re.compile(r"^[a-z0-9_]{1,60}$")
SIDES = ("long", "short")

# Whitelisted ORDER BY choices (rule 2): the caller's string is only a lookup key.
_ORDER_BY = {
    "pos": "pos ASC",
    "newest": "ts DESC, id DESC",
    "oldest": "ts ASC, id ASC",
}


class StorageError(Exception):
    """A problem the caller can show to the user as-is."""


def _check(value: Any, pattern: "re.Pattern", what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise StorageError(f"Invalid {what}: {value!r}")
    return value


def _check_side(side: Any) -> str:
    if side not in SIDES:
        raise StorageError(f"Invalid side: {side!r}")
    return side


# ── connections ───────────────────────────────────────────────────────
# The bot runs scanner coroutines, asyncio.to_thread workers (backtests) and a
# process pool; sqlite3 connections must not be shared across threads, so each
# thread gets its own. WAL lets the web API / Discord read while the scanner writes.
_local = threading.local()
_init_lock = threading.Lock()
_initialized_path: Optional[str] = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30, isolation_level=None)  # explicit BEGIN/COMMIT below
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _conn() -> sqlite3.Connection:
    _ensure_schema()
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != DB_FILE:
        conn = _connect()
        _local.conn = conn
        _local.path = DB_FILE
    return conn


def _ensure_schema() -> None:
    global _initialized_path
    if _initialized_path == DB_FILE:
        return
    with _init_lock:
        if _initialized_path == DB_FILE:
            return
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS signals (
                       id      INTEGER PRIMARY KEY AUTOINCREMENT,
                       ticker  TEXT NOT NULL,
                       tf      TEXT NOT NULL,
                       side    TEXT NOT NULL CHECK (side IN ('long','short')),
                       pos     INTEGER NOT NULL,
                       track   TEXT,
                       ts      TEXT,
                       exit_type TEXT,
                       payload TEXT NOT NULL
                   )"""
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_slot "
                "ON signals (ticker, tf, side, pos)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_signals_track ON signals (track, exit_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals (ts)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kv (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        _initialized_path = DB_FILE


def _tx() -> "_Tx":
    return _Tx()


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT (ROLLBACK on error), on this thread's connection."""

    def __enter__(self) -> sqlite3.Connection:
        self.conn = _conn()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return False


# ── signal history ────────────────────────────────────────────────────
def _row_params(ticker: str, tf: str, side: str, pos: int, rec: Dict) -> Tuple:
    track = rec.get("track", "a")
    exit_type = rec.get("exit_type")
    ts = rec.get("timestamp")
    return (
        ticker,
        tf,
        side,
        pos,
        track if isinstance(track, str) and _TRACK_RE.match(track) else None,
        ts if isinstance(ts, str) else (None if ts is None else str(ts)),
        exit_type if isinstance(exit_type, str) and _EXIT_RE.match(exit_type) else None,
        json.dumps(rec, separators=(",", ":"), ensure_ascii=False, default=str),
    )


def save_history(history: Dict) -> int:
    """Replaces the stored history with `history` in ONE transaction: a crash or a
    concurrent reader sees either the old history or the new one, never half.
    Returns the number of records written."""
    rows: List[Tuple] = []
    for ticker, tfs in (history or {}).items():
        if not isinstance(tfs, dict):
            continue
        _check(ticker, _TICKER_RE, "ticker")
        for tf, sides in tfs.items():
            if not isinstance(sides, dict):
                continue
            _check(tf, _TF_RE, "timeframe")
            for side in SIDES:
                for pos, rec in enumerate(sides.get(side) or []):
                    if isinstance(rec, dict):
                        rows.append(_row_params(ticker, tf, side, pos, rec))
    with _tx() as conn:
        conn.execute("DELETE FROM signals")
        conn.executemany(
            "INSERT INTO signals (ticker, tf, side, pos, track, ts, exit_type, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def load_history() -> Dict:
    """The whole history as the nested dict the rest of the bot expects."""
    out: Dict[str, Dict[str, Dict[str, List[Dict]]]] = {}
    cur = _conn().execute(
        "SELECT ticker, tf, side, payload FROM signals ORDER BY ticker, tf, side, pos"
    )
    for ticker, tf, side, payload in cur:
        try:
            rec = json.loads(payload)
        except ValueError:
            logger.error(f"[STORAGE] unreadable record skipped: {ticker} {tf} {side}")
            continue
        out.setdefault(ticker, {}).setdefault(tf, {"long": [], "short": []})[side].append(rec)
    return out


def count_signals() -> int:
    return _conn().execute("SELECT COUNT(*) FROM signals").fetchone()[0]


def query_signals(
    ticker: Optional[str] = None,
    tf: Optional[str] = None,
    side: Optional[str] = None,
    track: Optional[str] = None,
    exit_type: Optional[str] = None,
    since: Optional[str] = None,
    order_by: str = "pos",
    limit: Optional[int] = None,
) -> List[Dict]:
    """Filtered read straight from SQL. Every filter is optional and bound as a
    parameter; `order_by` is a key of the whitelist, never SQL text."""
    where: List[str] = []
    params: List[Any] = []
    if ticker is not None:
        where.append("ticker = ?")
        params.append(_check(ticker, _TICKER_RE, "ticker"))
    if tf is not None:
        where.append("tf = ?")
        params.append(_check(tf, _TF_RE, "timeframe"))
    if side is not None:
        where.append("side = ?")
        params.append(_check_side(side))
    if track is not None:
        where.append("track = ?")
        params.append(_check(track, _TRACK_RE, "track"))
    if exit_type is not None:
        where.append("exit_type = ?")
        params.append(_check(exit_type, _EXIT_RE, "exit_type"))
    if since is not None:
        if not isinstance(since, str) or len(since) > 40:
            raise StorageError(f"Invalid since: {since!r}")
        where.append("ts >= ?")
        params.append(since)
    order_sql = _ORDER_BY.get(order_by)
    if order_sql is None:
        raise StorageError(f"Invalid order_by: {order_by!r}")
    sql = "SELECT payload FROM signals"
    if where:
        sql += " WHERE " + " AND ".join(where)  # `where` holds only constant fragments above
    sql += " ORDER BY " + order_sql              # constant from the whitelist
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    out: List[Dict] = []
    for (payload,) in _conn().execute(sql, params):
        try:
            out.append(json.loads(payload))
        except ValueError:
            continue
    return out


def clear_history() -> None:
    """Deletes every signal record (the `!reset yes` path)."""
    with _tx() as conn:
        conn.execute("DELETE FROM signals")


def has_history() -> bool:
    return count_signals() > 0


# ── named JSON documents (open-position snapshot) ─────────────────────
def kv_save(name: str, value: Any) -> None:
    _check(name, _KV_NAME_RE, "document name")
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
    now = datetime.now(timezone.utc).isoformat()
    with _tx() as conn:
        conn.execute(
            "INSERT INTO kv (name, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (name, payload, now),
        )


def kv_load(name: str, default: Any = None) -> Any:
    _check(name, _KV_NAME_RE, "document name")
    row = _conn().execute("SELECT value FROM kv WHERE name = ?", (name,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        logger.error(f"[STORAGE] document {name!r} is unreadable - using default")
        return default


def kv_exists(name: str) -> bool:
    _check(name, _KV_NAME_RE, "document name")
    return _conn().execute("SELECT 1 FROM kv WHERE name = ?", (name,)).fetchone() is not None


# ── one-time import from the old JSON files ───────────────────────────
def _count_records(history: Dict) -> int:
    n = 0
    for tfs in history.values():
        if isinstance(tfs, dict):
            for sides in tfs.values():
                if isinstance(sides, dict):
                    for side in SIDES:
                        n += sum(1 for r in (sides.get(side) or []) if isinstance(r, dict))
    return n


def _retire(path: str) -> None:
    """Renames an imported JSON file (never deletes it) so it is not imported twice."""
    target = path + ".migrated"
    i = 1
    while os.path.exists(target):
        i += 1
        target = f"{path}.migrated{i}"
    os.replace(path, target)
    logger.info(f"[STORAGE] {os.path.basename(path)} kept as {os.path.basename(target)}")


def migrate_from_json(history_file: str, state_file: str) -> Dict[str, Any]:
    """Imports signals_history.json / bot_state_snapshot.json into the database the
    first time they are found. Safe to call on every start:
      - the database is only filled when it is still empty (never overwrites),
      - the record count is verified against the file before the file is renamed
        to *.migrated, so a failed import leaves the JSON exactly where it was,
      - the old file is renamed, not deleted - it is your rollback.
    """
    result: Dict[str, Any] = {"history": None, "state": None}

    if os.path.exists(history_file) and not has_history():
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise StorageError(f"{os.path.basename(history_file)} is not an object")
            expected = _count_records(data)
            written = save_history(data)
            if written != expected or count_signals() != expected:
                clear_history()
                raise StorageError(f"record count mismatch: file {expected}, database {count_signals()}")
            _retire(history_file)
            result["history"] = written
            logger.info(f"[STORAGE] imported {written} signal records from JSON")
        except Exception as e:
            logger.error(f"[STORAGE] history import failed, JSON left untouched: {e}", exc_info=True)

    if os.path.exists(state_file) and not kv_exists("bot_state"):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise StorageError(f"{os.path.basename(state_file)} is not an object")
            kv_save("bot_state", data)
            if kv_load("bot_state") != json.loads(json.dumps(data, default=str)):
                raise StorageError("state snapshot did not round-trip")
            _retire(state_file)
            result["state"] = len(data)
            logger.info("[STORAGE] imported the position snapshot from JSON")
        except Exception as e:
            logger.error(f"[STORAGE] snapshot import failed, JSON left untouched: {e}", exc_info=True)

    return result
