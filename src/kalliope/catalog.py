"""Read-only access to the freshpool catalog (docs/catalog.md).

One long-lived connection, ``query_only`` so the ownership rule is mechanical
rather than disciplinary. Kalliope never writes freshpool's tables.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .models import Track

log = logging.getLogger(__name__)

_TRACK_COLS = (
    "t.id, t.root, t.path, t.hash, t.title, t.artist, t.album, "
    "t.album_artist, t.year, t.duration, t.genre, t.first_seen, "
    "r.base || '/' || t.path AS abs_path"
)


def _row_to_track(row: sqlite3.Row) -> Track:
    return Track(
        id=row["id"],
        root=row["root"],
        path=row["path"],
        abs_path=Path(row["abs_path"]),
        hash=row["hash"],
        title=row["title"],
        artist=row["artist"],
        album=row["album"],
        album_artist=row["album_artist"],
        year=row["year"],
        duration=row["duration"],
        genre=row["genre"],
        first_seen=row["first_seen"],
    )


class Catalog:
    def __init__(self, db_path: Path) -> None:
        if not db_path.exists():
            raise FileNotFoundError(
                f"catalog DB not found at {db_path} — is freshpool set up? "
                "(set CATALOG_DB if it lives elsewhere)"
            )
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA query_only = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")

    def close(self) -> None:
        self._conn.close()

    def track_count(self) -> int:
        (n,) = self._conn.execute("SELECT COUNT(*) FROM tracks").fetchone()
        return int(n)

    def counts_by_root(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT root, COUNT(*) AS n FROM tracks GROUP BY root"
        ).fetchall()
        return {row["root"]: int(row["n"]) for row in rows}

    # --- DJ-facing browse queries (read plays too: reads don't violate the
    # ownership rule, and airplay context is what makes browsing musical) ----

    _BROWSE_COLS = (
        "t.id, t.root, t.artist, t.title, t.album, t.year, t.duration, "
        "COUNT(p.id) AS spins, MAX(p.aired_at) AS last_aired"
    )
    _BROWSE_TAIL = "GROUP BY t.id"

    @staticmethod
    def _row_to_browse(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "artist": row["artist"],
            "title": row["title"],
            "album": row["album"],
            "year": row["year"],
            "duration_s": round(row["duration"]) if row["duration"] else None,
            "bin": "fresh pool" if row["root"] == "pool" else "library",
            "spins": row["spins"],
            "last_aired": row["last_aired"],
        }

    def search(self, query: str, limit: int = 25) -> list[dict[str, object]]:
        like = f"%{query}%"
        rows = self._conn.execute(
            f"SELECT {self._BROWSE_COLS} FROM tracks t "
            "LEFT JOIN plays p ON p.track_hash = t.hash "
            "WHERE t.artist LIKE ? OR t.title LIKE ? OR t.album LIKE ? "
            f"{self._BROWSE_TAIL} ORDER BY t.artist, t.album, t.track_no LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
        return [self._row_to_browse(r) for r in rows]

    def fresh(self, limit: int = 25) -> list[dict[str, object]]:
        rows = self._conn.execute(
            f"SELECT {self._BROWSE_COLS} FROM tracks t "
            "LEFT JOIN plays p ON p.track_hash = t.hash "
            "WHERE t.root = 'pool' "
            f"{self._BROWSE_TAIL} HAVING spins = 0 "
            "ORDER BY t.first_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_browse(r) for r in rows]

    def sample(self, limit: int = 25) -> list[dict[str, object]]:
        rows = self._conn.execute(
            f"SELECT {self._BROWSE_COLS} FROM tracks t "
            "LEFT JOIN plays p ON p.track_hash = t.hash "
            f"{self._BROWSE_TAIL} ORDER BY RANDOM() LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_browse(r) for r in rows]

    def by_ids(self, ids: list[int]) -> list[Track]:
        """Resolve ids to full tracks, preserving the requested order.
        Unknown ids are dropped (rows vanish when files do)."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT {_TRACK_COLS} FROM tracks t JOIN roots r USING (root) "
            f"WHERE t.id IN ({marks})",
            ids,
        ).fetchall()
        by_id = {row["id"]: _row_to_track(row) for row in rows}
        return [by_id[i] for i in ids if i in by_id]

    def eligible_tracks(self, exclude_hashes: set[str]) -> list[Track]:
        """All tracks whose hash is not in the exclusion set.

        Hashless tracks are always eligible (they can't be tracked in plays
        anyway). Exclusion is applied in Python: the catalog is small and the
        set comes from mixed sources (recent plays + in-flight queue).
        """
        rows = self._conn.execute(
            f"SELECT {_TRACK_COLS} FROM tracks t JOIN roots r USING (root)"
        ).fetchall()
        return [
            _row_to_track(row)
            for row in rows
            if row["hash"] is None or row["hash"] not in exclude_hashes
        ]

    def by_abs_path(self, abs_path: str) -> Track | None:
        row = self._conn.execute(
            f"SELECT {_TRACK_COLS} FROM tracks t JOIN roots r USING (root) "
            "WHERE r.base || '/' || t.path = ?",
            (abs_path,),
        ).fetchone()
        return _row_to_track(row) if row else None
