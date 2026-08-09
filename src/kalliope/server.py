"""Kalliope station server.

Speaks to three parties:
  - liquidsoap: GET /next (plain-text path to play), POST /aired (on_track)
  - listeners' players: GET / (web player), GET /now, /hls/* (segments)
  - later: the DJ brain and the request line live here too
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .catalog import Catalog
from .chronicle import Chronicle
from .config import Settings, load_settings
from .dj import DJ, TalkSegment, build_state_doc
from .models import AiredEvent, NowPlaying, StationEvent, Track, now_iso
from .picker import Picker
from .radio_db import RadioDB
from .tts import render_break

log = logging.getLogger("kalliope")


@dataclass
class BreakItem:
    wav: Path
    transcript: str
    duration: float
    mode: str = "standalone"   # standalone | show_open (talkovers never deck)


class Station:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.catalog = Catalog(settings.catalog_db)
        self.radio_db = RadioDB(settings.catalog_db)
        self.picker = Picker(self.catalog, self.radio_db, settings.no_repeat_hours)
        self.dj = DJ(settings)
        self.dj.usage_hook = self.radio_db.record_usage
        self.chronicle = Chronicle(self.radio_db, self.dj.write_memory)
        self.now_playing = NowPlaying(station=settings.station_name)
        # who -> tuned-in-since; keyed by harbor's client ip (tokens later)
        self.listeners: dict[str, str] = {}
        # when the room last emptied (monotonic); a tune-in after a long
        # enough quiet stretch earns a show-open break (SPEC §3)
        self.empty_since: float | None = time.monotonic()
        # power mode: runtime state wins over the configured default
        self.power: str = (
            self.radio_db.get_meta("power_mode") or settings.power_default
        )
        # --- the deck (SPEC §1.3: everything is lookahead) -----------------
        # DJ-planned queue of tracks and breaks; the shuffle picker only
        # plays when the deck runs dry (dead-air fallback, SPEC §1.2)
        self.deck: deque[Track | BreakItem] = deque()
        self.planning = False
        # title of the commissioned program currently on deck, if any
        self.current_program: str | None = None
        self.breaks_out: dict[str, BreakItem] = {}   # wav path -> handed-out break
        self.break_busy = False                       # manual /dj/break guard
        # talk-overs: track abs_path -> break armed to ride its intro,
        # pushed to the mixer's voice queue when that track hits the air
        self.pending_talkover: dict[str, BreakItem] = {}
        # timing diagnostics: what's on air and how long it should run
        self._on_air: tuple[str, float | None, float] | None = None  # label, expected_s, monotonic

    def tune_in(self, who: str) -> bool:
        """Record the arrival. Returns True when it should wake the DJ for
        a show open: first company after a long quiet stretch."""
        was_empty = not self.listeners
        self.listeners[who] = now_iso()
        self.radio_db.record_event("tune_in", who=who)
        log.info("tune in: %s (%d listening)", who, len(self.listeners))
        quiet_s = self.settings.show_open_quiet_min * 60
        due = (
            was_empty
            and quiet_s > 0
            and self.empty_since is not None
            and time.monotonic() - self.empty_since >= quiet_s
            and self.dj.on_shift
            and not self.break_busy
        )
        if was_empty:
            self.empty_since = None
        return due

    def tune_out(self, who: str) -> None:
        # harbor's disconnect id may be 'ip' or 'ip:port'; match loosely
        key = who if who in self.listeners else next(
            (k for k in self.listeners if k.split(":")[0] == who.split(":")[0]),
            None,
        )
        if key is not None:
            del self.listeners[key]
        if not self.listeners:
            self.empty_since = time.monotonic()
        self.radio_db.record_event("tune_out", who=who)
        log.info("tune out: %s (%d listening)", who, len(self.listeners))

    # --- the queue seam: what does liquidsoap play next? -------------------
    # NOTE: liquidsoap prefetches one request ahead, so anything appended to
    # the deck airs at least one track later than "now". Planned sets don't
    # care (they're computed ahead in order); forced breaks land in ~2 tracks.

    def _track_uri(self, track: Track) -> str:
        """A track's playable URI, with one static leveling gain from its
        measured loudness (constant through the song — dynamics survive).
        Unanalyzed tracks play as mastered; the next enrich pass covers them."""
        lufs = self.catalog.loudness(track.hash)
        if lufs is None:
            return str(track.abs_path)
        gain_db = max(-12.0, min(9.0, self.settings.target_lufs - lufs))
        mult = round(10 ** (gain_db / 20), 3)
        return f"annotate:liq_amplify={mult}:{track.abs_path}"

    def next_uri(self) -> str | None:
        uri: str | None = None
        if self.deck:
            item = self.deck.popleft()
            if isinstance(item, BreakItem):
                self.breaks_out[str(item.wav)] = item
                log.info("handing out: [break] %s (%.1fs)", item.wav.name, item.duration)
                # tight fades AND a tiny cross window: the default 3s cross
                # would overlap the last seconds of speech with the next
                # track's intro (and swallow the first under the outgoing one)
                uri = (
                    "annotate:liq_fade_in=0.1,liq_fade_out=0.1,"
                    f"liq_cross_duration=0.3:{item.wav}"
                )
            else:
                log.info("handing out (deck): %s — %s",
                         item.display_artist, item.display_title)
                uri = self._track_uri(item)
        else:
            if self.current_program is not None:
                log.info("program over: %r — shuffle resumes", self.current_program)
                self.current_program = None
            track = self.picker.next_track()
            if track is not None:
                log.info("handing out (shuffle fallback): %s — %s",
                         track.display_artist, track.display_title)
                uri = self._track_uri(track)
        return uri

    def log_transition(self, incoming: str) -> None:
        """Timing diagnostics: how long did the outgoing item actually run
        vs how long its file said it should? Big negative deltas mean the
        mixer is eating content (crossfade overlap, decoder trouble)."""
        now = time.monotonic()
        if self._on_air is not None:
            label, expected, since = self._on_air
            ran = now - since
            if expected:
                log.info("timing: %s ran %.1fs on air (file is %.1fs, delta %+.1fs)",
                         label, ran, expected, ran - expected)
            else:
                log.info("timing: %s ran %.1fs on air (untagged duration)", label, ran)
        brk = self.breaks_out.get(incoming)
        if brk is not None:
            self._on_air = (f"[break] {brk.wav.name}", brk.duration, now)
        else:
            track = self.catalog.by_abs_path(incoming)
            label = (f"{track.display_artist} — {track.display_title}"
                     if track else Path(incoming).name)
            self._on_air = (label, track.duration if track else None, now)

    def set_power(self, mode: str) -> None:
        self.power = mode
        self.radio_db.set_meta("power_mode", mode)
        self.radio_db.record_event("admin", data={"power": mode})
        log.info("power mode: %s", mode)

    def dj_active(self) -> bool:
        """Is Cal on the clock right now? (Money is only spent when yes.)"""
        if not self.dj.on_shift or self.power == "music":
            return False
        if self.power == "auto":
            if self.listeners:
                return True
            linger = self.settings.listener_linger_min * 60
            return (
                self.empty_since is not None
                and time.monotonic() - self.empty_since < linger
            )
        return True

    def deck_running_low(self) -> bool:
        music_left = sum(1 for i in self.deck if not isinstance(i, BreakItem))
        return self.dj_active() and not self.planning and music_left <= 1

    def deck_view(self) -> list[dict[str, object]]:
        """What's on air and queued, for the DJ's whats_queued tool.
        Snapshot semantics — safe to call from planning worker threads."""
        view: list[dict[str, object]] = []
        if self._on_air is not None:
            label, expected, _since = self._on_air
            view.append({"on_air": label, "duration_s": round(expected or 0)})
        for item in list(self.deck):
            if isinstance(item, BreakItem):
                view.append({"queued": "[your own talk break]",
                             "duration_s": round(item.duration)})
            else:
                view.append({
                    "queued": f"{item.display_artist} — {item.display_title}",
                    "duration_s": round(item.duration or 0),
                })
        return view

    def _state_doc(
        self,
        occasion: str | None = None,
        ask: str = "break",
        program_minutes: float | None = None,
    ) -> str:
        recent_breaks = [
            str((e.get("data") or {}).get("transcript", ""))
            for e in self.radio_db.recent_events(limit=30)
            if e["type"] == "break_aired"
        ][:3]
        return build_state_doc(
            recent_plays=self.radio_db.recent_plays(limit=12),
            listeners=self.listeners,
            recent_breaks=[b for b in recent_breaks if b],
            upcoming=None,
            catalog_counts=self.catalog.counts_by_root(),
            story=self.chronicle.story_so_far(),
            occasion=occasion,
            ask=ask,
            program_minutes=program_minutes,
        )

    def plan_more(self) -> None:
        """One DJ planning pass → deck grows by a set. Worker thread; any
        failure is logged and swallowed — shuffle fallback covers the gap."""
        try:
            plan = self.dj.plan_set(
                self._state_doc(ask="plan"), self.catalog,
                deck_view=self.deck_view,
            )
            items: list[Track | BreakItem] = list(plan.tracks)
            if plan.break_after is not None and plan.break_script:
                audio = render_break(plan.break_script, self.settings)
                if audio.backend == "elevenlabs":
                    self.radio_db.record_usage(
                        "tts", tts_chars=len(plan.break_script)
                    )
                brk = BreakItem(audio.path, plan.break_script, audio.duration)
                target = (
                    plan.tracks[plan.break_after + 1]
                    if plan.break_after + 1 < len(plan.tracks) else None
                )
                if self._talkover_fits(brk, target):
                    assert target is not None  # _talkover_fits guarantees
                    self.pending_talkover[str(target.abs_path)] = brk
                    log.info(
                        "talkover armed: %.1fs break rides %s — %s "
                        "(intro %.1fs)",
                        brk.duration, target.display_artist,
                        target.display_title,
                        self.catalog.intro_len(target.hash) or 0.0,
                    )
                else:
                    items.insert(plan.break_after + 1, brk)
            self.picker.mark_planned(t.hash for t in plan.tracks if t.hash)
            if plan.note:
                self.radio_db.record_event("dj_note", data={"note": plan.note})
            self.deck.extend(items)  # publish last: /next may fire any moment
            if self.current_program is not None:
                # a fresh set after a program's tail: the show is over
                log.info("program over: %r — rotation resumes", self.current_program)
                self.current_program = None
            log.info("deck now holds %d items", len(self.deck))
            # memory upkeep rides the planning cadence: deck is published,
            # we're already in a worker thread, and failures are swallowed
            self.chronicle.maintain()
        except Exception:
            log.exception("set planning failed — shuffle covers this stretch")
        finally:
            self.planning = False

    def start_program(self, brief: str, minutes: float) -> None:
        """One commissioned-program pass → the un-aired deck is replaced by
        the whole show. Worker thread; any failure is logged and swallowed —
        the existing rotation stands when it does."""
        try:
            doc = self._state_doc(
                occasion=brief, ask="program", program_minutes=minutes
            )
            program = self.dj.plan_program(
                doc, self.catalog, minutes, deck_view=self.deck_view
            )
            # render every talk up front (tape delay is the discipline here:
            # the scripts were written to survive airing an hour later)
            items: list[Track | BreakItem] = []
            talkovers: list[tuple[BreakItem, Track]] = []
            for idx, entry in enumerate(program.items):
                if isinstance(entry, Track):
                    items.append(entry)
                    continue
                assert isinstance(entry, TalkSegment)
                try:
                    audio = render_break(entry.script, self.settings)
                except Exception:
                    log.exception(
                        "talk segment render failed — program continues without it"
                    )
                    continue
                if audio.backend == "elevenlabs":
                    self.radio_db.record_usage("tts", tts_chars=len(entry.script))
                brk = BreakItem(audio.path, entry.script, audio.duration, "program")
                nxt = (program.items[idx + 1]
                       if idx + 1 < len(program.items) else None)
                if isinstance(nxt, Track) and self._talkover_fits(brk, nxt):
                    talkovers.append((brk, nxt))
                else:
                    items.append(brk)
            music = [i for i in items if isinstance(i, Track)]
            if not music:
                raise RuntimeError("program came back with no playable tracks")
            for brk, target in talkovers:
                self.pending_talkover[str(target.abs_path)] = brk
                log.info("talkover armed (program): %.1fs rides %s — %s",
                         brk.duration, target.display_artist,
                         target.display_title)
            self.picker.mark_planned(t.hash for t in music if t.hash)
            dropped = len(self.deck)
            # publish as adjacently as possible: /next may fire between these
            self.deck.clear()
            self.deck.extend(items)
            self.current_program = program.title
            if program.note:
                self.radio_db.record_event("dj_note", data={"note": program.note})
            self.radio_db.record_event("program_set", data={
                "title": program.title,
                "minutes": minutes,
                "brief": brief,
                "tracks": len(music),
                "talks": len(items) - len(music) + len(talkovers),
            })
            log.info(
                "program on deck: %r — %d items (%d queued items dropped)",
                program.title, len(items), dropped,
            )
        except Exception:
            log.exception("program planning failed — the rotation stands")
        finally:
            self.planning = False

    def _talkover_fits(self, brk: BreakItem, target: Track | None) -> bool:
        """A break rides the next track's intro only when the measured quiet
        window holds the whole thing: entry latency, speech, and a release
        before the song proper. Anything else airs standalone — a talk-over
        that runs into vocals is worse than no talk-over at all."""
        if not self.settings.talkover_enabled or target is None:
            return False
        intro = self.catalog.intro_len(target.hash)
        return (
            intro is not None
            and brk.duration + self.settings.talkover_margin_s <= intro
        )

    def push_talkover(self, brk: BreakItem, track: Track) -> None:
        """Hand an armed break to the mixer's voice queue. Worker thread;
        a failed push loses the break, never the music."""
        url = f"http://127.0.0.1:{self.settings.harbor_port}/voice"
        try:
            resp = httpx.post(url, content=str(brk.wav), timeout=5)
            resp.raise_for_status()
        except httpx.RemoteProtocolError:
            # harbor queues the voice before answering; a dropped socket
            # after the write almost certainly means "queued, then hung up"
            # (seen with handlers that return no body). Record it — a break
            # that aired without its transcript is worse than this risk.
            log.warning("talkover push got no HTTP response — assuming queued")
        except httpx.HTTPError:
            log.exception("talkover push failed — break lost, music unaffected")
            return
        self.radio_db.record_event(
            "break_aired",
            data={
                "transcript": brk.transcript,
                "mode": "talkover",
                "over": f"{track.display_artist} — {track.display_title}",
            },
        )
        log.info(
            "talkover on air: %.1fs over %s — %s (intro window %.1fs)",
            brk.duration, track.display_artist, track.display_title,
            self.catalog.intro_len(track.hash) or 0.0,
        )

    def make_break(self, occasion: str | None = None, mode: str = "standalone") -> None:
        """Standalone break → front of the deck (airs in ~2 tracks).
        Worker thread; failures logged and swallowed."""
        try:
            result = self.dj.write_break(self._state_doc(occasion))
            audio = render_break(result.script, self.settings)
            if audio.backend == "elevenlabs":
                self.radio_db.record_usage("tts", tts_chars=len(result.script))
            if result.note:
                self.radio_db.record_event("dj_note", data={"note": result.note})
            self.deck.appendleft(
                BreakItem(audio.path, result.script, audio.duration, mode)
            )
            log.info("break ready (%s): %r", mode, result.script[:80])
        except Exception:
            log.exception("break generation failed — music continues")
        finally:
            self.break_busy = False

    def run_enrichment(self) -> None:
        """One pass of station/enrich.sh (analyzer + genres, incremental).
        Worker thread; the station must never notice a failed pass."""
        script = self.settings.persona_path.parent / "enrich.sh"
        log_path = Path.home() / ".local/state/kalliope/enrich.log"
        try:
            with open(log_path, "w") as out:
                proc = subprocess.run(
                    [str(script)], stdout=out, stderr=subprocess.STDOUT,
                    timeout=3 * 3600,
                )
            tail = ""
            try:
                tail = log_path.read_text()[-500:].strip().splitlines()[-1]
            except (OSError, IndexError):
                pass
            log.info("enrichment pass done (exit %d): %s", proc.returncode, tail)
        except (subprocess.TimeoutExpired, OSError):
            log.exception("enrichment pass failed — catalog stays as-is")

    def close(self) -> None:
        self.catalog.close()
        self.radio_db.close()


def build_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        station = Station(settings)
        app.state.station = station
        log.info(
            "on air: %d tracks in catalog, hls at %s",
            station.catalog.track_count(),
            settings.hls_dir,
        )

        async def enrich_loop() -> None:
            # first pass shortly after startup (let the stream settle),
            # then every enrich_hours; single-flight by construction
            await asyncio.sleep(600)
            while True:
                await asyncio.to_thread(station.run_enrichment)
                await asyncio.sleep(settings.enrich_hours * 3600)

        enricher = (
            asyncio.create_task(enrich_loop())
            if settings.enrich_hours > 0 else None
        )
        try:
            yield
        finally:
            if enricher is not None:
                enricher.cancel()
            station.close()

    app = FastAPI(title="kalliope", lifespan=lifespan)
    app.mount("/hls", StaticFiles(directory=settings.hls_dir), name="hls")

    @app.get("/next", response_class=PlainTextResponse)
    async def next_track(request: Request) -> Response:
        station: Station = request.app.state.station
        uri = station.next_uri()
        if uri is None:
            return PlainTextResponse("", status_code=404)
        if station.deck_running_low():
            station.planning = True
            asyncio.create_task(asyncio.to_thread(station.plan_more))
        return PlainTextResponse(uri)

    @app.post("/aired")
    async def aired(event: AiredEvent, request: Request) -> JSONResponse:
        station: Station = request.app.state.station
        station.log_transition(event.filename)

        def show(np: NowPlaying) -> None:
            station.now_playing = np

        def show_later(np: NowPlaying) -> None:
            # the display flips when *listeners* hear the change, not when
            # the mixer does — harbor's burst buffer runs ~2.7s behind
            asyncio.get_running_loop().call_later(
                settings.display_latency_s, show, np
            )

        brk = station.breaks_out.pop(event.filename, None)
        if brk is not None:
            station.radio_db.record_event(
                "break_aired",
                data={"transcript": brk.transcript, "mode": brk.mode},
            )
            show_later(NowPlaying(
                station=station.settings.station_name,
                started_at=now_iso(),
                track=None,
                source_context="break",
            ))
            log.info("on air: [break] %r", brk.transcript[:80])
            return JSONResponse({"recorded": True, "break": True})
        track = station.catalog.by_abs_path(event.filename)
        if track is None:
            # not in the catalog (e.g. a future jingle file) — still surface
            # it as now-playing, just don't write history
            log.info("aired non-catalog file: %s", event.filename)
            show_later(NowPlaying(
                station=station.settings.station_name,
                started_at=now_iso(),
                track=None,
            ))
            return JSONResponse({"recorded": False})
        station.radio_db.record_play(track, context="rotation")
        armed = station.pending_talkover.pop(event.filename, None)
        if armed is not None:
            # the track just hit the mixer: the voice enters now, riding
            # the intro the fit-check already measured
            asyncio.create_task(
                asyncio.to_thread(station.push_talkover, armed, track)
            )
        show_later(NowPlaying(
            station=station.settings.station_name,
            started_at=now_iso(),
            track=track,
        ))
        log.info("on air: %s — %s", track.display_artist, track.display_title)
        return JSONResponse({"recorded": track.hash is not None})

    @app.post("/event")
    async def event(evt: StationEvent, request: Request) -> JSONResponse:
        station: Station = request.app.state.station
        if evt.type == "tune_in" and evt.who:
            if station.tune_in(evt.who):
                # first company after a long quiet stretch — wake the DJ
                # for a show open (airs in ~2 tracks, tape delay and all)
                station.break_busy = True
                log.info("show open: waking the DJ (someone's here)")
                asyncio.create_task(asyncio.to_thread(
                    station.make_break,
                    "Someone just tuned in — the first company after a long "
                    "quiet stretch. Open the show. This airs a few minutes "
                    "from now; they'll probably still be there, but write so "
                    "it survives if they aren't.",
                    "show_open",
                ))
            if station.deck_running_low():
                # auto mode wakes on company: the deck drained while Cal
                # was off the clock; someone's here now, so plan
                station.planning = True
                asyncio.create_task(asyncio.to_thread(station.plan_more))
        elif evt.type == "tune_out" and evt.who:
            station.tune_out(evt.who)
        else:
            station.radio_db.record_event(evt.type, who=evt.who, data=evt.data)
        return JSONResponse({"ok": True})

    # --- admin: the power lever (token-gated; unset token = disabled) ------

    def _admin_ok(request: Request) -> bool:
        token = settings.admin_token
        provided = request.headers.get("x-admin-token") or request.query_params.get(
            "token"
        )
        return bool(token) and provided == token

    @app.get("/admin/status")
    def admin_status(request: Request) -> JSONResponse:
        station: Station = request.app.state.station
        if not _admin_ok(request):
            return JSONResponse({"ok": False, "reason": "bad or missing token"}, 403)
        return JSONResponse({
            "power": station.power,
            "dj_on_the_clock": station.dj_active(),
            "dj_model": station.settings.dj_model,
            "listeners": len(station.listeners),
            "deck": len(station.deck),
            "planning": station.planning,
            "program": station.current_program,
            "spend": station.radio_db.spend_summary(),
        })

    @app.post("/admin/power")
    async def admin_power(request: Request) -> JSONResponse:
        station: Station = request.app.state.station
        if not _admin_ok(request):
            return JSONResponse({"ok": False, "reason": "bad or missing token"}, 403)
        try:
            body = await request.json()
            mode = str(body.get("mode", ""))
        except Exception:
            mode = ""
        if mode not in ("dj", "auto", "music"):
            return JSONResponse(
                {"ok": False, "reason": "mode must be dj | auto | music"}, 422
            )
        station.set_power(mode)
        return JSONResponse({"ok": True, "power": mode})

    @app.post("/admin/program")
    async def admin_program(request: Request) -> JSONResponse:
        """Commission a program: Cal builds a whole show in one sitting and
        it replaces whatever was queued (what's already prefetched still airs)."""
        station: Station = request.app.state.station
        if not _admin_ok(request):
            return JSONResponse({"ok": False, "reason": "bad or missing token"}, 403)
        try:
            body = await request.json()
            minutes = float(body.get("minutes", 0))
            brief = str(body.get("brief", "")).strip()
        except Exception:
            return JSONResponse({"ok": False, "reason": "bad request body"}, 422)
        if not 10 <= minutes <= 360:
            return JSONResponse(
                {"ok": False, "reason": "minutes must be 10..360"}, 422
            )
        if not station.dj.on_shift:
            return JSONResponse({"ok": False, "reason": "DJ not on shift"}, 503)
        if station.power == "music":
            return JSONResponse(
                {"ok": False, "reason": "power is music — no API calls"}, 503
            )
        if station.planning:
            return JSONResponse({"ok": False, "reason": "already planning"}, 409)
        station.planning = True
        asyncio.create_task(asyncio.to_thread(
            station.start_program,
            brief or "Your call entirely — program the show you want to make.",
            minutes,
        ))
        return JSONResponse(
            {"ok": True, "status": "programming", "minutes": minutes}
        )

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page() -> HTMLResponse:
        html = (
            resources.files("kalliope").joinpath("static/admin.html").read_text()
        )
        return HTMLResponse(html)

    @app.get("/events")
    def events(request: Request, limit: int = 50) -> JSONResponse:
        station: Station = request.app.state.station
        return JSONResponse(station.radio_db.recent_events(min(limit, 500)))

    @app.post("/dj/break")
    async def force_break(request: Request) -> JSONResponse:
        """Manual trigger: write and queue a break now (testing / 'say something')."""
        station: Station = request.app.state.station
        if not station.dj.on_shift:
            return JSONResponse({"ok": False, "reason": "DJ not on shift"}, 503)
        if not station.dj_active():
            return JSONResponse(
                {"ok": False, "reason": f"DJ off the clock (power={station.power})"},
                503,
            )
        queued = any(isinstance(i, BreakItem) for i in station.deck)
        if station.break_busy or queued:
            return JSONResponse({"ok": False, "reason": "break already in flight"}, 409)
        station.break_busy = True
        asyncio.create_task(asyncio.to_thread(station.make_break))
        return JSONResponse(
            {"ok": True, "status": "writing", "airs": "in about two tracks"}
        )

    @app.get("/now")
    def now(request: Request) -> JSONResponse:
        station: Station = request.app.state.station
        payload = station.now_playing.model_dump(mode="json")
        payload["stream_path"] = (
            settings.stream_public or f":{settings.harbor_port}/stream"
        )
        payload["listeners"] = len(station.listeners)
        track = station.now_playing.track
        payload["genres"] = (
            station.catalog.genres_for(track.hash) if track else []
        )
        payload["deck"] = [
            "break" if isinstance(i, BreakItem)
            else f"{i.display_artist} — {i.display_title}"
            for i in list(station.deck)[:8]
        ]
        return JSONResponse(payload)

    @app.get("/", response_class=HTMLResponse)
    def player() -> HTMLResponse:
        html = (
            resources.files("kalliope").joinpath("static/player.html").read_text()
        )
        return HTMLResponse(html)

    # PWA surface: these live at the root (a service worker's scope can't
    # exceed the directory it is served from)
    _PWA_FILES = {
        "manifest.webmanifest": "application/manifest+json",
        "sw.js": "text/javascript",
        "icon-192.png": "image/png",
        "icon-512.png": "image/png",
        "icon-maskable.png": "image/png",
    }

    @app.get("/{asset}")
    def pwa_asset(asset: str) -> Response:
        media_type = _PWA_FILES.get(asset)
        if media_type is None:
            return PlainTextResponse("not found", status_code=404)
        data = resources.files("kalliope").joinpath(f"static/{asset}").read_bytes()
        return Response(content=data, media_type=media_type)

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    settings = load_settings()
    app = build_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
