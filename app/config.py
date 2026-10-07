"""Application settings, overridable through ``GEO_*`` environment variables."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str | None = None  # defaults to SQLite inside data_dir

    max_upload_bytes: int = 100 * 1024 * 1024          # size of the uploaded file
    max_uncompressed_bytes: int = 1024 * 1024 * 1024   # zip-bomb guard
    max_archive_entries: int = 500
    read_chunk_size: int = 5000                        # features read from GDAL per chunk
    insert_batch_size: int = 1000

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'app.db').resolve()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
