#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["librosa>=0.10", "numpy", "pyloudnorm"]
# ///
"""analyze.py — backfill kalliope's track_analysis table (SPEC §1.1).

Decodes each track once (ffmpeg), then computes:
  bpm            beat-tracked tempo
  energy         relative onset-density, 0..1 (compare within the library)
  intro_len      seconds of quiet at track start — RMS-based v1 proxy for
                 "no-vocal intro"; good enough for talk-over placement
  outro_type     'fade' if the last stretch decays >10dB, else 'cold'
  loudness_lufs  integrated loudness (BS.1770 via pyloudnorm)

Keyed by content hash so results survive pool→library promotion. Incremental:
tracks already analyzed at this ANALYZER_VERSION are skipped (--redo forces).
Everything downstream is NULL-safe forever, so partial runs are fine.

Usage:
    uv run scripts/analyze.py                 # everything new
    uv run scripts/analyze.py --limit 20      # a taste
    uv run scripts/analyze.py --redo          # re-analyze all
"""
import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import librosa
import numpy as np
import pyloudnorm

ANALYZER_VERSION = "v1"  # bump when metrics change; old rows get redone
SR = 22050
HOP = 512

SCHEMA = """
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


def decode(path: str) -> np.ndarray:
    """One ffmpeg pass: any codec -> mono float32 at SR. Raises on failure."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR),
         "-f", "f32le", "-"],
        capture_output=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-200:])
    y = np.frombuffer(proc.stdout, dtype=np.float32)
    if len(y) < SR:  # under a second of audio is not a song
        raise RuntimeError("decoded to almost nothing")
    return y


def analyze(y: np.ndarray) -> dict:
    duration = len(y) / SR
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    active = rms[rms > 1e-5]
    median_rms = float(np.median(active)) if len(active) else 0.0

    tempo, _ = librosa.beat.beat_track(y=y, sr=SR, hop_length=HOP)
    bpm = float(np.atleast_1d(tempo)[0]) or None

    onset = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP)
    energy = float(1.0 - np.exp(-np.mean(onset) / 2.0))

    # intro: first moment RMS holds above half the track's median for ~1s
    frames_per_s = SR / HOP
    intro_len = 0.0
    if median_rms > 0:
        hold = int(frames_per_s)
        above = rms >= 0.5 * median_rms
        for i in range(len(above) - hold):
            if above[i : i + hold].all():
                intro_len = round(i / frames_per_s, 1)
                break
        intro_len = min(intro_len, round(duration / 2, 1), 60.0)

    # outro: does the last stretch decay like a studio fade?
    outro_type = "cold"
    win = int(12 * frames_per_s)
    if len(rms) > win and median_rms > 0:
        tail_db = 20 * np.log10(rms[-win:] + 1e-9)
        head = float(np.median(tail_db[: int(3 * frames_per_s)]))
        end = float(np.mean(tail_db[-int(frames_per_s) :]))
        if head - end > 10:
            outro_type = "fade"

    try:
        lufs = pyloudnorm.Meter(SR).integrated_loudness(y.astype(np.float64))
        lufs = round(float(lufs), 1) if np.isfinite(lufs) else None
    except ValueError:
        lufs = None

    return {
        "bpm": round(bpm, 1) if bpm else None,
        "energy": round(energy, 3),
        "intro_len": intro_len,
        "outro_type": outro_type,
        "loudness_lufs": lufs,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get(
        "CATALOG_DB", str(Path.home() / ".local/state/freshpool/catalog.db")))
    ap.add_argument("--redo", action="store_true", help="re-analyze everything")
    ap.add_argument("--limit", type=int, default=0, help="stop after N tracks")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 10000")
    db.executescript(SCHEMA)

    done = {
        r["track_hash"]
        for r in db.execute(
            "SELECT track_hash FROM track_analysis WHERE analyzer_version = ?",
            (ANALYZER_VERSION,),
        )
    } if not args.redo else set()

    rows = db.execute(
        "SELECT t.hash, t.artist, t.title, r.base || '/' || t.path AS abs_path "
        "FROM tracks t JOIN roots r USING (root) WHERE t.hash IS NOT NULL "
        "GROUP BY t.hash"
    ).fetchall()
    todo = [r for r in rows if r["hash"] not in done]
    skipped = len(rows) - len(todo)
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(rows)} tracks, {skipped} already analyzed, {len(todo)} to do")

    ok = failed = 0
    for i, row in enumerate(todo, 1):
        label = f"{row['artist'] or '?'} — {row['title'] or Path(row['abs_path']).stem}"
        try:
            result = analyze(decode(row["abs_path"]))
        except Exception as e:  # one bad file must not stop the batch
            print(f"[{i}/{len(todo)}] FAIL {label}: {e}", flush=True)
            failed += 1
            continue
        db.execute(
            "INSERT OR REPLACE INTO track_analysis "
            "(track_hash, bpm, energy, intro_len, outro_type, loudness_lufs, "
            " analyzed_at, analyzer_version) VALUES (?,?,?,?,?,?,?,?)",
            (row["hash"], result["bpm"], result["energy"], result["intro_len"],
             result["outro_type"], result["loudness_lufs"],
             datetime.now().isoformat(timespec="seconds"), ANALYZER_VERSION),
        )
        db.commit()  # per-track: a crashed run keeps its progress
        ok += 1
        print(f"[{i}/{len(todo)}] {label}: "
              f"{result['bpm'] or '?'} bpm, energy {result['energy']}, "
              f"intro {result['intro_len']}s, {result['outro_type']} outro, "
              f"{result['loudness_lufs']} LUFS", flush=True)

    print(f"\ndone: {ok} analyzed, {failed} failed")
    db.close()
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
