"""SQLite persistence for the add-on: households, readings, hunts, reports.

Hunts survive process restarts and span Alexa+ sessions: the engine is a pure function of
(readings, reports), so the store is the only state that matters.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Iterable, List, Optional

from huntkit.core import Reading

SCHEMA = """
CREATE TABLE IF NOT EXISTS household(
  id TEXT PRIMARY KEY, name TEXT, topology TEXT NOT NULL, tz_min INTEGER NOT NULL DEFAULT 330,
  source TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reading(
  hid TEXT NOT NULL, boot_id TEXT NOT NULL, sample_id TEXT NOT NULL, uptime_ms INTEGER NOT NULL,
  t REAL NOT NULL, total REAL NOT NULL, valve TEXT NOT NULL, leak INTEGER NOT NULL,
  PRIMARY KEY(hid, boot_id, sample_id));
CREATE INDEX IF NOT EXISTS reading_t ON reading(hid, t);
CREATE TABLE IF NOT EXISTS hunt(
  id TEXT PRIMARY KEY, hid TEXT NOT NULL, started REAL NOT NULL, topology TEXT NOT NULL,
  ended REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS oauth_pending(
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, explicit INTEGER NOT NULL, state TEXT,
  challenge TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT, created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS oauth_code(
  code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, scopes TEXT NOT NULL, expires REAL NOT NULL, challenge TEXT NOT NULL,
  redirect_uri TEXT NOT NULL, explicit INTEGER NOT NULL, resource TEXT, subject TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_token(
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL, scopes TEXT NOT NULL, expires REAL NOT NULL,
  resource TEXT, subject TEXT NOT NULL, family TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS report(
  hunt_id TEXT NOT NULL, seq INTEGER NOT NULL, t REAL NOT NULL, kind TEXT NOT NULL, node TEXT,
  PRIMARY KEY(hunt_id, seq));
CREATE TABLE IF NOT EXISTS actuation(
  id INTEGER PRIMARY KEY AUTOINCREMENT, hid TEXT NOT NULL, hunt_id TEXT, t REAL NOT NULL, pipe TEXT NOT NULL,
  want TEXT NOT NULL, source TEXT NOT NULL, ok INTEGER NOT NULL, detail TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS actuation_pipe ON actuation(hid, pipe, t);
CREATE TABLE IF NOT EXISTS latch(
  hid TEXT NOT NULL, pipe TEXT NOT NULL, since REAL NOT NULL, hunt_id TEXT, lpm REAL,
  PRIMARY KEY(hid, pipe));
CREATE TABLE IF NOT EXISTS fill(
  id TEXT PRIMARY KEY, hid TEXT NOT NULL, outlet TEXT NOT NULL, target_l REAL NOT NULL, started REAL NOT NULL,
  ended REAL, outcome TEXT, state TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS offer(
  hid TEXT NOT NULL, pipe TEXT NOT NULL, since REAL NOT NULL, expires REAL NOT NULL, hunt_id TEXT, lpm REAL,
  PRIMARY KEY(hid, pipe));
CREATE TABLE IF NOT EXISTS house_mode(
  hid TEXT PRIMARY KEY, mode TEXT NOT NULL, since REAL NOT NULL, state TEXT NOT NULL DEFAULT '{}');
"""

# columns added after the first release; existing databases are upgraded in place
MIGRATIONS = [
    ("hunt", "mode", "TEXT NOT NULL DEFAULT 'guided'"),
    ("hunt", "consent_t", "REAL"),
    ("report", "src", "TEXT NOT NULL DEFAULT 'user'"),
]


class Store:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            for table, col, decl in MIGRATIONS:
                cols = [r["name"] for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()]
                if col not in cols:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self._db.commit()

    # ---------------------------------------------------------------- generic helpers (OAuth tables)
    def execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur.rowcount

    def fetch(self, sql: str, params: tuple = ()) -> List[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # ---------------------------------------------------------------- households
    def put_household(self, hid: str, name: str, topology: list, tz_min: int, source: dict, created: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO household(id,name,topology,tz_min,source,created) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, topology=excluded.topology, "
                "tz_min=excluded.tz_min, source=excluded.source",
                (hid, name, json.dumps(topology), tz_min, json.dumps(source), created))
            self._db.commit()

    def household(self, hid: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM household WHERE id=?", (hid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["topology"] = json.loads(d["topology"])
        d["source"] = json.loads(d["source"])
        return d

    # ---------------------------------------------------------------- readings
    def add_readings(self, hid: str, rs: Iterable[Reading]) -> int:
        n = 0
        with self._lock:
            for r in rs:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO reading(hid,boot_id,sample_id,uptime_ms,t,total,valve,leak) VALUES(?,?,?,?,?,?,?,?)",
                    (hid, r.boot_id, r.sample_id, r.uptime_ms, r.t, r.out_total_l, r.valve, int(r.leak)))
                n += cur.rowcount
            self._db.commit()
        return n

    def readings(self, hid: str, since: float = 0.0, until: float = 1e18) -> List[Reading]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reading WHERE hid=? AND t>=? AND t<=? ORDER BY t, uptime_ms", (hid, since, until)).fetchall()
        return [Reading(r["sample_id"], r["boot_id"], r["uptime_ms"], r["t"], r["total"], r["valve"], bool(r["leak"]))
                for r in rows]

    def last_reading_t(self, hid: str) -> Optional[float]:
        with self._lock:
            r = self._db.execute("SELECT MAX(t) m FROM reading WHERE hid=?", (hid,)).fetchone()
        return r["m"] if r and r["m"] is not None else None

    # ---------------------------------------------------------------- hunts
    def create_hunt(self, hunt_id: str, hid: str, started: float, topology: list, mode: str = "guided",
                    consent_t: Optional[float] = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO hunt(id,hid,started,topology,mode,consent_t) VALUES(?,?,?,?,?,?)",
                             (hunt_id, hid, started, json.dumps(topology), mode, consent_t))
            self._db.commit()

    def hunt(self, hunt_id: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM hunt WHERE id=?", (hunt_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["topology"] = json.loads(d["topology"])
        return d

    def latest_hunt(self, hid: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT id FROM hunt WHERE hid=? ORDER BY started DESC LIMIT 1", (hid,)).fetchone()
        return self.hunt(r["id"]) if r else None

    def end_hunt(self, hunt_id: str, t: float, outcome: str) -> bool:
        """True only for the caller that actually ended it (so clean-up runs exactly once)."""
        with self._lock:
            cur = self._db.execute("UPDATE hunt SET ended=?, outcome=? WHERE id=? AND ended IS NULL", (t, outcome, hunt_id))
            self._db.commit()
            return cur.rowcount > 0

    def open_hunts(self, mode: Optional[str] = None) -> List[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id, hid, mode FROM hunt WHERE ended IS NULL" + (" AND mode=?" if mode else ""),
                                    (mode,) if mode else ()).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- reports
    def add_report(self, hunt_id: str, t: float, kind: str, node: Optional[str], src: str = "user") -> int:
        with self._lock:
            r = self._db.execute("SELECT COALESCE(MAX(seq)+1,0) n FROM report WHERE hunt_id=?", (hunt_id,)).fetchone()
            seq = r["n"]
            self._db.execute("INSERT INTO report(hunt_id,seq,t,kind,node,src) VALUES(?,?,?,?,?,?)",
                             (hunt_id, seq, t, kind, node, src))
            self._db.commit()
        return seq

    def reports(self, hunt_id: str) -> List[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM report WHERE hunt_id=? ORDER BY seq", (hunt_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- actuation audit log and latches
    def add_actuation(self, hid: str, hunt_id: Optional[str], t: float, pipe: str, want: str, source: str, ok: bool,
                      detail: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT INTO actuation(hid,hunt_id,t,pipe,want,source,ok,detail) VALUES(?,?,?,?,?,?,?,?)",
                             (hid, hunt_id, t, pipe, want, source, int(ok), detail))
            self._db.commit()

    def actuations(self, hid: str, hunt_id: Optional[str] = None, limit: int = 500) -> List[dict]:
        with self._lock:
            if hunt_id:
                rows = self._db.execute("SELECT * FROM actuation WHERE hid=? AND hunt_id=? ORDER BY id LIMIT ?",
                                        (hid, hunt_id, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM actuation WHERE hid=? ORDER BY id DESC LIMIT ?", (hid, limit)).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            r["ok"] = bool(r["ok"])
        return out

    def last_actuation_t(self, hid: str, pipe: str) -> Optional[float]:
        with self._lock:
            r = self._db.execute("SELECT MAX(t) m FROM actuation WHERE hid=? AND pipe=? AND ok=1", (hid, pipe)).fetchone()
        return r["m"] if r and r["m"] is not None else None

    def count_actuations(self, hunt_id: str) -> int:
        with self._lock:
            r = self._db.execute("SELECT COUNT(*) n FROM actuation WHERE hunt_id=? AND source='hunt'", (hunt_id,)).fetchone()
        return int(r["n"])

    def set_latch(self, hid: str, pipe: str, since: float, hunt_id: Optional[str], lpm: Optional[float]) -> None:
        with self._lock:
            self._db.execute("INSERT INTO latch(hid,pipe,since,hunt_id,lpm) VALUES(?,?,?,?,?) "
                             "ON CONFLICT(hid,pipe) DO UPDATE SET since=excluded.since, hunt_id=excluded.hunt_id, lpm=excluded.lpm",
                             (hid, pipe, since, hunt_id, lpm))
            self._db.commit()

    def clear_latch(self, hid: str, pipe: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM latch WHERE hid=? AND pipe=?", (hid, pipe))
            self._db.commit()
            return cur.rowcount > 0

    def latches(self, hid: str) -> List[dict]:
        """Pipes the customer agreed to keep closed (a latch exists only after an explicit yes)."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM latch WHERE hid=? ORDER BY since", (hid,)).fetchall()
        return [dict(r) for r in rows]

    # An isolation OFFER is the open question "keep this pipe closed until it is checked?". While it is open the pipe is held
    # on a lease (the hub reopens it by itself); it becomes a latch only if the customer says yes.
    def set_offer(self, hid: str, pipe: str, since: float, expires: float, hunt_id: Optional[str], lpm: Optional[float]) -> None:
        with self._lock:
            self._db.execute("INSERT INTO offer(hid,pipe,since,expires,hunt_id,lpm) VALUES(?,?,?,?,?,?) "
                             "ON CONFLICT(hid,pipe) DO UPDATE SET since=excluded.since, expires=excluded.expires, "
                             "hunt_id=excluded.hunt_id, lpm=excluded.lpm", (hid, pipe, since, expires, hunt_id, lpm))
            self._db.commit()

    def clear_offer(self, hid: str, pipe: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM offer WHERE hid=? AND pipe=?", (hid, pipe))
            self._db.commit()
            return cur.rowcount > 0

    def clear_offers(self, hid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM offer WHERE hid=?", (hid,))
            self._db.commit()

    def offers(self, hid: str, now: float) -> List[dict]:
        """Open offers. An unanswered one lapses with the lease, so it is dropped here."""
        with self._lock:
            self._db.execute("DELETE FROM offer WHERE hid=? AND expires<=?", (hid, now))
            self._db.commit()
            rows = self._db.execute("SELECT * FROM offer WHERE hid=? ORDER BY since", (hid,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- fills ("fill the bath, 120 litres")
    # ---------------------------------------------------------------- house modes (addon/modes.py)
    def set_house_mode(self, hid: str, mode: str, since: float, state: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO house_mode(hid,mode,since,state) VALUES(?,?,?,?)", (hid, mode, since, json.dumps(state)))
            self._db.commit()

    def house_mode(self, hid: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM house_mode WHERE hid=?", (hid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["state"] = json.loads(d["state"])
        return d

    def clear_house_mode(self, hid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM house_mode WHERE hid=?", (hid,))
            self._db.commit()

    def create_fill(self, fill_id: str, hid: str, outlet: str, target_l: float, started: float, state: dict) -> None:
        with self._lock:
            self._db.execute("INSERT INTO fill(id,hid,outlet,target_l,started,state) VALUES(?,?,?,?,?,?)",
                             (fill_id, hid, outlet, target_l, started, json.dumps(state)))
            self._db.commit()

    def fill(self, fill_id: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM fill WHERE id=?", (fill_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["state"] = json.loads(d["state"])
        return d

    def latest_fill(self, hid: str) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT id FROM fill WHERE hid=? ORDER BY started DESC, rowid DESC LIMIT 1", (hid,)).fetchone()
        return self.fill(r["id"]) if r else None

    def open_fills(self) -> List[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id FROM fill WHERE ended IS NULL").fetchall()
        return [self.fill(r["id"]) for r in rows]

    def save_fill_state(self, fill_id: str, state: dict) -> None:
        with self._lock:
            self._db.execute("UPDATE fill SET state=? WHERE id=?", (json.dumps(state), fill_id))
            self._db.commit()

    def end_fill(self, fill_id: str, t: float, outcome: str, state: dict) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE fill SET ended=?, outcome=?, state=? WHERE id=? AND ended IS NULL",
                                   (t, outcome, json.dumps(state), fill_id))
            self._db.commit()
            return cur.rowcount > 0
