#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""levelwatch.py — watch the stream's loudness, window by window.

Tunes into the mp3 stream, decodes via ffmpeg, and prints one line per
rolling window: RMS level, delta against the same song's opening level,
and what /now says is playing. A steady climb within one song is the
signature of gain-rider pumping (the thing that made quiet intros swell).

Usage:
    uv run scripts/levelwatch.py                          # local station
    uv run scripts/levelwatch.py --url https://radio.example/stream
    uv run scripts/levelwatch.py --window 5 --alert 3
"""
import argparse
import array
import json
import math
import subprocess
import sys
import time
import urllib.request

def now_playing(api: str) -> str:
    try:
        with urllib.request.urlopen(f"{api}/now", timeout=2) as r:
            d = json.load(r)
        t = d.get("track") or {}
        if t:
            artist = (t.get("artist") or "?").split("/")[0]
            return f"{artist} — {t.get('title') or '?'}"
        return "(break)" if d.get("source_context") == "break" else "(unknown)"
    except Exception:
        return "(api unreachable)"

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8322/stream")
    ap.add_argument("--api", default="http://127.0.0.1:8321")
    ap.add_argument("--window", type=float, default=5.0, help="seconds per reading")
    ap.add_argument("--alert", type=float, default=3.0,
                    help="flag when a song climbs this many dB over its opening")
    args = ap.parse_args()

    sr = 22050
    chunk = int(sr * args.window)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", args.url,
         "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    song, baseline, windows = None, None, 0
    print(f"{'time':8s}  {'rms':>7s}  {'Δsong':>6s}  playing", flush=True)
    try:
        while True:
            raw = proc.stdout.read(chunk * 4)
            if len(raw) < chunk * 4:
                print("stream ended", file=sys.stderr)
                return 1
            samples = array.array("f", raw)
            rms = math.sqrt(sum(x * x for x in samples) / len(samples))
            db = 20 * math.log10(rms) if rms > 0 else -96.0
            current = now_playing(args.api)
            if current != song:
                song, baseline, windows = current, None, 0
            windows += 1
            if windows == 3:
                # baseline from the third window: the first two catch the
                # crossfade, and a song's fade-in is not its level
                baseline = db
            delta = db - baseline if baseline is not None else 0.0
            flag = "  ← climbing" if delta >= args.alert else ""
            print(f"{time.strftime('%H:%M:%S')}  {db:6.1f}dB  {delta:+5.1f}  "
                  f"{current}{flag}", flush=True)
    except KeyboardInterrupt:
        return 0
    finally:
        proc.terminate()

if __name__ == "__main__":
    sys.exit(main())
