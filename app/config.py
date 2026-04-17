"""Application settings for local CLI and API execution."""

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.run import ExtractionBackend


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ANGELIC_",
        extra="ignore",
    )

    env: str = Field(default="local")
    output_dir: Path = Field(default=Path("outputs"))
    default_data_room: Path = Field(default=Path("samples/data_room"))
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8011)
    default_currency: str = Field(default="USD")
    default_unit_scale: str = Field(default="ones")
    default_extraction_backend: ExtractionBackend = Field(default="deterministic")
    gemini_api_key: Optional[str] = Field(default=None)
    gemini_model: str = Field(default="gemini-2.5-pro")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance for the current process."""

    return Settings()
