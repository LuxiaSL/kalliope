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

-- API spend ledger: one row per paid call (LLM tokens priced at insert
-- time so history survives model/price changes; TTS tracked as characters
-- since ElevenLabs credits->$ depends on the plan).
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,          -- 'plan','break','memory','genre','tts'
  model TEXT,
  in_tokens INTEGER DEFAULT 0,
  out_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0,
  tts_chars INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_at ON usage (at);

-- Cal's long-term memory: the chronicle forest (chapters -> arcs), ported
-- from heimdall's CSPN. Raw material = events; chronicle_meta tracks the
-- fold high-water mark.
CREATE TABLE IF NOT EXISTS chronicle (
  id TEXT PRIMARY KEY,
  level INTEGER NOT NULL,
  content TEXT NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]',
  first_at TEXT,
  last_at TEXT,
  parent_id TEXT,
  tokens INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chronicle_level ON chronicle (level);

CREATE TABLE IF NOT EXISTS chronicle_meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

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

    # --- API spend ledger -------------------------------------------------

    # $ per million tokens (input, output); cache reads bill at 0.1x input,
    # cache writes at 1.25x. Unknown models fall back to opus pricing.
    _PRICES = {
        "claude-fable-5": (10.0, 50.0),
        "claude-opus-5": (5.0, 25.0),
        "claude-opus-4-8": (5.0, 25.0),
        "claude-sonnet-5": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }

    def record_usage(
        self, kind: str, *, model: str | None = None,
        in_tokens: int = 0, out_tokens: int = 0,
        cache_read: int = 0, cache_write: int = 0, tts_chars: int = 0,
    ) -> None:
        p_in, p_out = self._PRICES.get(model or "", (5.0, 25.0))
        cost = (
            in_tokens * p_in
            + cache_read * 0.1 * p_in
            + cache_write * 1.25 * p_in
            + out_tokens * p_out
        ) / 1_000_000 if model else 0.0
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO usage (at, kind, model, in_tokens, out_tokens, "
                    " cache_read_tokens, cache_write_tokens, tts_chars, cost_usd) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (now_iso(), kind, model, in_tokens, out_tokens,
                     cache_read, cache_write, tts_chars, round(cost, 6)),
                )
        except sqlite3.Error:
            log.exception("failed to record usage — spend meter undercounts")

    def spend_summary(self) -> dict[str, dict[str, float]]:
        """Cost + TTS characters for today / 7 days / 30 days / all time."""
        out: dict[str, dict[str, float]] = {}
        from datetime import datetime, timedelta
        now = datetime.now()
        windows = {
            "today": now.strftime("%Y-%m-%dT00:00:00"),
            "week": (now - timedelta(days=7)).isoformat(timespec="seconds"),
            "month": (now - timedelta(days=30)).isoformat(timespec="seconds"),
            "all": "0000",
        }
        try:
            for name, cutoff in windows.items():
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(cost_usd),0) AS usd, "
                    " COALESCE(SUM(tts_chars),0) AS chars, COUNT(*) AS calls "
                    "FROM usage WHERE at >= ?", (cutoff,),
                ).fetchone()
                out[name] = {
                    "usd": round(row["usd"], 2),
                    "tts_chars": int(row["chars"]),
                    "calls": int(row["calls"]),
                }
        except sqlite3.Error:
            log.exception("spend summary failed")
        return out

    # --- chronicle store (used by chronicle.Chronicle) --------------------

    def get_meta(self, key: str) -> str | None:
        try:
            row = self._conn.execute(
                "SELECT v FROM chronicle_meta WHERE k = ?", (key,)
            ).fetchone()
            return row["v"] if row else None
        except sqlite3.Error:
            log.exception("chronicle meta read failed")
            return None

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO chronicle_meta (k, v) VALUES (?, ?)",
                (key, value),
            )

    def events_after(self, event_id: int, limit: int = 200) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT id, at, type, who, data FROM events WHERE id > ? "
            "ORDER BY id ASC LIMIT ?",
            (event_id, limit),
        ).fetchall()

    def plays_between(self, first_at: str, last_at: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT p.aired_at, t.artist, t.title FROM plays p "
            "LEFT JOIN tracks t ON t.hash = p.track_hash "
            "WHERE p.aired_at >= ? AND p.aired_at <= ? ORDER BY p.id",
            (first_at, last_at),
        ).fetchall()

    def chronicle_add(
        self, *, id_: str, level: int, content: str, source_ids: list,
        first_at: str | None, last_at: str | None, tokens: int, created: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO chronicle "
                "(id, level, content, source_ids, first_at, last_at, "
                " parent_id, tokens, created) VALUES (?,?,?,?,?,?,NULL,?,?)",
                (id_, level, content, json.dumps(source_ids),
                 first_at, last_at, tokens, created),
            )

    def chronicle_set_parent(self, child_id: str, parent_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE chronicle SET parent_id = ? WHERE id = ?",
                (parent_id, child_id),
            )

    def chronicle_unmerged(self, level: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM chronicle WHERE level = ? AND parent_id IS NULL "
            "ORDER BY created ASC",
            (level,),
        ).fetchall()

    def chronicle_roots(self, limit: int | None = None) -> list[sqlite3.Row]:
        q = ("SELECT * FROM chronicle WHERE parent_id IS NULL "
             "ORDER BY level DESC, created DESC")
        if limit:
            q += f" LIMIT {int(limit)}"
        return self._conn.execute(q).fetchall()

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
