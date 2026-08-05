# kalliope

A broadcast radio station for a personal music library, with an LLM DJ.

Not a shuffle button: one continuous stream exists server-side whether anyone
is listening or not. Clients tune in and out; tuning in mid-song is normal
and desirable. The DJ — **Cal**, a Claude with a deadpan public-radio
sensibility ([`station/persona.md`](station/persona.md)) — programs the
rotation by browsing the catalog with tools, plans sets of four to six
tracks, writes its own breaks, and reads them through a local TTS voice.
The signal carries a deliberate whisper of static, because a perfect stream
sounds like a file and a slightly worn one sounds like a place.

Named for the muse of the beautiful voice — and for the calliope, the
self-playing steam organ.

```
catalog.db ──► picker/state server ──► liquidsoap ──► mp3 stream (:8322)
 (SQLite)       │  FastAPI :8321  │      │  crossfade      └► HLS segments
                │  /next /aired   │      │  normalize
    Cal ◄───────┤  /now /events   │      └  patina (hiss + warmth)
 (Claude API)   │  web player     │
    │           └─────────────────┘
    └── plans sets, writes breaks ──► piper TTS ──► break WAVs into rotation
```

## What you need

- **Python 3.13+** and [`uv`](https://docs.astral.sh/uv/)
- **[liquidsoap](https://www.liquidsoap.info/) 2.4+** — the mixer
  (`nix profile install nixpkgs#liquidsoap`, `apt install liquidsoap`,
  or `opam install liquidsoap`)
- **`curl`** (liquidsoap uses it to talk to the server)
- A **music library** on disk (`.mp3 .flac .m4a .ogg .opus`)
- Optional, for the DJ: an **Anthropic API key** and
  **[piper-tts](https://github.com/rhasspy/piper)**
  (`nix profile install nixpkgs#piper-tts`) with a voice model.
  Without these the station still runs — shuffle with crossfades, no voice.

## Setup

```sh
git clone git@github.com:LuxiaSL/kalliope.git && cd kalliope
uv sync

# 1. Build a catalog from your music (incremental; re-run whenever)
uv run scripts/scan.py ~/Music
export CATALOG_DB=~/.local/state/kalliope/catalog.db

# 2. (optional) the DJ's voice
mkdir -p ~/.local/state/kalliope/voices && cd ~/.local/state/kalliope/voices
curl -fsSLO "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx"
curl -fsSLO "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json"
cd -

# 3. (optional) the DJ's brain — .env in the repo root, gitignored
echo 'ANTHROPIC_API_KEY="sk-ant-..."' > .env && chmod 600 .env

# 4. (optional) a nicer voice — ElevenLabs key in the same .env; used
#    automatically when present, falls back to piper when it fails
echo 'ELEVENLABS_API_KEY="..."' >> .env

# 5. on air
./station/run.sh

# 6. (optional, anytime) enrich the catalog — the DJ sequences better with
#    measured tempo/energy and genre labels in its tool results
./station/enrich.sh           # analyzer + genres, incremental; symlink it
                              # somewhere on PATH if you like. The running
                              # station also fires this every 6h on its own
                              # (KALLIOPE_ENRICH_HOURS, 0 to disable)
```

Player at **http://127.0.0.1:8321/** — one big tune-in button, now-playing,
listener count. Raw stream at `http://127.0.0.1:8322/stream` for anything
that plays MP3 (mpv, VLC, a real tuner app).

## Configuration

Everything is environment variables (or `.env`):

| Variable | Default | What |
|---|---|---|
| `CATALOG_DB` | `~/.local/state/freshpool/catalog.db` | The catalog SQLite (see below) |
| `KALLIOPE_PORT` / `KALLIOPE_HARBOR_PORT` | `8321` / `8322` | Server / stream ports |
| `KALLIOPE_NO_REPEAT_HOURS` | `6` | Shuffle-fallback no-repeat window |
| `ANTHROPIC_API_KEY` | — | DJ brain; absent = DJ-less station |
| `KALLIOPE_DJ_MODEL` | `claude-opus-5` | `claude-sonnet-5` cuts cost ~4x |
| `KALLIOPE_DJ_ENABLED` | `true` | Hard off-switch for the DJ |
| `ELEVENLABS_API_KEY` | — | Premium TTS voice; absent = piper |
| `KALLIOPE_TTS_BACKEND` | `auto` | `piper` forces local; `auto` prefers ElevenLabs when keyed |
| `KALLIOPE_ELEVENLABS_VOICE` | George | Any voice id from `GET /v1/voices` |
| `KALLIOPE_ELEVENLABS_MODEL` | `eleven_flash_v2_5` | `eleven_multilingual_v2` for max quality at 2x credits |
| `KALLIOPE_PIPER_VOICE` | `…/voices/en_US-lessac-medium.onnx` | Local/fallback TTS voice model |
| `KALLIOPE_POWER_DEFAULT` | `dj` | `auto` = DJ works only while someone listens (tune-in wakes it); `music` = no API at all |
| `KALLIOPE_LISTENER_LINGER_MIN` | `30` | `auto` keeps the DJ on this long after the room empties |
| `KALLIOPE_ADMIN_TOKEN` | — | Enables `/admin` (power lever page + API); unset = disabled |
| `KALLIOPE_SHOW_OPEN_QUIET_MIN` | `45` | Tune-in after this many empty minutes earns a show open; `0` disables |
| `KALLIOPE_ENRICH_HOURS` | `6` | Station self-runs `enrich.sh` this often; `0` disables |
| `KALLIOPE_TALKOVER_ENABLED` | `true` | Breaks ride long intros when the analyzed window fits |
| `KALLIOPE_TALKOVER_MARGIN_S` | `3.5` | Entry + release slack a talk-over needs inside the intro |
| `KALLIOPE_DUCK` | `0.2` | Music level under a talk-over voice (0.2 ≈ −14dB) |
| `KALLIOPE_VOICE_GAIN` | `1.0` | Talk-over voice trim, by ear |
| `KALLIOPE_PATINA` | `0.002` | Static bed gain; `0` for a clean signal |
| `KALLIOPE_WARMTH` | `on` | Gentle bandpass; `off` to disable |
| `KALLIOPE_AUTO_UPDATE` | `on` | `run.sh` fast-forwards to origin/main on start (stashes local edits, never blocks startup); `off` for development checkouts |

## How it fits together

- **The catalog is a contract, not a component.** Kalliope reads a SQLite
  file describing your library (`roots` + `tracks` tables) and never writes
  to those tables — [`docs/catalog.md`](docs/catalog.md) documents the schema
  and its identity rules. `scripts/scan.py` is a minimal scanner that
  fulfills the contract; any acquisition pipeline that writes the same shape
  works (the home install feeds it from a spotdl-based stack). Kalliope adds
  its own tables alongside: play history, session events, break transcripts.
- **The DJ plans, the shuffle covers.** When the on-deck queue runs low, Cal
  gets catalog tools (search, fresh-arrivals, random sample) and commits an
  ordered set with a break placed where a breath belongs. If planning ever
  fails, a no-repeat shuffle takes over seamlessly. The station never stops.
- **Breaks are pre-rendered audio**, slotted into rotation like tracks.
  Transcripts land in the `events` table; `GET /events` serves recent ones
  (the "what did the DJ just say" view).
- **Endpoints:** `/` player · `/now` now-playing + deck preview · `/events`
  session log · `POST /dj/break` force a break (airs in ~2 tracks) ·
  `/hls/live.m3u8` HLS variant of the stream.

## Status

Working: continuous stream, crossfades, patina, DJ set-planning with breaks,
listener presence (tune-ins are events the DJ can see), play history, audio
analysis (BPM/energy/intro/outro/LUFS via `scripts/analyze.py`), genre
backfill (MusicBrainz + inference via `scripts/genres.py`) feeding the DJ's
catalog tools and the player display, ElevenLabs voice with piper fallback,
talk-over breaks (the DJ's voice rides a long instrumental intro, music
ducked underneath, released before the song proper — only when the analyzed
intro window fits), show opens (first tune-in after a quiet stretch wakes
the DJ), long-term memory (the session log periodically folds into
first-person "chapters" and "arcs" the DJ carries into every break — months
of broadcast compact into character), web player. Planned, per
[`SPEC.md`](SPEC.md): the request line ("call in"), Android client.

Runs on a laptop. Meant to feel less like an app and more like a place.
