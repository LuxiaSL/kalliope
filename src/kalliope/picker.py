"""Rotation picker: what does the station play next?

v1 is shuffle with a no-repeat window. This module is the seam where the DJ
brain later plugs in — the DJ will enqueue explicitly and the picker becomes
the dead-air fallback (SPEC §1.2: the station never stops).
"""

from __future__ import annotations

import logging
import random
from collections import deque
from collections.abc import Iterable
from datetime import datetime, timedelta

from .catalog import Catalog
from .models import Track
from .radio_db import RadioDB

log = logging.getLogger(__name__)


class Picker:
    def __init__(
        self, catalog: Catalog, radio_db: RadioDB, no_repeat_hours: float
    ) -> None:
        self._catalog = catalog
        self._radio_db = radio_db
        self._no_repeat = timedelta(hours=no_repeat_hours)
        # tracks handed out or deck-planned but possibly not yet aired —
        # keeps the prefetch queue and the fallback from doubling up
        self._in_flight: deque[str] = deque(maxlen=20)

    def mark_planned(self, hashes: Iterable[str]) -> None:
        """DJ-planned tracks join the in-flight window so the shuffle
        fallback doesn't hand out something the deck is about to play."""
        self._in_flight.extend(hashes)

    def next_track(self) -> Track | None:
        cutoff = (datetime.now() - self._no_repeat).isoformat(timespec="seconds")
        exclude = self._radio_db.recently_aired_hashes(cutoff)
        exclude.update(self._in_flight)

        candidates = self._playable(self._catalog.eligible_tracks(exclude))
        if not candidates:
            # small library or long window: relax to "anything but in-flight"
            log.info("no-repeat window exhausted the pool; relaxing")
            candidates = self._playable(
                self._catalog.eligible_tracks(set(self._in_flight))
            )
        if not candidates:
            log.error("catalog has no playable tracks")
            return None

        track = random.choice(candidates)
        if track.hash is not None:
            self._in_flight.append(track.hash)
        return track

    @staticmethod
    def _playable(tracks: list[Track]) -> list[Track]:
        """Filesystem is ground truth — a row is a claim, the file is the fact."""
        return [t for t in tracks if t.abs_path.is_file()]
