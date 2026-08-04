"""Kalliope station server.

Speaks to three parties:
  - liquidsoap: GET /next (plain-text path to play), POST /aired (on_track)
  - listeners' players: GET / (web player), GET /now, /hls/* (segments)
  - later: the DJ brain and the request line live here too
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .catalog import Catalog
from .config import Settings, load_settings
from .dj import DJ, build_state_doc
from .models import AiredEvent, NowPlaying, StationEvent, Track, now_iso
from .picker import Picker
from .radio_db import RadioDB
from .tts import render_break

log = logging.getLogger("kalliope")


@dataclass
class BreakItem:
    wav: Path
    transcript: str


class Station:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.catalog = Catalog(settings.catalog_db)
        self.radio_db = RadioDB(settings.catalog_db)
        self.picker = Picker(self.catalog, self.radio_db, settings.no_repeat_hours)
        self.dj = DJ(settings)
        self.now_playing = NowPlaying(station=settings.station_name)
        # who -> tuned-in-since; keyed by harbor's client ip (tokens later)
        self.listeners: dict[str, str] = {}
        # --- the deck (SPEC §1.3: everything is lookahead) -----------------
        # DJ-planned queue of tracks and breaks; the shuffle picker only
        # plays when the deck runs dry (dead-air fallback, SPEC §1.2)
        self.deck: deque[Track | BreakItem] = deque()
        self.planning = False
        self.break_transcripts: dict[str, str] = {}  # wav path -> script
        self.break_busy = False                       # manual /dj/break guard

    def tune_in(self, who: str) -> None:
        self.listeners[who] = now_iso()
        self.radio_db.record_event("tune_in", who=who)
        log.info("tune in: %s (%d listening)", who, len(self.listeners))

    def tune_out(self, who: str) -> None:
        # harbor's disconnect id may be 'ip' or 'ip:port'; match loosely
        key = who if who in self.listeners else next(
            (k for k in self.listeners if k.split(":")[0] == who.split(":")[0]),
            None,
        )
        if key is not None:
            del self.listeners[key]
        self.radio_db.record_event("tune_out", who=who)
        log.info("tune out: %s (%d listening)", who, len(self.listeners))

    # --- the queue seam: what does liquidsoap play next? -------------------
    # NOTE: liquidsoap prefetches one request ahead, so anything appended to
    # the deck airs at least one track later than "now". Planned sets don't
    # care (they're computed ahead in order); forced breaks land in ~2 tracks.

    def next_uri(self) -> str | None:
        uri: str | None = None
        if self.deck:
            item = self.deck.popleft()
            if isinstance(item, BreakItem):
                self.break_transcripts[str(item.wav)] = item.transcript
                log.info("handing out: [break] %s", item.wav.name)
                # short fades so speech isn't chopped by the crossfade defaults
                uri = f"annotate:liq_fade_in=0.2,liq_fade_out=0.5:{item.wav}"
            else:
                log.info("handing out (deck): %s — %s",
                         item.display_artist, item.display_title)
                uri = str(item.abs_path)
        else:
            track = self.picker.next_track()
            if track is not None:
                log.info("handing out (shuffle fallback): %s — %s",
                         track.display_artist, track.display_title)
                uri = str(track.abs_path)
        return uri

    def deck_running_low(self) -> bool:
        music_left = sum(1 for i in self.deck if not isinstance(i, BreakItem))
        return self.dj.on_shift and not self.planning and music_left <= 1

    def _state_doc(self) -> str:
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
        )

    def plan_more(self) -> None:
        """One DJ planning pass → deck grows by a set. Worker thread; any
        failure is logged and swallowed — shuffle fallback covers the gap."""
        try:
            plan = self.dj.plan_set(self._state_doc(), self.catalog)
            items: list[Track | BreakItem] = list(plan.tracks)
            if plan.break_after is not None and plan.break_script:
                wav = render_break(
                    plan.break_script, self.settings.piper_voice,
                    self.settings.breaks_dir,
                )
                items.insert(plan.break_after + 1, BreakItem(wav, plan.break_script))
            self.picker.mark_planned(t.hash for t in plan.tracks if t.hash)
            if plan.note:
                self.radio_db.record_event("dj_note", data={"note": plan.note})
            self.deck.extend(items)  # publish last: /next may fire any moment
            log.info("deck now holds %d items", len(self.deck))
        except Exception:
            log.exception("set planning failed — shuffle covers this stretch")
        finally:
            self.planning = False

    def make_break(self) -> None:
        """Standalone forced break → front of the deck (airs in ~2 tracks).
        Worker thread; failures logged and swallowed."""
        try:
            result = self.dj.write_break(self._state_doc())
            wav = render_break(
                result.script, self.settings.piper_voice, self.settings.breaks_dir
            )
            if result.note:
                self.radio_db.record_event("dj_note", data={"note": result.note})
            self.deck.appendleft(BreakItem(wav, result.script))
            log.info("break ready: %r", result.script[:80])
        except Exception:
            log.exception("break generation failed — music continues")
        finally:
            self.break_busy = False

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
        try:
            yield
        finally:
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
    def aired(event: AiredEvent, request: Request) -> JSONResponse:
        station: Station = request.app.state.station
        transcript = station.break_transcripts.pop(event.filename, None)
        if transcript is not None:
            station.radio_db.record_event(
                "break_aired", data={"transcript": transcript}
            )
            station.now_playing = NowPlaying(
                station=station.settings.station_name,
                started_at=now_iso(),
                track=None,
                source_context="break",
            )
            log.info("on air: [break] %r", transcript[:80])
            return JSONResponse({"recorded": True, "break": True})
        track = station.catalog.by_abs_path(event.filename)
        if track is None:
            # not in the catalog (e.g. a future jingle file) — still surface
            # it as now-playing, just don't write history
            log.info("aired non-catalog file: %s", event.filename)
            station.now_playing = NowPlaying(
                station=station.settings.station_name,
                started_at=now_iso(),
                track=None,
            )
            return JSONResponse({"recorded": False})
        station.radio_db.record_play(track, context="rotation")
        station.now_playing = NowPlaying(
            station=station.settings.station_name,
            started_at=now_iso(),
            track=track,
        )
        log.info("on air: %s — %s", track.display_artist, track.display_title)
        return JSONResponse({"recorded": track.hash is not None})

    @app.post("/event")
    def event(evt: StationEvent, request: Request) -> JSONResponse:
        station: Station = request.app.state.station
        if evt.type == "tune_in" and evt.who:
            station.tune_in(evt.who)
        elif evt.type == "tune_out" and evt.who:
            station.tune_out(evt.who)
        else:
            station.radio_db.record_event(evt.type, who=evt.who, data=evt.data)
        return JSONResponse({"ok": True})

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
        if station.break_busy or station.pending_break is not None:
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
        payload["stream_path"] = f":{settings.harbor_port}/stream"
        payload["listeners"] = len(station.listeners)
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
