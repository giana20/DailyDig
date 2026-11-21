"""SQLite storage (WAL). One file, short-lived connections, no ORM."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists(
  mbid TEXT PRIMARY KEY, name TEXT, deezer_id TEXT, ytm_browse_id TEXT, image TEXT, popularity_pct REAL
);
-- track_key = recording MBID when known, else "n:<norm artist>|<norm title>"
CREATE TABLE IF NOT EXISTS tracks(
  track_key TEXT PRIMARY KEY, recording_mbid TEXT, title TEXT, artist TEXT, artist_mbid TEXT,
  album TEXT, duration_ms INTEGER, release_group_mbid TEXT, deezer_id TEXT, first_seen REAL
);
CREATE TABLE IF NOT EXISTS play_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, track_key TEXT, recording_mbid TEXT,
  title TEXT, artist TEXT, artist_mbid TEXT, played_at INTEGER, listened_ms INTEGER, duration_ms INTEGER,
  source TEXT, playlist_id INTEGER, outcome TEXT,
  UNIQUE(user, track_key, played_at)
);
CREATE INDEX IF NOT EXISTS ix_play_user_time ON play_events(user, played_at);
CREATE INDEX IF NOT EXISTS ix_play_artist ON play_events(artist_mbid);
CREATE TABLE IF NOT EXISTS candidates(
  track_key TEXT, user TEXT, generator TEXT, seed_ref TEXT, raw_score REAL, features_json TEXT,
  first_seen REAL, last_seen REAL, PRIMARY KEY(track_key, user, generator)
);
CREATE TABLE IF NOT EXISTS scores(
  track_key TEXT, user TEXT, heuristic REAL, ml REAL, final REAL, reason_codes TEXT, tier TEXT,
  scored_at REAL, PRIMARY KEY(track_key, user)
);
CREATE TABLE IF NOT EXISTS seen_ledger(
  track_key TEXT, user TEXT, first_offered REAL, last_offered REAL, times_offered INTEGER DEFAULT 0,
  last_played REAL, cooldown_until REAL, PRIMARY KEY(track_key, user)
);
CREATE TABLE IF NOT EXISTS playlists(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT UNIQUE, name TEXT, config_json TEXT, schedule_cron TEXT,
  enabled INTEGER DEFAULT 1, last_run REAL, next_run REAL
);
CREATE TABLE IF NOT EXISTS playlist_tracks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER, run_id TEXT, track_key TEXT, position INTEGER,
  title TEXT, artist TEXT, artist_mbid TEXT, recording_mbid TEXT, duration_ms INTEGER, generator TEXT,
  reason TEXT, score REAL, status TEXT DEFAULT 'pending', file_path TEXT, error TEXT,
  video_id TEXT, added_at REAL, in_library INTEGER DEFAULT 0,
  features_json TEXT, label INTEGER, labeled_at REAL
);
CREATE INDEX IF NOT EXISTS ix_pt_playlist ON playlist_tracks(playlist_id, status);
CREATE TABLE IF NOT EXISTS charts(
  source TEXT, region TEXT, date TEXT, kind TEXT, entries_json TEXT, PRIMARY KEY(source, region, date, kind)
);
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, target_type TEXT, target_id TEXT, action TEXT,
  context_json TEXT, created_at REAL, expires_at REAL
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, payload TEXT, state TEXT DEFAULT 'queued',
  worker_id TEXT, lease_until REAL, run_at REAL, attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
  result TEXT, error TEXT, created_at REAL, updated_at REAL, dedupe_key TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state, run_at);
CREATE TABLE IF NOT EXISTS job_locks(name TEXT PRIMARY KEY, owner TEXT, expires_at REAL);
CREATE TABLE IF NOT EXISTS rate_slots(name TEXT PRIMARY KEY, next_slot REAL);
CREATE TABLE IF NOT EXISTS kv_cache(key TEXT PRIMARY KEY, value TEXT, fresh_until REAL, stale_until REAL);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS bandit(
  arm TEXT PRIMARY KEY, alpha REAL DEFAULT 1, beta REAL DEFAULT 1, updated_at REAL
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = self._connect()
        try:
            yield c
        finally:
            c.close()

    def init(self) -> None:
        with self.conn() as c:
            c.executescript(SCHEMA)

    # -- helpers ----------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.conn() as c:
            return c.execute(sql, tuple(params)).rowcount

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.conn() as c:
            c.execute("BEGIN")
            c.executemany(sql, [tuple(r) for r in rows])
            c.execute("COMMIT")

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute(sql, tuple(params)).fetchone()

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.conn() as c:
            return c.execute(sql, tuple(params)).lastrowid

    # -- tiny kv ----------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def now() -> float:
    return time.time()
