"""TTS worker: break script in, WAV path out (SPEC §1.4).

v1 is Piper via subprocess — pre-render always, no streaming TTS anywhere.
"""

from __future__ import annotations

import logging
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from .models import now_iso

log = logging.getLogger(__name__)

# trim engine silence from both edges, then add deliberate breathing room:
# 0.25s lead-in and 0.4s tail so the mixer's cross window eats padding,
# never words
_POLISH_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,"
    "areverse,"
    "adelay=250,apad=pad_dur=0.4"
)


class TTSError(RuntimeError):
    pass


@dataclass
class BreakAudio:
    path: Path
    duration: float  # seconds, of the final polished file


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def render_break(script: str, voice: Path, out_dir: Path) -> BreakAudio:
    """Render a break script to WAV: piper, then an ffmpeg polish pass
    (edge-silence trim + lead/tail pads). Raises TTSError on total failure —
    callers drop the break and keep the music going (the station never stops).
    A failed polish falls back to the raw render.
    """
    if not voice.is_file():
        raise TTSError(f"piper voice model missing: {voice}")
    stamp = now_iso().replace(":", "")
    raw_path = out_dir / f"break-{stamp}.raw.wav"
    out_path = out_dir / f"break-{stamp}.wav"
    try:
        proc = subprocess.run(
            ["piper", "--model", str(voice), "--output_file", str(raw_path)],
            input=script.encode(),
            capture_output=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise TTSError(f"piper failed to run: {e}") from e
    if proc.returncode != 0 or not raw_path.is_file():
        raise TTSError(
            f"piper exited {proc.returncode}: {proc.stderr.decode(errors='replace')[-500:]}"
        )
    try:
        polish = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(raw_path),
             "-af", _POLISH_FILTER, "-c:a", "pcm_s16le", str(out_path)],
            capture_output=True,
            timeout=60,
        )
        if polish.returncode != 0 or not out_path.is_file():
            raise OSError(polish.stderr.decode(errors="replace")[-300:])
        raw_path.unlink(missing_ok=True)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("polish pass failed (%s) — airing raw render", e)
        out_path = raw_path
    try:
        duration = _wav_duration(out_path)
    except (wave.Error, OSError) as e:
        raise TTSError(f"unreadable render: {e}") from e
    log.info("break rendered: %.1fs of speech (%s)", duration, out_path.name)
    return BreakAudio(out_path, duration)
