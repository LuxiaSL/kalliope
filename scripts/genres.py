#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic>=0.60", "pydantic>=2"]
# ///
"""genres.py — backfill kalliope's genres table, artist by artist.

The library's genre tags are empty and Spotify's genre metadata is dead for
new apps, so genres come from two live sources, in order of trust:

  1. MusicBrainz (source='musicbrainz') — free, no key, community-tagged.
     Rate-limited to ~1 req/s per their policy.
  2. Claude inference (source='inferred') — for artists MusicBrainz doesn't
     know or hasn't tagged. Needs ANTHROPIC_API_KEY; skipped without one.

Rows land keyed by content hash (durable identity — survives pool→library
promotion), lowercase, up to three genres per artist per source. Incremental:
artists whose tracks already carry genre rows are skipped (--redo forces).

Usage:
    uv run scripts/genres.py                # MusicBrainz, then infer the rest
    uv run scripts/genres.py --no-infer     # MusicBrainz only
    uv run scripts/genres.py --limit 10     # first 10 artists (testing)
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

MB_ROOT = "https://musicbrainz.org/ws/2"
UA = "kalliope/0.1 (+https://github.com/LuxiaSL/kalliope)"
GENRES_PER_ARTIST = 3
INFER_BATCH = 40
INFER_MODEL = "claude-sonnet-5"

SCHEMA = """
CREATE TABLE IF NOT EXISTS genres (
  track_hash TEXT NOT NULL,
  genre TEXT NOT NULL,
  source TEXT NOT NULL CHECK
    (source IN ('tag','spotify','musicbrainz','manual','inferred')),
  PRIMARY KEY (track_hash, genre, source)
);
"""


def mb_get(path: str, params: dict) -> dict:
    params = {**params, "fmt": "json"}
    url = f"{MB_ROOT}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def mb_genres(artist: str) -> list[str]:
    """Search MusicBrainz for the artist, return their top genre tags.
    Empty list = unknown artist or untagged. Raises on network trouble."""
    found = mb_get("artist/", {"query": f'artist:"{artist}"', "limit": 3})
    candidates = found.get("artists") or []
    match = next((a for a in candidates if int(a.get("score", 0)) >= 90), None)
    if match is None:
        return []
    time.sleep(1.1)  # MB rate policy: ~1 req/s
    detail = mb_get(f"artist/{match['id']}", {"inc": "genres"})
    tagged = sorted(
        (g for g in detail.get("genres") or [] if g.get("count", 0) > 0),
        key=lambda g: -g["count"],
    )
    return [g["name"].strip().lower() for g in tagged[:GENRES_PER_ARTIST]]


def infer_genres(artists: list[str]) -> dict[str, list[str]]:
    """Ask Claude for genres on artists MusicBrainz couldn't help with.
    Returns {} without an API key rather than failing the run."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("no ANTHROPIC_API_KEY — skipping inference pass")
        return {}
    import anthropic
    from pydantic import BaseModel, Field

    class ArtistGenres(BaseModel):
        artist: str
        genres: list[str] = Field(
            description="One to three lowercase genre labels, or empty if "
            "you genuinely don't know this artist. Never guess from the name."
        )

    class Answers(BaseModel):
        answers: list[ArtistGenres]

    client = anthropic.Anthropic(api_key=key)
    out: dict[str, list[str]] = {}
    for i in range(0, len(artists), INFER_BATCH):
        batch = artists[i : i + INFER_BATCH]
        try:
            resp = client.messages.parse(
                model=INFER_MODEL,
                max_tokens=4000,
                messages=[{
                    "role": "user",
                    "content": (
                        "Label each musical artist with their genres. "
                        "Lowercase, specific over broad (say 'screamo' not "
                        "'rock' when you know), empty list when unsure:\n"
                        + "\n".join(f"- {a}" for a in batch)
                    ),
                }],
                output_format=Answers,
            )
            parsed = resp.parsed_output
        except anthropic.APIError as e:
            print(f"inference batch failed ({e}) — continuing")
            continue
        if parsed is None:
            continue
        wanted = {a.lower(): a for a in batch}
        for row in parsed.answers:
            original = wanted.get(row.artist.strip().lower())
            if original:
                out[original] = [
                    g.strip().lower() for g in row.genres if g.strip()
                ][:GENRES_PER_ARTIST]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get(
        "CATALOG_DB", str(Path.home() / ".local/state/freshpool/catalog.db")))
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--no-infer", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after N artists")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 10000")
    # same migration the server does: an empty v0 table (keyed by track_id)
    # gets rebuilt keyed by hash; a non-empty one is left for hand-migration
    old = db.execute("SELECT sql FROM sqlite_master WHERE name='genres'").fetchone()
    if old and "track_hash" not in old["sql"]:
        (n,) = db.execute("SELECT COUNT(*) FROM genres").fetchone()
        if n:
            sys.exit(f"genres table has the old track_id schema with {n} rows — "
                     "migrate by hand before running this")
        with db:
            db.execute("DROP TABLE genres")
    db.executescript(SCHEMA)

    # artist -> hashes; multi-artist tags are '/'-joined, every segment counts
    by_artist: dict[str, set[str]] = {}
    for row in db.execute(
        "SELECT DISTINCT artist, hash FROM tracks "
        "WHERE artist IS NOT NULL AND hash IS NOT NULL"
    ):
        for name in (s.strip() for s in row["artist"].split("/")):
            if name:
                by_artist.setdefault(name, set()).add(row["hash"])

    covered = {r["track_hash"] for r in db.execute("SELECT track_hash FROM genres")}
    todo = sorted(
        a for a, hashes in by_artist.items()
        if args.redo or not hashes <= covered
    )
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(by_artist)} artists, {len(todo)} to look up")

    def write(artist: str, genres: list[str], source: str) -> int:
        n = 0
        with db:
            for h in by_artist[artist]:
                for g in genres:
                    cur = db.execute(
                        "INSERT OR IGNORE INTO genres (track_hash, genre, source) "
                        "VALUES (?,?,?)", (h, g, source),
                    )
                    n += cur.rowcount
        return n

    unknown: list[str] = []
    for i, artist in enumerate(todo, 1):
        try:
            genres = mb_genres(artist)
        except Exception as e:  # network hiccup: save for the inference pass
            print(f"[{i}/{len(todo)}] {artist}: MB lookup failed ({e})", flush=True)
            unknown.append(artist)
            time.sleep(1.1)
            continue
        if genres:
            write(artist, genres, "musicbrainz")
            print(f"[{i}/{len(todo)}] {artist}: {', '.join(genres)}", flush=True)
        else:
            unknown.append(artist)
            print(f"[{i}/{len(todo)}] {artist}: not on MusicBrainz", flush=True)
        time.sleep(1.1)

    if unknown and not args.no_infer:
        print(f"\ninferring {len(unknown)} artists MusicBrainz missed…")
        for artist, genres in infer_genres(unknown).items():
            if genres:
                write(artist, genres, "inferred")
                print(f"  {artist}: {', '.join(genres)} (inferred)")

    (n_rows,) = db.execute("SELECT COUNT(*) FROM genres").fetchone()
    (n_hashes,) = db.execute(
        "SELECT COUNT(DISTINCT track_hash) FROM genres"
    ).fetchone()
    (n_tracks,) = db.execute(
        "SELECT COUNT(DISTINCT hash) FROM tracks WHERE hash IS NOT NULL"
    ).fetchone()
    print(f"\ngenres table: {n_rows} rows covering {n_hashes}/{n_tracks} tracks "
          f"({datetime.now().isoformat(timespec='seconds')})")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
