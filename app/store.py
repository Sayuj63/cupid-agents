"""Tiny SQLite store. Rows keep their payloads as JSON so the schema stays flexible."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DB_PATH", os.path.join(ROOT, "data", "app.db"))
_lock = threading.RLock()
_conn = None


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS people (
              id TEXT PRIMARY KEY, name TEXT, linkedin_url TEXT, instagram_url TEXT,
              status TEXT, error TEXT, raw TEXT, profile TEXT, source TEXT, created_at REAL);
            CREATE TABLE IF NOT EXISTS picks (
              a TEXT, b TEXT, score REAL, reason TEXT, PRIMARY KEY (a, b));
            CREATE TABLE IF NOT EXISTS dates (
              id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT, b TEXT, status TEXT, venue TEXT,
              transcript TEXT, verdict_a TEXT, verdict_b TEXT, created_at REAL, UNIQUE(a, b));
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, kind TEXT, data TEXT, ts REAL);
            """
        )
    return _conn


def _loads(v):
    return json.loads(v) if v else None


def person_row(r) -> dict | None:
    if not r:
        return None
    d = dict(r)
    d["raw"] = _loads(d["raw"])
    d["profile"] = _loads(d["profile"])
    return d


def upsert_person(pid: str, **fields):
    with _lock:
        c = db()
        if not c.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
            c.execute("INSERT INTO people(id, created_at, status) VALUES (?,?,?)", (pid, time.time(), "queued"))
        for k, v in fields.items():
            if k in ("raw", "profile") and v is not None:
                v = json.dumps(v, ensure_ascii=False)
            c.execute(f"UPDATE people SET {k}=? WHERE id=?", (v, pid))
        c.commit()


def get_person(pid: str, with_raw: bool = False) -> dict | None:
    cols = "*" if with_raw else "id,name,linkedin_url,instagram_url,status,error,NULL as raw,profile,source,created_at"
    with _lock:
        return person_row(db().execute(f"SELECT {cols} FROM people WHERE id=?", (pid,)).fetchone())


def people(ready_only: bool = True) -> list[dict]:
    q = "SELECT id,name,linkedin_url,instagram_url,status,error,NULL as raw,profile,source,created_at FROM people"
    if ready_only:
        q += " WHERE status='ready'"
    with _lock:
        return [person_row(r) for r in db().execute(q + " ORDER BY created_at").fetchall()]


def set_pick(a, b, score, reason):
    with _lock:
        db().execute("INSERT OR REPLACE INTO picks VALUES (?,?,?,?)", (a, b, float(score), reason))
        db().commit()


def picks() -> dict:
    with _lock:
        rows = db().execute("SELECT * FROM picks").fetchall()
    return {(r["a"], r["b"]): {"score": r["score"], "reason": r["reason"]} for r in rows}


def date_row(r) -> dict | None:
    if not r:
        return None
    d = dict(r)
    for k in ("transcript", "verdict_a", "verdict_b"):
        d[k] = _loads(d[k])
    return d


def create_date(a, b) -> int:
    with _lock:
        c = db()
        a, b = sorted([a, b])
        row = c.execute("SELECT id FROM dates WHERE a=? AND b=?", (a, b)).fetchone()
        if row:
            return row["id"]
        cur = c.execute("INSERT INTO dates(a,b,status,transcript,created_at) VALUES (?,?,?,?,?)", (a, b, "scheduled", "[]", time.time()))
        c.commit()
        return cur.lastrowid


def update_date(did, **fields):
    with _lock:
        c = db()
        for k, v in fields.items():
            if k in ("transcript", "verdict_a", "verdict_b") and v is not None:
                v = json.dumps(v, ensure_ascii=False)
            c.execute(f"UPDATE dates SET {k}=? WHERE id=?", (v, did))
        c.commit()


def get_date(did) -> dict | None:
    with _lock:
        return date_row(db().execute("SELECT * FROM dates WHERE id=?", (did,)).fetchone())


def dates(pid: str | None = None) -> list[dict]:
    with _lock:
        if pid:
            rows = db().execute("SELECT * FROM dates WHERE a=? OR b=? ORDER BY id", (pid, pid)).fetchall()
        else:
            rows = db().execute("SELECT * FROM dates ORDER BY id").fetchall()
    return [date_row(r) for r in rows]


def emit(job: str, kind: str, data):
    with _lock:
        db().execute("INSERT INTO events(job, kind, data, ts) VALUES (?,?,?,?)", (job, kind, json.dumps(data, ensure_ascii=False), time.time()))
        db().commit()


def events(job: str, after: int = 0) -> list[dict]:
    with _lock:
        rows = db().execute("SELECT id, kind, data FROM events WHERE job=? AND id>? ORDER BY id", (job, after)).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "data": json.loads(r["data"])} for r in rows]
