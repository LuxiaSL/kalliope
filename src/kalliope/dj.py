"""The DJ brain (SPEC §1.3): one Claude call per break, structured output.

Persona lives in station/persona.md — the prompt is the craft surface.
Everything here is NULL-safe and failure-safe: a dead DJ never stops the music.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import anthropic
from anthropic import beta_tool
from pydantic import BaseModel, Field

from .catalog import Catalog
from .config import Settings
from .models import Track

log = logging.getLogger(__name__)


class DJError(RuntimeError):
    pass


class PlannedSet(BaseModel):
    """A programmed stretch of radio: ordered tracks, optionally one break."""

    tracks: list[Track]
    break_after: int | None = None   # break airs after tracks[break_after]
    break_script: str | None = None
    note: str | None = None


PLANNING_BRIEF = """
## Setting the decks

Beyond the mic, you program the rotation. When asked to plan, use your tools
to look around the catalog — search it, check the fresh pool, pull a random
sample for serendipity — then commit an ordered set with set_the_decks.

Craft, in order of importance:
- Sequence for flow. Follow something heavy with somewhere to land. Changes
  of direction are welcome; whiplash every transition is not. Where tool
  results carry bpm, energy, and genres, use them as sequencing instruments —
  they're measured from the audio, so they're the closest thing you have to
  ears. Where they're absent, the analyzer just hasn't gotten there yet.
- genre_search and genre_map are how you move by sound instead of by name:
  map the terrain, then pull from a corner of it. Genre labels come from
  MusicBrainz and inference — treat them as good directions, not gospel.
- Work the fresh pool in. New arrivals earn spins; that's how they graduate.
  But a set of all-new is a demo reel, not a show — blend with the library.
- Respect the history you can see. spins and last_aired are in every tool
  result; don't lean on what just aired.
- Breaks: at most one per set, placed where a breath belongs. When listeners
  are present, most sets should have one. When nobody's tuned in, usually
  plan no break — though once in a while, talk anyway. Transmitting into the
  dark is part of the job.
- Sets are four to six tracks. Trust the next planning pass for what comes
  after; don't try to program the whole night at once.
"""



class BreakScript(BaseModel):
    """What the DJ hands back — a script for the TTS voice, plus log color."""

    script: str = Field(
        description=(
            "The break as read on air, written for the ear. Two to four short "
            "sentences, usually. No markdown, no stage directions."
        )
    )
    note: str | None = Field(
        default=None,
        description=(
            "Optional private note for the session log — a running bit to "
            "remember, an observation about the night. Not read on air."
        ),
    )


def build_state_doc(
    *,
    recent_plays: list[dict[str, object]],
    listeners: dict[str, str],
    recent_breaks: list[str],
    upcoming: Track | None,
    catalog_counts: dict[str, int],
) -> str:
    """The rolling station state doc (SPEC §1.3), assembled per call."""
    now = datetime.now()
    lines = [
        f"You're writing this on {now.strftime('%A')} around {now.strftime('%H:%M')}. "
        "It airs minutes from now — tape delay rules apply.",
        f"Tuned in as you write (may well change by airtime): {len(listeners)}"
        + (f" (since: {', '.join(sorted(listeners.values()))})" if listeners else ""),
        "",
        "Recently aired (newest first):",
    ]
    for p in recent_plays or [{}]:
        artist = str(p.get("artist") or "unknown artist").split("/")[0]
        title = p.get("title") or "an untagged track"
        lines.append(f"  {p.get('aired_at', '?')}  {artist} — {title}")
    if upcoming is not None:
        up = f"  {upcoming.display_artist} — {upcoming.display_title}"
        if upcoming.album:
            up += f" (from {upcoming.album}"
            up += f", {upcoming.year})" if upcoming.year else ")"
        lines += ["", "Coming up next, if you want to tip it:", up]
    if recent_breaks:
        lines += ["", "Your recent breaks (don't repeat yourself):"]
        lines += [f'  "{b}"' for b in recent_breaks]
    lines += [
        "",
        f"Library: {catalog_counts.get('library', 0)} tracks settled, "
        f"{catalog_counts.get('pool', 0)} in the fresh pool.",
        "",
        "Write your next break.",
    ]
    return "\n".join(lines)


class DJ:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: anthropic.Anthropic | None = None
        self._persona: str | None = None
        if not settings.dj_enabled:
            log.info("DJ disabled by config")
            return
        if not settings.anthropic_api_key:
            log.warning("no ANTHROPIC_API_KEY — station runs without a DJ")
            return
        try:
            self._persona = settings.persona_path.read_text()
        except OSError:
            log.exception("persona file unreadable — station runs without a DJ")
            return
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    @property
    def on_shift(self) -> bool:
        return self._client is not None

    def write_break(self, state_doc: str) -> BreakScript:
        """One Claude call, structured output. Raises DJError on any failure."""
        if self._client is None or self._persona is None:
            raise DJError("DJ is not on shift")
        try:
            response = self._client.messages.parse(
                model=self._settings.dj_model,
                max_tokens=2000,
                system=[
                    {
                        "type": "text",
                        "text": self._persona,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": state_doc}],
                output_format=BreakScript,
            )
        except anthropic.APIError as e:
            raise DJError(f"API call failed: {e}") from e
        if response.stop_reason == "refusal":
            raise DJError("model declined to write this break")
        parsed = response.parsed_output
        if parsed is None or not parsed.script.strip():
            raise DJError("empty break script")
        log.info(
            "break written (%d in, %d out tokens, cache read %s)",
            response.usage.input_tokens,
            response.usage.output_tokens,
            getattr(response.usage, "cache_read_input_tokens", "?"),
        )
        return parsed

    def plan_set(self, state_doc: str, catalog: Catalog) -> PlannedSet:
        """Let the DJ browse the catalog with tools and program the next set.
        Raises DJError on any failure — the shuffle fallback covers for it.
        """
        if self._client is None or self._persona is None:
            raise DJError("DJ is not on shift")

        committed: dict[str, PlannedSet] = {}

        @beta_tool
        def search_catalog(query: str, limit: int = 25) -> str:
            """Search the music catalog by artist, title, or album substring.

            Args:
                query: Substring matched against artist, title, and album tags.
                limit: Maximum rows to return.
            """
            return json.dumps(catalog.search(query, min(limit, 50)))

        @beta_tool
        def fresh_pool(limit: int = 25) -> str:
            """List the newest never-aired arrivals in the fresh pool.

            Args:
                limit: Maximum rows to return.
            """
            return json.dumps(catalog.fresh(min(limit, 50)))

        @beta_tool
        def random_sample(limit: int = 25) -> str:
            """Pull a random sample from the whole catalog, for serendipity.

            Args:
                limit: Maximum rows to return.
            """
            return json.dumps(catalog.sample(min(limit, 50)))

        @beta_tool
        def genre_search(genre: str, limit: int = 25) -> str:
            """Pull tracks tagged with a genre (substring: 'punk' finds
            post-punk too). Random order, so repeat calls dig deeper.

            Args:
                genre: Genre label or fragment to match.
                limit: Maximum rows to return.
            """
            return json.dumps(catalog.by_genre(genre, min(limit, 50)))

        @beta_tool
        def genre_map() -> str:
            """The library's terrain: every genre on file with its track
            count, biggest first. Good first move when planning by mood."""
            return json.dumps(catalog.genre_map())

        @beta_tool
        def set_the_decks(
            track_ids: list[int],
            break_after: int = -1,
            break_script: str = "",
            note: str = "",
        ) -> str:
            """Commit the next set. Call exactly once, when you've decided.

            Args:
                track_ids: Ordered catalog ids, four to six of them.
                break_after: Index into track_ids after which your break airs
                    (0 = after the first track). -1 for no break this set.
                break_script: The break, written for the ear, if break_after
                    is not -1. Same craft rules as always.
                note: Optional private note for the session log.
            """
            tracks = catalog.by_ids(track_ids)
            if len(tracks) != len(track_ids):
                missing = set(track_ids) - {t.id for t in tracks}
                return f"error: unknown track ids {sorted(missing)} — check and resubmit"
            if not 3 <= len(tracks) <= 8:
                return "error: sets are four to six tracks (three to eight at the outside)"
            if len({t.id for t in tracks}) != len(tracks):
                return "error: a set doesn't repeat a track"
            has_break = break_after >= 0
            if has_break and not (0 <= break_after < len(tracks)):
                return f"error: break_after must be -1 or 0..{len(tracks) - 1}"
            if has_break and not break_script.strip():
                return "error: break_after set but break_script is empty"
            committed["plan"] = PlannedSet(
                tracks=tracks,
                break_after=break_after if has_break else None,
                break_script=break_script.strip() if has_break else None,
                note=note.strip() or None,
            )
            return "decks set"

        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._settings.dj_model,
                max_tokens=4000,
                system=[
                    {"type": "text", "text": self._persona},
                    {
                        "type": "text",
                        "text": PLANNING_BRIEF,
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                messages=[{"role": "user", "content": state_doc}],
                tools=[search_catalog, fresh_pool, random_sample,
                       genre_search, genre_map, set_the_decks],
            )
            usage_in = usage_out = 0
            for i, message in enumerate(runner):
                usage_in += message.usage.input_tokens
                usage_out += message.usage.output_tokens
                if message.stop_reason == "refusal":
                    raise DJError("model declined to plan")
                if "plan" in committed or i >= 10:
                    break
        except anthropic.APIError as e:
            raise DJError(f"planning call failed: {e}") from e
        plan = committed.get("plan")
        if plan is None:
            raise DJError("planner finished without setting the decks")
        log.info(
            "decks set: %d tracks, break %s (%d in / %d out tokens)",
            len(plan.tracks),
            f"after #{plan.break_after}" if plan.break_after is not None else "none",
            usage_in,
            usage_out,
        )
        return plan
