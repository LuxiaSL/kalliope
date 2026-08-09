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


class TalkSegment(BaseModel):
    """One spoken stretch inside a commissioned program."""

    script: str


class PlannedProgram(BaseModel):
    """A commissioned show: an ordered run of tracks and talk segments."""

    items: list[Track | TalkSegment]
    title: str
    note: str | None = None


# ElevenLabs at conversational pace runs ~14 chars/second; the extra second
# covers the loudnorm head/tail padding. Used only for planning arithmetic.
_TALK_CHARS_PER_S = 14.0


def _talk_estimate_s(script: str) -> float:
    return len(script) / _TALK_CHARS_PER_S + 1.0


# Craft notes shared by both planning modes (a set and a program are the
# same discipline at different lengths).
CRAFT_NOTES = """\
- Sequence for flow. Follow something heavy with somewhere to land. Changes
  of direction are welcome; whiplash every transition is not. Where tool
  results carry bpm, energy, and genres, use them as sequencing instruments —
  they're measured from the audio, so they're the closest thing you have to
  ears. Where they're absent, the analyzer just hasn't gotten there yet.
- genre_search and genre_map are how you move by sound instead of by name:
  map the terrain, then pull from a corner of it. Genre labels come from
  MusicBrainz and inference — treat them as good directions, not gospel.
- Where a track shows intro_s, it opens with that many seconds of quiet
  build before the song proper. A break placed right before such a track
  airs as a talk-over: your voice rides the intro, music ducked underneath,
  released before the vocals land. It's the best trick radio has — set one
  up when the sequence gives you a runway. Spoken sentences run about seven
  seconds each, and the whole break must fit the window with a few seconds
  to spare, so a talk-over wants your shortest writing. Too long for the
  window and it airs standalone instead — no harm, just less craft.
- Work the fresh pool in. New arrivals earn spins; that's how they graduate.
  But a set of all-new is a demo reel, not a show — blend with the library.
- Respect the history you can see. spins and last_aired are in every tool
  result; don't lean on what just aired.
- whats_queued shows what's on air and everything already queued behind it:
  whatever you commit follows the end of that. Plan the seam."""


PLANNING_BRIEF = f"""
## Setting the decks

Beyond the mic, you program the rotation. When asked to plan, use your tools
to look around the catalog — search it, check the fresh pool, pull a random
sample for serendipity — then commit an ordered set with set_the_decks.

Craft, in order of importance:
{CRAFT_NOTES}
- Breaks: at most one per set, placed where a breath belongs. When listeners
  are present, most sets should have one. When nobody's tuned in, usually
  plan no break — though once in a while, talk anyway. Transmitting into the
  dark is part of the job.
- Sets are four to six tracks. Trust the next planning pass for what comes
  after; don't try to program the whole night at once.
"""


PROGRAM_BRIEF = f"""
## Programming a show

You've been commissioned to build a whole program — one shaped stretch of
radio, planned in a single sitting and committed with commit_program. The
commission says roughly how long; every track row carries duration_s, so
keep a running total as you build and land near the target. commit_program
echoes the arithmetic back, so if it says you're short or long, adjust and
resubmit.

Craft, in order of importance:
{CRAFT_NOTES}
- Give it an arc: an opening, movements, a close. A program is a shaped
  thing, not a long set.
- Talk segments are yours to place — as many as the show wants. A voice
  every fifteen to twenty-five minutes feels like company; more is chatter.
  Never two in a row.
- Everything you write airs on tape delay, some of it an hour or more after
  you write it. Nothing that dates: no clock, no "right now", no guesses
  about who's listening. Speak from inside the show — what just played,
  what's coming — that's the only timeline you actually know.
- A talk placed right before a track with intro_s rides that intro as a
  talk-over when it fits the window; keep those the shortest of all.
- The commission is the show's brief — honor its mood and intent over any
  habit.
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
    story: str = "",
    occasion: str | None = None,
    ask: str = "break",
    program_minutes: float | None = None,
) -> str:
    """The rolling station state doc (SPEC §1.3), assembled per call."""
    now = datetime.now()
    lines = [
        f"You're writing this on {now.strftime('%A')} around {now.strftime('%H:%M')}. "
        "It airs minutes from now — tape delay rules apply.",
        f"Tuned in as you write (may well change by airtime): {len(listeners)}"
        + (f" (since: {', '.join(sorted(listeners.values()))})" if listeners else ""),
    ]
    if story:
        lines += [
            "",
            "The story so far — your own memories of this station, oldest first:",
            story,
        ]
    lines += [
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
    ]
    if occasion:
        lines += ["", f"The occasion: {occasion}"]
    # the closing instruction must match the call: a literal-minded model
    # handed "write your next break" will write a break instead of planning
    if ask == "plan":
        lines += ["", "Set the decks: browse with your tools, then commit "
                      "the next set with set_the_decks."]
    elif ask == "program":
        minutes = int(program_minutes or 60)
        lines += ["", f"Build a program of about {minutes} minutes: browse "
                      "with your tools, then commit the whole run — tracks "
                      "and talk segments in air order — with commit_program."]
    else:
        lines += ["", "Write your next break."]
    return "\n".join(lines)


class DJ:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: anthropic.Anthropic | None = None
        self._persona: str | None = None
        # set by the server: records paid calls into the spend ledger.
        # Signature matches RadioDB.record_usage; None = no metering.
        self.usage_hook = None
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

    def _meter(self, kind: str, usage) -> None:
        """Report one API call's tokens to the spend ledger; never raises."""
        if self.usage_hook is None:
            return
        try:
            self.usage_hook(
                kind,
                model=self._settings.dj_model,
                in_tokens=getattr(usage, "input_tokens", 0) or 0,
                out_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            )
        except Exception:
            log.exception("usage metering failed — spend undercounts")

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
        self._meter("break", response.usage)
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

    def write_memory(self, material: str, prior: str = "", kind: str = "chapter") -> str:
        """Cal forms a lasting first-person memory of a stretch of broadcast
        (the chronicle fold — CSPN pattern). Returns "" on any failure so
        the chronicle can retry later; never raises into the caller."""
        if self._client is None or self._persona is None:
            return ""
        label = "stretch of the station's life" if kind == "chapter" else (
            "longer arc of the station's life, told through your own earlier memories"
        )
        prior_block = (
            f"MEMORIES YOU'VE ALREADY FORMED (your own, oldest first):\n{prior}\n\n"
            if prior else ""
        )
        prompt = (
            f"{prior_block}"
            f"You are forming a lasting MEMORY of this {label} — as it happened, "
            "with no knowledge of what came after. First person, past tense. "
            "Keep concrete details worth keeping: what you played and said, who "
            "came and went, anything that felt like it might become a running "
            "thread. Three to five sentences, in your own voice. This is for "
            "your log, not for air — no sign-offs, no station identification.\n\n"
            f"WHAT TO REMEMBER:\n{material}"
        )
        try:
            response = self._client.messages.create(
                model=self._settings.dj_model,
                max_tokens=800,
                system=[
                    {
                        "type": "text",
                        "text": self._persona,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError:
            log.exception("memory-forming call failed — chronicle will retry")
            return ""
        self._meter("memory", response.usage)
        text = "".join(
            b.text for b in response.content if getattr(b, "type", "") == "text"
        ).strip()
        if response.stop_reason == "max_tokens" and "." in text:
            # never store a memory that trails off mid-sentence
            text = text[: text.rfind(".") + 1]
            log.warning("memory hit the token cap — trimmed to last sentence")
        if text:
            log.info("memory formed (%s, %d chars)", kind, len(text))
        return text

    def _browse_tools(self, catalog: Catalog, deck_view=None) -> list:
        """The read-only catalog tools shared by both planning modes.
        deck_view: optional zero-arg callable returning a JSON-able snapshot
        of what's on air and queued (the server's live deck)."""

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
        def whats_queued() -> str:
            """What's on air right now and everything already queued behind
            it, in play order. Whatever you commit follows the end of this —
            plan the seam."""
            try:
                return json.dumps(deck_view() if deck_view else [])
            except Exception:
                log.exception("deck view failed inside planning tool")
                return "[]"

        tools = [search_catalog, fresh_pool, random_sample,
                 genre_search, genre_map]
        if deck_view is not None:
            tools.append(whats_queued)
        return tools

    def plan_set(
        self, state_doc: str, catalog: Catalog, deck_view=None
    ) -> PlannedSet:
        """Let the DJ browse the catalog with tools and program the next set.
        Raises DJError on any failure — the shuffle fallback covers for it.
        """
        if self._client is None or self._persona is None:
            raise DJError("DJ is not on shift")

        committed: dict[str, PlannedSet] = {}

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
                tools=self._browse_tools(catalog, deck_view) + [set_the_decks],
            )
            usage_in = usage_out = 0
            last_text = ""
            for i, message in enumerate(runner):
                self._meter("plan", message.usage)
                usage_in += message.usage.input_tokens
                usage_out += message.usage.output_tokens
                last_text = "".join(
                    b.text for b in message.content
                    if getattr(b, "type", "") == "text"
                )
                if message.stop_reason == "refusal":
                    raise DJError("model declined to plan")
                if "plan" in committed or i >= 10:
                    break
        except anthropic.APIError as e:
            raise DJError(f"planning call failed: {e}") from e
        plan = committed.get("plan")
        if plan is None:
            # keep what the model said instead — the difference between a
            # mystery and a prompt bug
            raise DJError(
                "planner finished without setting the decks; it said: "
                f"{last_text[:300]!r}"
            )
        log.info(
            "decks set: %d tracks, break %s (%d in / %d out tokens)",
            len(plan.tracks),
            f"after #{plan.break_after}" if plan.break_after is not None else "none",
            usage_in,
            usage_out,
        )
        return plan

    def plan_program(
        self,
        state_doc: str,
        catalog: Catalog,
        target_min: float,
        deck_view=None,
    ) -> PlannedProgram:
        """A commissioned show: one agentic pass builds an arbitrary-length
        run of tracks and talk segments. Raises DJError on any failure —
        the caller keeps its existing deck when it does."""
        if self._client is None or self._persona is None:
            raise DJError("DJ is not on shift")

        target_s = max(10.0, float(target_min)) * 60
        committed: dict[str, PlannedProgram] = {}

        @beta_tool
        def commit_program(items: list[dict], title: str, note: str = "") -> str:
            """Commit the whole program. Call once, when the full run is
            built. Returns the duration arithmetic — if it says the program
            is short or long, adjust and resubmit.

            Args:
                items: The program in air order. Each item is an object with
                    exactly one key: {"track": <catalog id>} for a song, or
                    {"talk": "<script>"} for a spoken segment written for
                    the ear (same craft rules as breaks).
                title: A short name for the program — for the log, not air.
                note: Optional private note for the session log.
            """
            if len(items) > 100:
                return "error: over 100 items — trim the program"
            parsed: list[tuple[str, object]] = []
            for i, raw in enumerate(items):
                if not isinstance(raw, dict) or len(raw) != 1:
                    return (f"error: item {i} must be an object with exactly "
                            'one key — {"track": id} or {"talk": "script"}')
                (key, value), = raw.items()
                if key == "track":
                    try:
                        parsed.append(("track", int(value)))  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        return f"error: item {i}: track must be a catalog id"
                elif key == "talk":
                    if not isinstance(value, str) or not value.strip():
                        return f"error: item {i}: talk needs a script"
                    parsed.append(("talk", value.strip()))
                else:
                    return f"error: item {i}: unknown key {key!r}"
            track_ids = [v for k, v in parsed if k == "track"]
            if len(track_ids) < 4:
                return "error: a program needs at least four tracks"
            if len(set(track_ids)) != len(track_ids):
                return "error: a program doesn't repeat a track"
            for (k1, _), (k2, _) in zip(parsed, parsed[1:]):
                if k1 == k2 == "talk":
                    return "error: never two talk segments in a row"
            tracks = {t.id: t for t in catalog.by_ids(track_ids)}  # type: ignore[arg-type]
            missing = [t for t in track_ids if t not in tracks]
            if missing:
                return (f"error: unknown track ids {sorted(missing)} — "  # type: ignore[type-var]
                        "check and resubmit")
            if not title.strip():
                return "error: give the program a title"
            total_s, untagged = 0.0, 0
            for kind, value in parsed:
                if kind == "talk":
                    total_s += _talk_estimate_s(str(value))
                elif tracks[value].duration:
                    total_s += float(tracks[value].duration or 0)
                else:
                    total_s += 240.0   # untagged length: call it four minutes
                    untagged += 1
            if not 0.75 * target_s <= total_s <= 1.3 * target_s:
                gap_min = (target_s - total_s) / 60
                fix = (f"add about {gap_min:.0f} more minutes" if gap_min > 0
                       else f"trim about {-gap_min:.0f} minutes")
                return (f"error: the program runs ~{total_s / 60:.0f} minutes "
                        f"against a target of ~{target_s / 60:.0f} — {fix} "
                        "and resubmit")
            committed["program"] = PlannedProgram(
                items=[tracks[v] if k == "track"
                       else TalkSegment(script=str(v))
                       for k, v in parsed],
                title=title.strip(),
                note=note.strip() or None,
            )
            talks = sum(1 for k, _ in parsed if k == "talk")
            report = (f"program committed: ~{total_s / 60:.0f} minutes, "
                      f"{len(track_ids)} tracks, {talks} talk segments")
            if untagged:
                report += (f" ({untagged} tracks had no tagged length, "
                           "counted at four minutes each)")
            return report

        try:
            runner = self._client.beta.messages.tool_runner(
                model=self._settings.dj_model,
                max_tokens=8000,
                system=[
                    {"type": "text", "text": self._persona},
                    {
                        "type": "text",
                        "text": PROGRAM_BRIEF,
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
                messages=[{"role": "user", "content": state_doc}],
                tools=self._browse_tools(catalog, deck_view) + [commit_program],
            )
            usage_in = usage_out = 0
            last_text = ""
            for i, message in enumerate(runner):
                self._meter("program", message.usage)
                usage_in += message.usage.input_tokens
                usage_out += message.usage.output_tokens
                last_text = "".join(
                    b.text for b in message.content
                    if getattr(b, "type", "") == "text"
                )
                if message.stop_reason == "refusal":
                    raise DJError("model declined to program")
                if "program" in committed or i >= 20:
                    break
        except anthropic.APIError as e:
            raise DJError(f"program call failed: {e}") from e
        program = committed.get("program")
        if program is None:
            raise DJError(
                "programmer finished without committing; it said: "
                f"{last_text[:300]!r}"
            )
        log.info(
            "program committed: %r — %d items (%d in / %d out tokens)",
            program.title, len(program.items), usage_in, usage_out,
        )
        return program
