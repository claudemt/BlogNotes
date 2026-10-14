"""The only authority for publication state and captured historical metrics."""
from __future__ import annotations
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

METRICS = ("impressions", "views", "likes", "favorites", "comments", "shares", "followers",
           "view_time_avg", "danmaku")
# Added to an existing database by _migrate(); kept out of METRICS because it
# holds a rate, not a count, and is written to its own column.
RATE_METRICS = ("click_rate",)

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class Store:
    def __init__(self, file: Path):
        file.parent.mkdir(parents=True, exist_ok=True)
        self.file = file
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notes (
                    uid TEXT PRIMARY KEY, folder TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
                    xhs_id TEXT UNIQUE, submitted_at TEXT, published_at TEXT,
                    published_hash TEXT, last_error TEXT, created_at TEXT NOT NULL,
                    modified_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_uid TEXT NOT NULL REFERENCES notes(uid),
                    captured_at TEXT NOT NULL,
                    impressions INTEGER, views INTEGER, likes INTEGER,
                    favorites INTEGER, comments INTEGER, shares INTEGER,
                    followers INTEGER, click_rate REAL,
                    view_time_avg INTEGER, danmaku INTEGER,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(note_uid, captured_at)
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_note_time ON snapshots(note_uid, captured_at);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            self._migrate(db)

    def _migrate(self, db) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS leaves existing tables alone, so a database
        from an earlier version would silently drop the newer metrics. Adding a
        nullable column is additive and keeps old rows intact.
        """
        have = {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}
        for column in ("view_time_avg", "danmaku"):
            if column not in have:
                db.execute(f"ALTER TABLE snapshots ADD COLUMN {column} INTEGER")

    @contextmanager
    def db(self):
        with self.lock:
            conn = sqlite3.connect(self.file, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def touch_source(self, source):
        with self.db() as db:
            db.execute("""INSERT INTO notes(uid,folder,title,status,created_at,modified_at)
                VALUES(?,?,?,'draft',?,?) ON CONFLICT(folder) DO UPDATE SET
                title=excluded.title,modified_at=excluded.modified_at""",
                (uuid.uuid4().hex, source.slug, source.title, utcnow(), source.modified_at))

    def note(self, uid: str) -> dict | None:
        with self.db() as db:
            result = db.execute("SELECT * FROM notes WHERE uid=?", (uid,)).fetchone()
            return dict(result) if result else None

    def note_folder(self, folder: str) -> dict | None:
        with self.db() as db:
            result = db.execute("SELECT * FROM notes WHERE folder=?", (folder,)).fetchone()
            return dict(result) if result else None

    def records(self) -> list[dict]:
        with self.db() as db:
            return [dict(x) for x in db.execute("SELECT * FROM notes")]

    def modify(self, uid: str, **changes):
        whitelist = {"status", "xhs_id", "submitted_at", "published_at", "published_hash", "last_error"}
        if not changes or not set(changes) <= whitelist:
            raise ValueError("Invalid state change")
        sql = ",".join(f"{key}=?" for key in changes)
        with self.db() as db:
            db.execute(f"UPDATE notes SET {sql} WHERE uid=?", (*changes.values(), uid))

    def snapshot(self, uid: str, payload: dict, captured_at: str | None = None) -> bool:
        at = captured_at or utcnow()
        columns = ("note_uid", "captured_at", *METRICS, *RATE_METRICS, "raw_json")
        values = [uid, at]
        values += [payload.get(m) for m in METRICS]
        values += [payload.get(m) for m in RATE_METRICS]
        values.append(json.dumps(payload, ensure_ascii=False))
        with self.db() as db:
            cur = db.execute(
                f"INSERT OR IGNORE INTO snapshots ({','.join(columns)}) "
                f"VALUES({','.join('?' * len(columns))})", values)
            return cur.rowcount > 0

    def latest(self, uid: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM snapshots WHERE note_uid=? ORDER BY captured_at DESC,id DESC LIMIT 1", (uid,)).fetchone()
            return dict(row) if row else None

    def timeline(self, uid: str, limit: int = 2000) -> list[dict]:
        columns = ",".join(("captured_at", *METRICS, *RATE_METRICS))
        with self.db() as db:
            return [dict(r) for r in db.execute(
                f"SELECT {columns} FROM snapshots "
                f"WHERE note_uid=? ORDER BY captured_at DESC,id DESC LIMIT ?", (uid,limit))][::-1]

    def set_meta(self, key: str, value: str):
        with self.db() as db:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,value))

    def get_meta(self, key: str) -> str | None:
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
