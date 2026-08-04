#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mutagen"]
# ///
"""scan.py — build a kalliope-compatible catalog from a music directory.

For people running kalliope without a separate acquisition stack: point this
at your music, get a catalog.db the station can read.

Usage:
    uv run scripts/scan.py ~/Music/library
    uv run scripts/scan.py ~/Music/library --root library --db ~/.local/state/kalliope/catalog.db

Re-run any time; it's incremental (only re-reads new/changed files) and
prunes rows whose files vanished. The schema matches the catalog contract
in docs/catalog.md.
"""
import argparse
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

import mutagen

EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".opus"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY,
  root TEXT NOT NULL,
  path TEXT NOT NULL,
  hash TEXT,
  size INTEGER, mtime REAL,
  title TEXT, artist TEXT, album TEXT, album_artist TEXT,
  track_no INTEGER, disc_no INTEGER, year INTEGER,
  duration REAL, genre TEXT,
  first_seen TEXT, scanned_at TEXT,
  bpm REAL, energy REAL, intro_len REAL,
  outro_type TEXT, loudness_lufs REAL,
  UNIQUE(root, path)
);
CREATE TABLE IF NOT EXISTS roots (root TEXT PRIMARY KEY, base TEXT NOT NULL);
"""


def sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def num(v: object) -> int | None:
    try:
        return int(str(v).split("/")[0])
    except (TypeError, ValueError):
        return None


def tags(p: Path) -> dict[str, object]:
    a = mutagen.File(p, easy=True)
    if a is None or a.tags is None:
        return {"duration": round(a.info.length, 2) if a and a.info else None}
    get = lambda k: (a.get(k) or [None])[0]  # noqa: E731
    date = get("date") or ""
    return {
        "title": get("title"), "artist": get("artist"),
        "album": get("album"), "album_artist": get("albumartist"),
        "track_no": num(get("tracknumber")), "disc_no": num(get("discnumber")),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "duration": round(a.info.length, 2) if a.info else None,
        "genre": get("genre"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("music_dir", type=Path)
    ap.add_argument("--root", default="library", help="root name (default: library)")
    ap.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".local/state/kalliope/catalog.db",
        help="catalog path (point CATALOG_DB here)",
    )
    args = ap.parse_args()
    base = args.music_dir.expanduser().resolve()
    if not base.is_dir():
        raise SystemExit(f"not a directory: {base}")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    now = datetime.now().isoformat(timespec="seconds")
    db.execute("INSERT OR REPLACE INTO roots VALUES (?, ?)", (args.root, str(base)))

    stats = dict(new=0, updated=0, removed=0, same=0)
    on_disk = set()
    for p in base.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        rel = str(p.relative_to(base))
        on_disk.add(rel)
        st = p.stat()
        row = db.execute(
            "SELECT id, size, mtime FROM tracks WHERE root=? AND path=?",
            (args.root, rel),
        ).fetchone()
        if row and row[1] == st.st_size and row[2] == st.st_mtime:
            stats["same"] += 1
            continue
        rec = dict(root=args.root, path=rel, hash=sha1(p), size=st.st_size,
                   mtime=st.st_mtime, scanned_at=now, **tags(p))
        if row:
            rec["id"] = row[0]
            db.execute(
                f"UPDATE tracks SET {','.join(f'{k}=:{k}' for k in rec)} WHERE id=:id",
                rec,
            )
            stats["updated"] += 1
        else:
            rec["first_seen"] = now
            db.execute(
                f"INSERT INTO tracks ({','.join(rec)}) "
                f"VALUES ({','.join(':' + k for k in rec)})",
                rec,
            )
            stats["new"] += 1
    for tid, path in db.execute(
        "SELECT id, path FROM tracks WHERE root=?", (args.root,)
    ).fetchall():
        if path not in on_disk:
            db.execute("DELETE FROM tracks WHERE id=?", (tid,))
            stats["removed"] += 1
    db.commit()
    print(f"{args.root}: " + ", ".join(f"{v} {k}" for k, v in stats.items()))
    print(f"catalog at {args.db} — set CATALOG_DB={args.db}")


if __name__ == "__main__":
    main()
