"""Kalliope-owned tables in the shared catalog DB.

Created at startup with CREATE TABLE IF NOT EXISTS; we never touch
freshpool's tables on this connection either, but this one is writable.
Foreign keys stay OFF by design — dangling plays.track_id after catalog
prunes is valid history (docs/catalog.md, identity section).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .models import Track, now_iso

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
  id INTEGER PRIMARY KEY,
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  track_hash TEXT NOT NULL,
  aired_at TEXT NOT NULL,
  context TEXT
);
CREATE INDEX IF NOT EXISTS idx_plays_hash_time ON plays (track_hash, aired_at);

CREATE TABLE IF NOT EXISTS genres (
  track_hash TEXT NOT NULL,
  genre TEXT NOT NULL,
  source TEXT NOT NULL CHECK
    (source IN ('tag','spotify','musicbrainz','manual','inferred')),
  PRIMARY KEY (track_hash, genre, source)
);

CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
  playlist_id INTEGER NOT NULL REFERENCES playlists(id),
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  position INTEGER,
  PRIMARY KEY (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  type TEXT NOT NULL,          -- 'tune_in','tune_out','break_aired','dj_note','request'
  who TEXT,                    -- listener identity: ip for now, token name later
  data TEXT                    -- JSON payload, event-type specific
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events (at);

CREATE TABLE IF NOT EXISTS track_analysis (
  track_hash TEXT PRIMARY KEY,
  bpm REAL,
  energy REAL,
  intro_len REAL,
  outro_type TEXT CHECK (outro_type IN ('cold','fade')),
  loudness_lufs REAL,
  analyzed_at TEXT NOT NULL,
  analyzer_version TEXT
);
"""


class RadioDB:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._migrate()
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def _migrate(self) -> None:
        """One-shot schema fixes for kalliope-owned tables (never freshpool's).

        genres originally keyed by track_id; hash is the durable identity
        (docs/catalog.md), so the empty v0 table gets rebuilt keyed by hash.
        A non-empty old-shape table is left alone and logged — losing rows is
        worse than living with the old shape.
        """
        try:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'genres'"
            ).fetchone()
            if row is None or "track_hash" in row["sql"]:
                return
            (n,) = self._conn.execute("SELECT COUNT(*) FROM genres").fetchone()
            if n:
                log.warning(
                    "genres table has old track_id schema with %d rows — "
                    "leaving it; migrate by hand if genre lookups misbehave", n
                )
                return
            with self._conn:
                self._conn.execute("DROP TABLE genres")
            log.info("rebuilt empty genres table keyed by track_hash")
        except sqlite3.Error:
            log.exception("genres migration failed — continuing with old shape")

    def close(self) -> None:
        self._conn.close()

    def record_play(self, track: Track, context: str = "rotation") -> None:
        if track.hash is None:
            # plays.track_hash is NOT NULL and hash is our durable identity;
            # a hashless track simply can't enter history.
            log.warning("not recording play for hashless track %s", track.abs_path)
            return
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO plays (track_id, track_hash, aired_at, context) "
                    "VALUES (?, ?, ?, ?)",
                    (track.id, track.hash, now_iso(), context),
                )
        except sqlite3.Error:
            # airplay history is nice-to-have; the station must not die for it
            log.exception("failed to record play for %s", track.abs_path)

    def record_event(
        self, type_: str, who: str | None = None, data: dict | None = None
    ) -> None:
        """Append to the session log — the DJ's long-term memory substrate."""
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO events (at, type, who, data) VALUES (?, ?, ?, ?)",
                    (now_iso(), type_, who, json.dumps(data) if data else None),
                )
        except sqlite3.Error:
            log.exception("failed to record event %s", type_)

    def recent_events(self, limit: int = 50) -> list[dict[str, object]]:
        try:
            rows = self._conn.execute(
                "SELECT at, type, who, data FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error:
            log.exception("failed to read events")
            return []
        return [
            {
                "at": r["at"],
                "type": r["type"],
                "who": r["who"],
                "data": json.loads(r["data"]) if r["data"] else None,
            }
            for r in rows
        ]

    def recent_plays(self, limit: int = 12) -> list[dict[str, object]]:
        """Airplay history with tags, for the DJ's state doc. Pruned tracks
        show NULL tags — valid history either way (docs/catalog.md)."""
        try:
            rows = self._conn.execute(
                "SELECT p.aired_at, p.context, t.artist, t.title, t.album, t.year "
                "FROM plays p LEFT JOIN tracks t ON t.hash = p.track_hash "
                "ORDER BY p.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error:
            log.exception("failed to read recent plays for state doc")
            return []
        return [dict(r) for r in rows]

    def recently_aired_hashes(self, cutoff_iso: str) -> set[str]:
        try:
            rows = self._conn.execute(
                "SELECT DISTINCT track_hash FROM plays WHERE aired_at >= ?",
                (cutoff_iso,),
            ).fetchall()
        except sqlite3.Error:
            log.exception("failed to read recent plays; repeating is possible")
            return set()
        return {row["track_hash"] for row in rows}
