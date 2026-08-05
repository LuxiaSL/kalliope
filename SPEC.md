# kalliope — Spec v0.2

A continuous broadcast radio station for a personal music library, with an LLM DJ that sequences tracks, performs breaks, takes call-in requests, and can look things up mid-session.

Named for the muse of the beautiful voice (καλλι + ὄψ) — and for the calliope, the self-playing steam organ: a machine that performs music. Sibling to `syrinx`.

> v0.2 (2026-08-04) folds in four decisions from spec review: project name; analysis moves to a kalliope-owned `track_analysis` table; **always-on** on-air model; **no skip button**. The catalog contract lives with the freshpool acquisition stack (Appendix A there); kalliope's working companion to it is [`docs/catalog.md`](docs/catalog.md).

## Core premise

The station is a **broadcast, not a session**. One continuous audio stream exists server-side whether anyone is listening or not. Clients tune in and out; tuning in mid-song is normal and desirable. Stop/start on the client is a *tuner*, not a pause button.

---

## 1. Components

```
┌─────────────────────────────────────────────────┐
│ SERVER (laptop w/ Tailscale → later VPS)        │
│                                                 │
│  Catalog DB ──┐                                 │
│  (SQLite,     │                                 │
│   freshpool)  ▼                                 │
│  DJ Brain ──► Schedule Queue ──► Mixer ──► HLS  │
│  (Claude API)      │              (liquidsoap)  │
│      ▲             │                            │
│      │             ▼                            │
│  Tools:        TTS Worker                       │
│  - catalog     (Piper/Kokoro,                   │
│  - web search   pre-rendered)                   │
│  - request line                                 │
│  - session log                                  │
└─────────────────────────────────────────────────┘
        │ HLS over Tailscale / authed HTTPS
        ▼
┌──────────────┐  ┌──────────────┐
│ Android app  │  │ Web player   │
│ (primary UI) │  │ (desktop)    │
└──────────────┘  └──────────────┘
```

### 1.1 Catalog & analysis

- **The catalog is not ours.** The freshpool acquisition stack (`catalog-scan`, `get-spotify`, playlist syncs) owns `roots`/`tracks` in `catalog.db`; kalliope holds a long-lived read-only connection and takes the DB path from `CATALOG_DB`. Schema, ownership rules, identity semantics, and the query cookbook: [`docs/catalog.md`](docs/catalog.md).
- **Analysis is ours.** Kalliope runs its own analyzer (essentia or librosa) and writes results to its own `track_analysis` table, keyed by **content hash** so results survive pool→library promotion: BPM, energy, **intro length** (seconds of low/no-vocal content at track start — this is what makes talk-overs possible), outro character (cold vs fade), integrated loudness. The reserved analysis columns in `tracks` stay untouched. Analyzer backfills lazily; everything downstream is NULL-safe forever.
- Kalliope also owns `plays`, `genres`, `playlists`, `playlist_tracks` (created at startup, `IF NOT EXISTS`).
- **Spotify import (one-time-ish job):** OAuth, pull all playlists, fuzzy-match to local files (normalized artist+title, duration ±2s), emit an unmatched report for manual resolution. Playlists land as ground-truth taste clusters. Don't rely on Spotify audio-features (deprecated for new apps) — local analysis replaces it.
- Optional: co-occurrence embedding. Tracks sharing playlists + genre overlap + audio-feature proximity → a cheap similarity index the DJ can query ("more like this, softer").

### 1.2 Mixer (the actual station)

- One continuous output. **liquidsoap** — it natively does queues, crossfades, ducking, silence detection, and Icecast/HLS output, and is scriptable enough to take commands from the DJ service over telnet/socket. Building a custom ffmpeg-concat mixer is possible but liquidsoap is decades of radio edge cases for free.
- Loudness-normalize everything to a target LUFS in the mixer chain (the library spans mastering eras; without this, breakcore will take your head off after a trip-hop block). Use `track_analysis.loudness_lufs` when present, liquidsoap's own normalization as the NULL fallback.
- Crossfade defaults + per-transition overrides from the DJ (e.g. "cold cut into this one, no fade").
- **Break insertion:** breaks are pre-rendered audio files (TTS output) pushed into the queue like tracks. Talk-over-intro breaks are a mixer instruction: play break audio ducked over track N+1's intro, duck depth ~10–14dB, release before vocals per `intro_len`.
- Dead-air watchdog: if the queue underruns (DJ service down, API hiccup), fall back to shuffle-from-catalog with no breaks. The station never stops.

### 1.3 DJ Brain

- A service that wakes on a schedule (e.g. every N tracks, or when queue depth < threshold) and on events (request received, listener tuned in, top of hour).
- Each invocation: one Claude API call with the **station state doc** + available tools; output is structured — next tracks to enqueue, break script (if any), transition directives, log annotations.
- **Station state doc** (rolling, assembled per call): current time; last ~20 aired tracks; current listeners (who tuned in when — you vs Celeste vs guest token); pending requests; recent break transcripts (persona continuity); today's session arc notes; any standing bits/running gags from the log.
- **Tools available to the DJ:**
  - `catalog.search(query | filters | similar_to)` — the primary instrument
  - `catalog.track_info(id)` — full features, play history, playlist memberships
  - `web_search` — look up an artist it's unsure about, what year something dropped, tour dates, whatever it wants to riff on. This is the "DJ has a browser in the booth" affordance.
  - `log.read(range)` / `log.annotate(note)` — memory substrate
  - `requests.pending()` / `requests.resolve(id, accept|decline, reason)`
- **Scheduling model:** everything is lookahead. While track N airs, the DJ plans N+1..N+k and renders any break audio. Nothing is real-time except request acknowledgment, and even that can ride until the current song ends (authentically radio).
- Break cadence: configurable, default every 3–5 tracks, denser at "show open" (first tune-in after silence) and on request handling. Not every transition needs talk — restraint is craft.

### 1.4 TTS Worker

- Queue consumer: takes break scripts, renders WAV, hands path to mixer queue.
- v1: **Piper** (fast, robust). Upgrade path: **Kokoro** for prosody, or a fine-tuned Piper voice if a bespoke DJ voice is wanted later.
- Render SSML-ish pacing hints if the engine supports them; otherwise the DJ writes scripts with punctuation-as-prosody (it's good at this).
- Pre-render always. No streaming TTS anywhere in v1.

### 1.5 Request line ("call in")

- v1: **text requests** from the client app. A request is `{text, who, timestamp}` — free-form: a song, a vibe, a genre swap, a complaint.
- DJ resolves each request explicitly: accept (schedule it, say when-ish), redirect ("not that, but here's the same itch"), or decline **in character with a reason** (set's mid-arc, played it an hour ago, absolutely not at 9am). Declines are a feature; permission logic lives in the persona prompt.
- v2: voice requests — hold-to-talk in the app, whisper.cpp on server transcribes, optionally air a TTS'd paraphrase of "the caller" for the bit.

### 1.6 Session / listener log

- Append-only event log: `tune_in`, `tune_out`, `track_aired`, `break_aired(transcript)`, `request(...)`, `dj_note(...)`. Radio-internal — same DB file or its own, implementer's call (not part of the catalog contract).
- This is the DJ's long-term memory. Nightly (or per-invocation summary) compaction into a digest the state doc includes: listening patterns, standing jokes, requests granted/denied, what got tuned-away-from (tune_out timing against track boundaries is an implicit thumbs-down signal — use gently).

---

## 2. Clients

### 2.1 Android (primary)

- ExoPlayer against the HLS endpoint. Foreground service + MediaSession (lockscreen/notification controls, Bluetooth).
- UI: big tune in/out toggle, now-playing (server pushes metadata via a small WS or ID3-in-stream), request composer, recent-breaks transcript view (nice for "what did the DJ just say" when you tuned in late).
- **Break-aware notices:** client subscribes to station events; on tune-in, show "break in ~2 tracks" / "you just missed a break — read it?" Tune-in events also flow to the server so the DJ *knows* you just walked in, which is where "oh, look who's here" bits come from.
- **No skip control, anywhere** (decided 2026-08-04). Pure broadcast: you get what the station plays, and complaints go through the request line in words. The DJ declining you is part of the show.

### 2.2 Desktop / web

- A web page: `<audio>` element on the HLS URL (hls.js), same metadata WS, same request box. This is nearly free and covers desktop entirely; a "port" is unnecessary.

### 2.3 Auth

- Bearer tokens per person (you, Celeste, maybe a guest token). Tailscale makes v1 auth almost moot; keep tokens anyway so the log knows who's listening.

---

## 3. On-air semantics

**Always-on** (decided 2026-08-04): the stream runs 24/7, on the laptop and later the VPS. The DJ performs breaks when listeners are present; rare unheard breaks are permitted and encouraged as a bit — the log proves the station lives without you.

The dead-air watchdog (§1.2) is what "always" means in practice: DJ down ≠ station down.

---

## 4. Build order

1. **Weekend 1 — the station exists:** liquidsoap playing shuffle from the catalog → HLS; web player tunes in. No DJ yet. (De-risks all audio plumbing first; catalog already exists courtesy of freshpool.)
2. **Weekend 2 — the DJ exists:** DJ service with catalog tool + state doc; Piper breaks between tracks; talk-over not required yet. Persona prompt v1.
3. **Week 3 — interaction:** request line (text), accept/decline flow, session log + tune-in events, break-aware client notices. Android app (or PWA stopgap).
4. **Polish tail (ongoing, fun):** analyzer + talk-over-intro with ducking, transition directives, co-occurrence similarity index, Spotify playlist import, voice requests, Kokoro/custom voice, listener-aware bits.

---

## 5. Costs & footprint

- DJ brain: a few hundred tokens per break, breaks every ~15 min while listening → pennies/day even on heavy rotation.
- TTS/transcription: local, free.
- Hosting: laptop + Tailscale = $0; VPS later ~$5–15/mo + block storage for the library (FLAC libraries run ~200–500GB → cheap on Hetzner).
- Bandwidth: one 256kbps-ish stream to 1–2 listeners is nothing. Always-on costs upload bandwidth only while someone is tuned in — HLS is pull.

## 6. Open questions (decide when they hurt)

- Transcode-at-ingest to a uniform mezzanine format vs. liquidsoap decoding everything live (probably the former for CPU predictability on a laptop). If a mezzanine cache exists, it's kalliope-owned and keyed by hash, like analysis.
- How much the DJ controls *micro*-sequencing (per-transition fades) vs. leaving mixer defaults — start with defaults, add directives when a transition annoys you.
### Decided (2026-08-04)

- Name: **kalliope**.
- Analysis: kalliope-owned `track_analysis` table keyed by hash; `tracks`' reserved columns stay untouched.
- On-air: **always-on**.
- Skip: **none**. The request line is the only lever.
- Personas: **shared station identity**, per-listener memory (it's what a real station does).
- DJ persona: **Cal** ([`station/persona.md`](station/persona.md)) — deadpan public radio; a Claude, and it comes up about as often as being from Ohio would. Claude-authored, fittingly.
- Signal: **patina on** — a whisper of lowpassed hiss under everything (survives dead air: the carrier always sounds live) + gentle 48Hz–11.5kHz bandpass warmth. `KALLIOPE_PATINA` (gain, 0 disables) / `KALLIOPE_WARMTH` (on/off).
- TTS: **ElevenLabs when keyed, piper as the always-there fallback** — a failed API render must degrade to a worse voice, never to a lost break. Flash v2.5 by default (half credits; free tier ≈ sixty breaks/month).
- Genres: **artist-level, from MusicBrainz + Claude inference** (`scripts/genres.py`), keyed by hash like analysis. Spotify's genre metadata is dead for new apps; the `tracks.genre` column stays empty in practice. Provenance in `genres.source`.
- Analyzer: **`scripts/analyze.py` shipped** (bpm/energy/intro/outro/LUFS, v1 semantics documented in docs/catalog.md); browse tools carry bpm/energy/genres so sequencing-by-sound is real.
- Talk-over ducking: **shipped** — a planned break rides the next track's intro when `intro_len` fits the render plus margin (server decides; Cal sees `intro_s` and aims for runways). Mixer side is a `smooth_add` voice queue fed over harbor HTTP; breaks are loudnorm'd to −16 LUFS so the voice sits predictably above the ducked bed. Forced `/dj/break` stays standalone by design.
