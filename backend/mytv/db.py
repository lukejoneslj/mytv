"""SQLite access. One shared connection guarded by a re-entrant lock."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("mytv")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY,
  number INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  kind TEXT NOT NULL,                       -- movie | episode
  mode TEXT NOT NULL DEFAULT 'shuffle',     -- shuffle | chronological
  queue_target INTEGER NOT NULL,            -- titles kept downloaded ahead
  accent TEXT NOT NULL,
  blocks TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL,
  ref TEXT NOT NULL,
  kind TEXT NOT NULL,                       -- movie | episode | filler
  title TEXT NOT NULL,
  show TEXT,
  season INTEGER,
  episode INTEGER,
  year INTEGER,
  genres TEXT NOT NULL DEFAULT '[]',
  description TEXT NOT NULL DEFAULT '',
  runtime REAL,                             -- seconds, estimate until processed
  category TEXT,
  popularity REAL NOT NULL DEFAULT 0,
  dedupe TEXT NOT NULL UNIQUE,
  has_poster INTEGER NOT NULL DEFAULT 0,
  tmdb_checked INTEGER NOT NULL DEFAULT 0,
  still_tried INTEGER NOT NULL DEFAULT 0,
  enriched INTEGER NOT NULL DEFAULT 0,
  source_url TEXT,
  size INTEGER,
  md5 TEXT,
  license TEXT,
  status TEXT NOT NULL DEFAULT 'discovered',
  -- discovered | downloading | downloaded | processing | ready | failed | hidden
  progress REAL NOT NULL DEFAULT 0,
  bytes_done INTEGER NOT NULL DEFAULT 0,
  speed REAL NOT NULL DEFAULT 0,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_retry REAL NOT NULL DEFAULT 0,
  duration REAL,                            -- exact, set once ready
  nsegs INTEGER,
  disk_bytes INTEGER NOT NULL DEFAULT 0,
  width INTEGER, height INTEGER,
  direct INTEGER,                           -- 1 = video was not re-encoded
  added_at REAL NOT NULL,
  UNIQUE (provider, ref)
);
CREATE INDEX IF NOT EXISTS media_category ON media(category, status);

CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL REFERENCES channels(id),
  media_id INTEGER NOT NULL REFERENCES media(id),
  position REAL NOT NULL,
  added_by TEXT NOT NULL DEFAULT 'auto',
  UNIQUE (channel_id, media_id)
);

CREATE TABLE IF NOT EXISTS airings (
  id INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL REFERENCES channels(id),
  media_id INTEGER NOT NULL REFERENCES media(id),
  start REAL NOT NULL,
  duration REAL NOT NULL,
  seg_offset INTEGER NOT NULL,
  nsegs INTEGER NOT NULL,
  seq_base INTEGER NOT NULL,
  disc_base INTEGER NOT NULL,
  kind TEXT NOT NULL                        -- program | rerun | filler
);
CREATE INDEX IF NOT EXISTS airings_time ON airings(channel_id, start);
CREATE INDEX IF NOT EXISTS airings_media ON airings(media_id, start);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_links (
  id INTEGER PRIMARY KEY, number INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cast_channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT, number INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, accent TEXT NOT NULL
);
"""

DEFAULT_CHANNELS = [
    (1, 1, "Classic Movies", "classic-movies", "movie", 3, "#d9a441"),
    (2, 2, "Comedy Movies", "comedy-movies", "movie", 3, "#ef7650"),
    (3, 3, "Sitcoms", "sitcoms", "episode", 8, "#46b8a6"),
    (4, 4, "Mystery & Thrillers", "mystery-thrillers", "movie", 3, "#8b93f0"),
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "cache_limit_gb": 60,
    "min_free_gb": 15,
    "rerun_reserve_movies": 1,
    "rerun_reserve_episodes": 4,
    "auto_cleanup": True,
    "cleanup_delay_min": 10,
    "max_downloads": 3,
    "plan_ahead_hours": 24,
    "max_height": 1080,
    "min_height": 320,
    "video_policy": "auto",          # auto = copy compatible video, always = re-encode everything
    "normalize_audio": True,
    "after_outage": "resume",        # resume | live
    "include_silent": False,
    "archive_enabled": True,
    "archive_max_year": 1963,
    "archive_max_titles": 500,
    "discovery_hours": 24,
    "tmdb_api_key": "",
    "public_url": "",
}


class DB:
    def __init__(self, path: Path | str):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._depth = 0
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.all("PRAGMA table_info(media)")}
        for col in ("tmdb_checked", "still_tried"):  # added after the first release
            if col not in cols:
                self.run(f"ALTER TABLE media ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        if not self.one("SELECT 1 FROM channels"):
            for c in DEFAULT_CHANNELS:
                self.run(
                    "INSERT INTO channels(id,number,name,category,kind,queue_target,accent) VALUES(?,?,?,?,?,?,?)", *c
                )

    def all(self, sql: str, *args: Any) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, *args: Any) -> dict | None:
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def run(self, sql: str, *args: Any) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    @contextmanager
    def tx(self):
        with self.lock:
            if self._depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("COMMIT")

    # settings -----------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM settings WHERE key=?", key)
        if r:
            return json.loads(r["value"])
        return DEFAULT_SETTINGS.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.run(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            key, json.dumps(value),
        )

    def settings(self) -> dict:
        return {k: self.get(k) for k in DEFAULT_SETTINGS}

    def number_taken(self, number: int, table: str = "", own_id: int = 0) -> bool:
        """Is a channel number in use by anything other than row own_id of table?"""
        return any(self.one(f"SELECT 1 FROM {t} WHERE number=? AND NOT (?=? AND id=?)", number, t, table, own_id)
                   for t in ("channels", "live_links", "cast_channels"))

    # events -------------------------------------------------------------
    def event(self, level: str, source: str, message: str) -> None:
        getattr(log, "warning" if level == "warn" else level, log.info)("%s: %s", source, message)
        last = self.one("SELECT ts FROM events WHERE source=? AND message=? ORDER BY id DESC LIMIT 1", source, message)
        if last and time.time() - last["ts"] < 600:
            return  # do not flood the log with a repeating condition
        self.run("INSERT INTO events(ts,level,source,message) VALUES(?,?,?,?)", time.time(), level, source, message)
        self.run("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 2000")
