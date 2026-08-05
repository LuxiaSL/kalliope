"""Station configuration.

Everything comes from the environment (or defaults). The catalog DB path
honors the freshpool contract: take ``CATALOG_DB`` from the environment,
never hardcode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KALLIOPE_", env_file=".env", extra="ignore"
    )

    # Catalog contract: path comes from CATALOG_DB (unprefixed, per Appendix A).
    catalog_db: Path = Field(
        default=Path.home() / ".local/state/freshpool/catalog.db",
        validation_alias=AliasChoices("CATALOG_DB", "KALLIOPE_CATALOG_DB"),
    )

    station_name: str = "kalliope"

    # kalliope HTTP server (picker API, now-playing, player page, HLS files)
    host: str = "127.0.0.1"
    port: int = 8321

    # liquidsoap's harbor output (native mp3 stream for <audio>)
    harbor_port: int = 8322

    # where liquidsoap writes HLS segments; served by us at /hls/
    hls_dir: Path = Path.home() / ".local/state/kalliope/hls"

    # listeners hear the stream ~this many seconds after the mixer plays it
    # (harbor burst buffer); the now-playing display waits to match their ears
    display_latency_s: float = 2.7

    # rotation: don't repeat a track aired within this window (auto-relaxes
    # if the eligible set empties — small libraries must still play)
    no_repeat_hours: float = 6.0

    # --- DJ brain ---------------------------------------------------------
    dj_enabled: bool = True
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "KALLIOPE_ANTHROPIC_API_KEY"),
    )
    dj_model: str = "claude-opus-5"
    persona_path: Path = Path(__file__).resolve().parents[2] / "station/persona.md"
    # break cadence, unheard-break odds, and set length live in the planning
    # brief (dj.PLANNING_BRIEF) — they're editorial judgment, not config

    # --- talk-over breaks (SPEC §1.2) -------------------------------------
    # a break rides over the next track's intro (ducked) when the measured
    # intro window fits the break plus this margin: ~2s of entry latency
    # (crossfade + push) and ~1.5s of release before the song proper
    talkover_enabled: bool = True
    talkover_margin_s: float = 3.5

    # --- TTS --------------------------------------------------------------
    # "auto" = ElevenLabs when a key is present, Piper otherwise; either way
    # a failed render falls through to Piper (the break must not die for a
    # flaky API)
    tts_backend: Literal["auto", "elevenlabs", "piper"] = "auto"
    elevenlabs_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ELEVENLABS_API_KEY", "KALLIOPE_ELEVENLABS_API_KEY"
        ),
    )
    # George — the docs' own example voice; warm, unhurried narration.
    # List alternatives: curl -H "xi-api-key: $ELEVENLABS_API_KEY" \
    #   https://api.elevenlabs.io/v1/voices
    elevenlabs_voice: str = "JBFqnCBsd6RMkjVDRZzb"
    # flash_v2_5 is half the credits of multilingual_v2 and still a tier
    # above local TTS; swap for "eleven_multilingual_v2" if credits allow
    elevenlabs_model: str = "eleven_flash_v2_5"
    piper_voice: Path = Path.home() / ".local/state/kalliope/voices/en_US-lessac-medium.onnx"
    breaks_dir: Path = Path.home() / ".local/state/kalliope/breaks"

    def ensure_dirs(self) -> None:
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        self.breaks_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
