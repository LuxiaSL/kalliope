"""Typed views of catalog rows and station state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


def now_iso() -> str:
    """Contract timestamp format: ISO-8601 local-naive, seconds precision."""
    return datetime.now().isoformat(timespec="seconds")


class Track(BaseModel):
    """A row from `tracks` with its path resolved through `roots`.

    Every tag column tolerates NULL (Appendix A: tagless files still index).
    """

    id: int
    root: str
    path: str
    abs_path: Path
    hash: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    year: int | None = None
    duration: float | None = None
    genre: str | None = None
    first_seen: str | None = None

    @property
    def display_artist(self) -> str:
        # multi-artist tags are '/'-joined; first segment is primary
        raw = self.artist or self.album_artist or "unknown artist"
        return raw.split("/")[0].strip()

    @property
    def display_title(self) -> str:
        return self.title or self.abs_path.stem


class NowPlaying(BaseModel):
    station: str
    on_air: bool = True
    started_at: str | None = None
    track: Track | None = None
    source_context: str = "rotation"


class AiredEvent(BaseModel):
    """POSTed by liquidsoap's on_track callback."""

    filename: str
    title: str | None = None
    artist: str | None = None


class StationEvent(BaseModel):
    """POSTed by liquidsoap (tune_in/tune_out) and later by clients/DJ."""

    type: str
    who: str | None = None
    data: dict[str, object] | None = None
