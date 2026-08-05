"""TTS worker: break script in, WAV path out (SPEC §1.4).

Two backends: ElevenLabs over HTTP when a key is present (the good voice),
Piper via subprocess as the always-local fallback. Pre-render always, no
streaming TTS anywhere. Any backend failure falls through to the next;
only when every backend fails does the caller lose the break (and the
station keeps playing music regardless).
"""

from __future__ import annotations

import logging
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .models import now_iso

log = logging.getLogger(__name__)

# trim engine silence from both edges, level to a broadcast-consistent
# -16 LUFS (engines differ wildly; talk-overs need a predictable voice
# above the ducked bed), then add deliberate breathing room: 0.25s lead-in
# and 0.4s tail so the mixer's cross window eats padding, never words
_POLISH_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,"
    "areverse,"
    "loudnorm=I=-16:TP=-1.5:LRA=11,"
    "adelay=250,apad=pad_dur=0.4"
)

_ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"


class TTSError(RuntimeError):
    pass


@dataclass
class BreakAudio:
    path: Path
    duration: float  # seconds, of the final polished file
    backend: str = "piper"


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _polish(raw: Path, out: Path, *, filtered: bool = True) -> None:
    """ffmpeg pass: decode whatever the engine produced to PCM WAV, trimming
    edge silence and padding when ``filtered``. Raises TTSError on failure."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(raw)]
    if filtered:
        # loudnorm upsamples internally; pin the rate back down
        cmd += ["-af", _POLISH_FILTER, "-ar", "44100"]
    cmd += ["-c:a", "pcm_s16le", str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise TTSError(f"ffmpeg failed to run: {e}") from e
    if proc.returncode != 0 or not out.is_file():
        raise TTSError(
            f"ffmpeg exited {proc.returncode}: "
            f"{proc.stderr.decode(errors='replace')[-300:]}"
        )


def _render_piper(script: str, settings: Settings, raw_path: Path) -> None:
    voice = settings.piper_voice
    if not voice.is_file():
        raise TTSError(f"piper voice model missing: {voice}")
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
            f"piper exited {proc.returncode}: "
            f"{proc.stderr.decode(errors='replace')[-500:]}"
        )


def _render_elevenlabs(script: str, settings: Settings, raw_path: Path) -> None:
    """One POST, mp3 back. (PCM/WAV output is Pro-gated; mp3 isn't, and the
    polish pass decodes to WAV anyway.)"""
    if not settings.elevenlabs_api_key:
        raise TTSError("no ELEVENLABS_API_KEY")
    try:
        resp = httpx.post(
            _ELEVENLABS_URL.format(voice=settings.elevenlabs_voice),
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": settings.elevenlabs_api_key},
            json={
                "text": script,
                "model_id": settings.elevenlabs_model,
                # slightly under-speed reads as late-night; style knobs are
                # deliberately not exposed — the script's punctuation is the
                # prosody surface (SPEC §1.4)
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "speed": 0.95,
                },
            },
            timeout=60,
        )
    except httpx.HTTPError as e:
        raise TTSError(f"elevenlabs request failed: {e}") from e
    if resp.status_code != 200:
        raise TTSError(
            f"elevenlabs HTTP {resp.status_code}: {resp.text[:300]}"
        )
    if not resp.content:
        raise TTSError("elevenlabs returned empty audio")
    raw_path.write_bytes(resp.content)


def _backend_order(settings: Settings) -> list[str]:
    if settings.tts_backend == "piper":
        return ["piper"]
    if settings.tts_backend == "elevenlabs" or settings.elevenlabs_api_key:
        return ["elevenlabs", "piper"]
    return ["piper"]


def render_break(script: str, settings: Settings) -> BreakAudio:
    """Render a break script to polished WAV, trying backends in order
    (ElevenLabs when configured, Piper as fallback). Raises TTSError only
    when every backend fails — callers drop the break and keep the music
    going (the station never stops).
    """
    stamp = now_iso().replace(":", "")
    out_dir = settings.breaks_dir
    last_error: TTSError | None = None
    for backend in _backend_order(settings):
        suffix = "mp3" if backend == "elevenlabs" else "wav"
        raw_path = out_dir / f"break-{stamp}.raw.{suffix}"
        out_path = out_dir / f"break-{stamp}.wav"
        try:
            if backend == "elevenlabs":
                _render_elevenlabs(script, settings, raw_path)
            else:
                _render_piper(script, settings, raw_path)
        except TTSError as e:
            log.warning("%s render failed (%s) — trying next backend", backend, e)
            last_error = e
            continue
        try:
            _polish(raw_path, out_path)
            raw_path.unlink(missing_ok=True)
        except TTSError as e:
            log.warning("polish pass failed (%s) — trying bare decode", e)
            try:
                # no filter, just get playable PCM out of the raw render
                _polish(raw_path, out_path, filtered=False)
                raw_path.unlink(missing_ok=True)
            except TTSError as e2:
                if suffix == "wav" and raw_path.is_file():
                    out_path = raw_path  # piper's raw WAV is airable as-is
                else:
                    log.warning("%s render unusable (%s)", backend, e2)
                    last_error = e2
                    continue
        try:
            duration = _wav_duration(out_path)
        except (wave.Error, OSError) as e:
            log.warning("%s produced unreadable audio (%s)", backend, e)
            last_error = TTSError(f"unreadable render: {e}")
            continue
        log.info(
            "break rendered via %s: %.1fs of speech (%s)",
            backend, duration, out_path.name,
        )
        return BreakAudio(out_path, duration, backend)
    raise TTSError(f"all TTS backends failed (last: {last_error})")
