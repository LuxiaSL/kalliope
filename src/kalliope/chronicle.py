"""Cal's long-term memory — a chronicle of the station's life (SPEC §1.6).

Ported from heimdall's CSPN chronicle (docs/CSPN.md, bit 2c): the raw
`events` stream folds into first-person "chapters" that Cal writes as
memories — as they happened, with no knowledge of what came after — and
chapters cascade-merge into higher-level "arcs" as they accumulate. The
state doc then carries "the story so far" under a token budget, so months
of broadcast compact into character instead of scrolling away.

Everything here is failure-safe: memory upkeep must never stop the music,
and a failed fold restores its material for the next pass.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass

from .models import now_iso
from .radio_db import RadioDB

log = logging.getLogger(__name__)

# fold cadence: a chapter forms when this many events are pending, or when
# a smaller batch has been waiting a full broadcast day
FOLD_AT_EVENTS = 60
FOLD_AT_HOURS = 24.0
FOLD_MIN_EVENTS = 8
# raw events per fold are capped so a flood (reconnect storms) can't blow
# the prompt; the excess folds next pass
FOLD_CAP = 200
MERGE_N = 6           # unmerged same-level chapters that merge into an arc
MAX_LEVEL = 8


@dataclass
class Chapter:
    id: str
    level: int
    content: str
    first_at: str | None
    last_at: str | None
    tokens: int


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class Chronicle:
    """Reads events, has the DJ write memories, stores the forest.

    Owns the `chronicle` + `chronicle_meta` tables (created by RadioDB).
    The `write_memory` callable is DJ.write_memory — injected so this
    module never imports the API client.
    """

    def __init__(self, radio_db: RadioDB, write_memory) -> None:
        self._db = radio_db
        self._write_memory = write_memory

    # ── raw material ────────────────────────────────────────────────────

    def _last_folded_id(self) -> int:
        row = self._db.get_meta("chronicle_last_event_id")
        return int(row) if row else 0

    def _pending_events(self) -> list[sqlite3.Row]:
        return self._db.events_after(self._last_folded_id(), limit=FOLD_CAP)

    @staticmethod
    def _render_events(rows: list[sqlite3.Row]) -> str:
        """Compact one-line-per-event rendering for the fold prompt."""
        lines: list[str] = []
        for r in rows:
            data = json.loads(r["data"]) if r["data"] else {}
            at = str(r["at"])[:16]  # minute precision reads better
            t = r["type"]
            if t == "break_aired":
                mode = f" ({data['mode']})" if data.get("mode") else ""
                lines.append(f'{at}  you said{mode}: "{data.get("transcript", "")}"')
            elif t == "dj_note":
                lines.append(f"{at}  your note: {data.get('note', '')}")
            elif t in ("tune_in", "tune_out"):
                who = r["who"] or "someone"
                verb = "tuned in" if t == "tune_in" else "tuned out"
                lines.append(f"{at}  {who} {verb}")
            else:
                lines.append(f"{at}  {t}: {json.dumps(data) if data else ''}")
        return "\n".join(lines)

    def _plays_summary(self, first_at: str, last_at: str) -> str:
        """What actually aired across the window — the music is the show."""
        rows = self._db.plays_between(first_at, last_at)
        if not rows:
            return ""
        artists: dict[str, int] = {}
        for r in rows:
            a = (r["artist"] or "unknown").split("/")[0].strip()
            artists[a] = artists.get(a, 0) + 1
        top = sorted(artists.items(), key=lambda kv: -kv[1])[:10]
        return (
            f"{len(rows)} tracks aired in this stretch. Most played: "
            + ", ".join(f"{a} ({n})" if n > 1 else a for a, n in top)
        )

    # ── the forest ──────────────────────────────────────────────────────

    def _row_to_chapter(self, r: sqlite3.Row) -> Chapter:
        return Chapter(
            id=r["id"], level=r["level"], content=r["content"],
            first_at=r["first_at"], last_at=r["last_at"], tokens=r["tokens"],
        )

    def _unmerged_at(self, level: int) -> list[Chapter]:
        rows = self._db.chronicle_unmerged(level)
        return [self._row_to_chapter(r) for r in rows]

    def _prior_memories(self, n: int = 3) -> str:
        roots = self._db.chronicle_roots(limit=n)
        return "\n\n".join(r["content"] for r in reversed(roots))

    def _fold_chapter(self) -> bool:
        """Fold pending events into an L1 chapter. Returns True if one formed."""
        rows = self._pending_events()
        if not rows:
            return False
        first_at, last_at = str(rows[0]["at"]), str(rows[-1]["at"])
        substantive = sum(
            1 for r in rows if r["type"] in ("break_aired", "dj_note", "request")
        )
        if substantive == 0:
            # nothing worth a memory — a canned stub, no API call (CSPN's
            # thin-chunk guard); the door creaking open and shut is not a story
            content = (
                f"(A quiet stretch — {len(rows)} log entries of routine "
                "broadcast, nobody saying much. The station played on.)"
            )
        else:
            material = self._render_events(rows)
            plays = self._plays_summary(first_at, last_at)
            if plays:
                material = f"{plays}\n\n{material}"
            content = self._write_memory(
                material, prior=self._prior_memories(), kind="chapter"
            )
            if not content:
                return False  # fold failed; events stay pending for next pass
        self._db.chronicle_add(
            id_=f"L1-{uuid.uuid4().hex[:6]}", level=1, content=content,
            source_ids=[int(r["id"]) for r in rows],
            first_at=first_at, last_at=last_at,
            tokens=_estimate_tokens(content), created=now_iso(),
        )
        self._db.set_meta("chronicle_last_event_id", str(rows[-1]["id"]))
        log.info("chronicle: folded %d events into a chapter (%s → %s)",
                 len(rows), first_at[:16], last_at[:16])
        return True

    def _merge_level(self, level: int) -> bool:
        children = self._unmerged_at(level)[:MERGE_N]
        if len(children) < MERGE_N:
            return False
        stretch = "\n\n".join(c.content for c in children)
        content = self._write_memory(
            stretch, prior=self._prior_memories(), kind="arc"
        )
        if not content:
            return False
        parent_id = f"L{level + 1}-{uuid.uuid4().hex[:6]}"
        self._db.chronicle_add(
            id_=parent_id, level=level + 1, content=content,
            source_ids=[c.id for c in children],
            first_at=children[0].first_at, last_at=children[-1].last_at,
            tokens=_estimate_tokens(content), created=now_iso(),
        )
        for c in children:
            self._db.chronicle_set_parent(c.id, parent_id)
        log.info("chronicle: merged %d L%d chapters into an arc", len(children), level)
        return True

    def _should_fold(self) -> bool:
        rows = self._pending_events()
        if len(rows) >= FOLD_AT_EVENTS:
            return True
        if len(rows) >= FOLD_MIN_EVENTS:
            oldest = str(rows[0]["at"])
            from datetime import datetime, timedelta
            try:
                age = datetime.now() - datetime.fromisoformat(oldest)
                return age >= timedelta(hours=FOLD_AT_HOURS)
            except ValueError:
                return False
        return False

    def maintain(self) -> None:
        """Fold if due, then cascade merges. Called from planning worker
        threads; every failure is logged and swallowed."""
        try:
            if self._should_fold():
                self._fold_chapter()
            level = 1
            while level <= MAX_LEVEL and self._merge_level(level):
                level += 1
        except Exception:
            log.exception("chronicle maintenance failed — memory waits, music plays")

    def story_so_far(self, budget_tokens: int = 600) -> str:
        """Top-of-forest memories under a token budget, oldest first so it
        reads as a narrative. Empty string until the first chapter forms."""
        try:
            roots = [self._row_to_chapter(r) for r in self._db.chronicle_roots()]
        except Exception:
            log.exception("chronicle read failed")
            return ""
        picked: list[Chapter] = []
        used = 0
        for c in roots:  # highest level first, newest first
            t = c.tokens or _estimate_tokens(c.content)
            if picked and used + t > budget_tokens:
                break
            picked.append(c)
            used += t
        return "\n\n".join(c.content for c in reversed(picked))
