"""TTS worker: break script in, WAV path out (SPEC §1.4).

v1 is Piper via subprocess — pre-render always, no streaming TTS anywhere.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .models import now_iso

log = logging.getLogger(__name__)


class TTSError(RuntimeError):
    pass


def render_break(script: str, voice: Path, out_dir: Path) -> Path:
    """Render a break script to WAV. Raises TTSError on any failure —
    callers drop the break and keep the music going (the station never stops).
    """
    if not voice.is_file():
        raise TTSError(f"piper voice model missing: {voice}")
    out_path = out_dir / f"break-{now_iso().replace(':', '')}.wav"
    try:
        proc = subprocess.run(
            ["piper", "--model", str(voice), "--output_file", str(out_path)],
            input=script.encode(),
            capture_output=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise TTSError(f"piper failed to run: {e}") from e
    if proc.returncode != 0 or not out_path.is_file():
        raise TTSError(
            f"piper exited {proc.returncode}: {proc.stderr.decode(errors='replace')[-500:]}"
        )
    return out_path
