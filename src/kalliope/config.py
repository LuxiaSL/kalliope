"""Station configuration.

Everything comes from the environment (or defaults). The catalog DB path
honors the freshpool contract: take ``CATALOG_DB`` from the environment,
never hardcode.
"""

from __future__ import annotations

from pathlib import Path

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

    # --- TTS --------------------------------------------------------------
    piper_voice: Path = Path.home() / ".local/state/kalliope/voices/en_US-lessac-medium.onnx"
    breaks_dir: Path = Path.home() / ".local/state/kalliope/breaks"

    def ensure_dirs(self) -> None:
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        self.breaks_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
