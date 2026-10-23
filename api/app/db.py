"""Хранилище StoreSense на SQLite.

Храним только обезличенные события (время, филиал, зона, числа).
Ни видео, ни кадров, ни лиц здесь нет и не будет.
"""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("DB_PATH", "/data/storesense.db")
_lock = threading.Lock()

EVENT_TYPES = {
    "entry",        # человек вошёл в магазин
    "exit",         # человек вышел
    "zone_visit",   # посещение зоны: zone, value = секунд в зоне
    "consult",      # покупатель в зоне: zone, value = ожидание продавца (сек), data.consulted, data.duration
    "shelf",        # состояние полки: zone, data.shelf, data.status = full|low|empty
    "heartbeat",    # камера жива
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    branch  TEXT    NOT NULL,
    camera  TEXT,
    type    TEXT    NOT NULL,
    zone    TEXT,
    value   REAL,
    data    TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_branch_type_ts ON events(branch, type, ts);

CREATE TABLE IF NOT EXISTS cameras (
    branch    TEXT NOT NULL,
    camera    TEXT NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (branch, camera)
);

CREATE TABLE IF NOT EXISTS shelves (
    branch  TEXT NOT NULL,
    shelf   TEXT NOT NULL,
    zone    TEXT,
    status  TEXT NOT NULL,
    since   REAL NOT NULL,
    PRIMARY KEY (branch, shelf)
);

CREATE TABLE IF NOT EXISTS reports (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    branch  TEXT,
    kind    TEXT NOT NULL,      -- daily | alert
    text    TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init() -> None:
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)


def insert_events(events: list[dict]) -> int:
    """Принимает пачку событий. Невалидные молча пропускает, возвращает число сохранённых."""
    rows, beats, shelves = [], [], []
    for e in events:
        try:
            etype = str(e["type"])
            branch = str(e["branch"])
            ts = float(e.get("ts") or time.time())
        except (KeyError, TypeError, ValueError):
            continue
        if etype not in EVENT_TYPES:
            continue
        camera = e.get("camera")
        zone = e.get("zone")
        data = e.get("data") or {}
        value = e.get("value")
        if camera:
            beats.append((branch, camera, ts))
        if etype == "heartbeat":
            continue  # хартбиты не пишем в журнал, только обновляем last_seen
        if etype == "shelf":
            status = data.get("status")
            shelf = data.get("shelf")
            if status in ("full", "low", "empty") and shelf:
                shelves.append((branch, shelf, zone, status, ts))
        rows.append((ts, branch, camera, etype, zone,
                     float(value) if value is not None else None,
                     json.dumps(data, ensure_ascii=False) if data else None))

    with _lock, connect() as conn:
        if rows:
            conn.executemany(
                "INSERT INTO events(ts, branch, camera, type, zone, value, data) VALUES (?,?,?,?,?,?,?)",
                rows)
        for b, c, ts in beats:
            conn.execute(
                "INSERT INTO cameras(branch, camera, last_seen) VALUES (?,?,?) "
                "ON CONFLICT(branch, camera) DO UPDATE SET last_seen = MAX(last_seen, excluded.last_seen)",
                (b, c, ts))
        for b, s, z, st, ts in shelves:
            # since меняется только когда статус действительно поменялся
            conn.execute(
                "INSERT INTO shelves(branch, shelf, zone, status, since) VALUES (?,?,?,?,?) "
                "ON CONFLICT(branch, shelf) DO UPDATE SET "
                "  since = CASE WHEN shelves.status = excluded.status THEN shelves.since ELSE excluded.since END, "
                "  status = excluded.status, zone = excluded.zone",
                (b, s, z, st, ts))
    return len(rows) + sum(1 for e in events if e.get("type") == "heartbeat")


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def add_report(branch: str | None, kind: str, text: str) -> None:
    with _lock, connect() as conn:
        conn.execute("INSERT INTO reports(ts, branch, kind, text) VALUES (?,?,?,?)",
                     (time.time(), branch, kind, text))


def cleanup(keep_days: int = 400) -> None:
    """Старые события удаляем, чтобы база не росла бесконечно."""
    with _lock, connect() as conn:
        conn.execute("DELETE FROM events WHERE ts < ?", (time.time() - keep_days * 86400,))
