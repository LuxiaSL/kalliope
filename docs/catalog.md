# Catalog DB — navigation guide

How kalliope reads (and selectively writes) the freshpool catalog. This is the
*navigable* companion to the contract in `SPEC.md` Appendix A; if the two ever
disagree, the appendix wins, and the appendix loses to the acquisition code.

Grounded against the live DB on 2026-08-04 (101 tracks, mid-sync).

## 30-second orientation

- **One SQLite file**: `~/.local/state/freshpool/catalog.db` — but always take
  the path from `CATALOG_DB` (env/config), never hardcode.
- **WAL mode.** Many readers, one writer per table-owner. Kalliope holds one
  long-lived read connection; short write transactions for its own tables.
- **Two owners, disjoint tables**:

  | Tables | Owner | Kalliope's access |
  |---|---|---|
  | `roots`, `tracks` (+ `artists`, `albums` views) | freshpool (`catalog-scan`) | **read-only, always** |
  | `fetch_attempts` | freshpool (`sync-missing`) | read-only (DJ may riff on "the ones that got away") |
  | `plays`, `genres`, `playlists`, `playlist_tracks`, `track_analysis`, `events` | kalliope | read/write; `CREATE TABLE IF NOT EXISTS` at startup |

- **Timestamps** everywhere are ISO-8601 local-naive strings
  (`YYYY-MM-DDTHH:MM:SS`, via `datetime.now().isoformat(timespec="seconds")`).
  They sort correctly as strings; compare them as strings.

## Paths: nothing is absolute

`tracks.path` is relative. Resolve through `roots`:

```sql
SELECT r.base || '/' || t.path AS abs_path
FROM tracks t JOIN roots r USING (root);
```

Current roots (read the table, don't assume the set is fixed):

- `library` → `~/Music/library` — deliberate fetches (`get-spotify`)
- `pool` → `~/Music/freshpool` — auto-fetched playlist syncs; this is the
  "new-music bin" and the source of graduation candidates

## Identity: `id` is a convenience, `hash` is the truth

Read A.4 twice; the short version:

- Filesystem is ground truth — scans **prune** rows whose files vanished.
- Rename/move *within* a root: row survives (matched by hash), `id` and
  `first_seen` survive.
- Move *across* roots (pool → library promotion): **remove + re-insert**. New
  `id`, `first_seen` resets.
- Therefore every durable reference kalliope makes (plays, requests,
  annotations) stores **`hash`**, with `track_id` as a re-linkable join hint.
  After promotions, re-link: `UPDATE plays SET track_id = (SELECT id FROM
  tracks WHERE hash = plays.track_hash) WHERE ...` — and tolerate misses.
- `plays` rows whose track was pruned are **still valid history**. Never
  cascade-delete, and **do not enable `PRAGMA foreign_keys` on connections
  that touch `plays`** — dangling `track_id`s are expected and legal there.

## `tracks` — what's actually in the columns

Schema is in the appendix; here's what live data taught us:

- **NULLs everywhere, tolerate them all.** Tagless files still index with just
  `path` + `duration`. Every tag column can be NULL.
- **`artist` can be multi-valued**, joined with `/`:
  `"DJ Shadow/Run The Jewels"`. The first segment is the primary artist. Split
  on `/` for display/matching; don't `WHERE artist = ?` for artist lookups —
  prefer `artist LIKE ? || '%'` or match against the `artists` view (which
  groups on `COALESCE(album_artist, artist)`).
- **`genre` is empty in practice so far** (spotdl isn't writing genre tags on
  the current pool). Don't build sequencing logic that assumes it; kalliope's
  own `genres` table (multi-source: tag/spotify/manual/inferred) is the real
  genre substrate.
- **Analysis columns (`bpm`, `energy`, `intro_len`, `outro_type`,
  `loudness_lufs`) are reserved and stay NULL — kalliope ignores them.**
  Decision 2026-08-04: analysis lives in kalliope's own `track_analysis`
  table (below), keyed by hash. The reserved columns remain freshpool's to
  populate or drop someday; never read them as authoritative.
- `size`/`mtime` are the scanner's change-detection cache — not for consumers.
- Scanned extensions: `.mp3 .flac .m4a .ogg .opus`.

## Views: `artists` and `albums`

Read-only conveniences over `tracks`, grouped on
`COALESCE(album_artist, artist)`. Good for browse UIs and DJ "what do we have
by X" lookups; not identity-stable (they re-derive on every query).

## Kalliope-owned tables

Created at startup with `CREATE TABLE IF NOT EXISTS`; never touch freshpool's.

```sql
-- airplay history; durable via track_hash (see identity section)
CREATE TABLE IF NOT EXISTS plays (
  id INTEGER PRIMARY KEY,
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  track_hash TEXT NOT NULL,
  aired_at TEXT NOT NULL,
  context TEXT                    -- 'rotation' | 'request' | 'show_open' | ...
);

-- multi-genre, multi-source; supersedes tracks.genre for sequencing.
-- Keyed by hash (2026-08-04 rebuild: originally track_id, but hash is the
-- durable identity — genre rows must survive pool → library promotion).
-- Backfilled by scripts/genres.py: MusicBrainz artist tags first
-- (source='musicbrainz'), Claude inference for the artists MB doesn't know
-- (source='inferred'). Spotify genre metadata is dead for new apps —
-- 'spotify' remains legal for old data but nothing writes it today.
CREATE TABLE IF NOT EXISTS genres (
  track_hash TEXT NOT NULL,
  genre TEXT NOT NULL,
  source TEXT NOT NULL CHECK
    (source IN ('tag','spotify','musicbrainz','manual','inferred')),
  PRIMARY KEY (track_hash, genre, source)
);

CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  source TEXT NOT NULL            -- 'spotify-import' | 'manual' | ...
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
  playlist_id INTEGER NOT NULL REFERENCES playlists(id),
  track_id INTEGER NOT NULL REFERENCES tracks(id),
  position INTEGER,
  PRIMARY KEY (playlist_id, track_id)
);

-- session/listener log (SPEC §1.6) — the DJ's memory substrate.
-- tune_in/tune_out arrive via liquidsoap harbor connect callbacks;
-- track airings live in plays, not here (single source per fact).
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  type TEXT NOT NULL,          -- 'tune_in','tune_out','break_aired','dj_note','request'
  who TEXT,                    -- listener identity: ip for now, token name later
  data TEXT                    -- JSON payload, event-type specific
);

-- audio analysis, kalliope-owned (decision 2026-08-04).
-- Keyed by hash so results survive pool → library promotion.
CREATE TABLE IF NOT EXISTS track_analysis (
  track_hash TEXT PRIMARY KEY,
  bpm REAL,
  energy REAL,                    -- 0–1
  intro_len REAL,                 -- seconds of low/no-vocal intro (talk-over window)
  outro_type TEXT CHECK (outro_type IN ('cold','fade')),
  loudness_lufs REAL,             -- integrated loudness
  analyzed_at TEXT NOT NULL,
  analyzer_version TEXT
);
```

"Analyzed?" = the hash has a row here with `bpm IS NOT NULL`. The DJ/mixer
must have NULL-safe fallbacks (mixer defaults) indefinitely — the analyzer
backfills lazily and new pool tracks arrive faster than it runs.

`scripts/analyze.py` is the backfiller (one ffmpeg decode + librosa +
pyloudnorm per track; incremental by `analyzer_version`). v1 semantics to
keep honest about: `energy` is relative onset density (compare within the
library, not across analyzers), and `intro_len` is a *quiet-intro* proxy —
seconds until RMS holds above half the track median — not true vocal
detection. Good enough to place talk-overs; don't oversell it.

## Query cookbook

```sql
-- resolve playable path for one track
SELECT r.base || '/' || t.path AS abs_path
FROM tracks t JOIN roots r USING (root) WHERE t.id = :id;

-- new-music bin: fresh pool adds, newest first
SELECT t.*, r.base || '/' || t.path AS abs_path
FROM tracks t JOIN roots r USING (root)
WHERE t.root = 'pool' ORDER BY t.first_seen DESC;

-- fresh and never aired
SELECT t.* FROM tracks t
WHERE t.root = 'pool'
  AND NOT EXISTS (SELECT 1 FROM plays p WHERE p.track_hash = t.hash);

-- graduation candidates: spun 3+, still in the pool
SELECT t.artist, t.title, COUNT(*) AS spins
FROM plays p JOIN tracks t ON t.hash = p.track_hash
WHERE t.root = 'pool'
GROUP BY t.hash HAVING spins >= 3 ORDER BY spins DESC;

-- track + analysis + last-aired, the DJ's full picture of one song
SELECT t.*, a.bpm, a.energy, a.intro_len, a.outro_type, a.loudness_lufs,
       (SELECT MAX(p.aired_at) FROM plays p WHERE p.track_hash = t.hash) AS last_aired
FROM tracks t LEFT JOIN track_analysis a ON a.track_hash = t.hash
WHERE t.id = :id;

-- don't repeat: anything aired in the last N hours (string compare works)
SELECT DISTINCT track_hash FROM plays WHERE aired_at >= :cutoff_iso;
```

## Connection etiquette

- Open read connections with `PRAGMA query_only = ON` where possible — makes
  the ownership rule mechanical instead of disciplinary.
- Keep write transactions short; `catalog-scan` may want the write lock.
  Set `PRAGMA busy_timeout` (a few seconds) rather than failing fast.
- Leave `foreign_keys` OFF (see identity section — dangling `plays.track_id`
  after prunes is by design).
