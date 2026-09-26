"""Typed configuration, loaded from the environment with a ``JOBSCOUT_`` prefix."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SYNC_DRIVERS = {
    "postgresql+asyncpg": "postgresql+psycopg",
    "postgresql+psycopg": "postgresql+psycopg",
    "postgresql+pg8000": "postgresql+psycopg",
    "sqlite+aiosqlite": "sqlite",
    "sqlite": "sqlite",
}


class Settings(BaseSettings):
    """Runtime configuration.

    Values come from the environment (or a local ``.env`` file) using the
    ``JOBSCOUT_`` prefix, e.g. ``JOBSCOUT_PER_DOMAIN_RATE=0.5``.
    """

    model_config = SettingsConfigDict(
        env_prefix="JOBSCOUT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./jobscout.db"
    database_url_sync: str | None = None
    database_pool_size: int = Field(default=5, ge=1)
    database_max_overflow: int = Field(default=10, ge=0)
    database_echo: bool = False

    # --- identity / politeness --------------------------------------------
    # A contactable user agent is a precondition for crawling anything: site
    # owners must be able to reach you. Override this in any real deployment.
    user_agent: str = "JobScoutBot/0.1 (+https://github.com/xtraspeed/jobscout)"
    respect_robots: bool = True
    crawl_delay_fallback: float = Field(default=1.0, ge=0.0)

    # --- concurrency and backoff ------------------------------------------
    global_concurrency: int = Field(default=8, ge=1)
    per_domain_concurrency: int = Field(default=2, ge=1)
    per_domain_rate: float = Field(default=1.0, gt=0.0, description="requests/second/host")
    request_timeout: float = Field(default=20.0, gt=0.0)
    connect_timeout: float = Field(default=10.0, gt=0.0)
    max_retries: int = Field(default=4, ge=0)
    backoff_base: float = Field(default=0.5, gt=0.0)
    backoff_cap: float = Field(default=30.0, gt=0.0)
    circuit_failure_threshold: int = Field(default=8, ge=1)
    circuit_reset_seconds: float = Field(default=60.0, gt=0.0)

    # --- crawl bounds -------------------------------------------------------
    max_depth: int = Field(default=2, ge=0)
    max_pages: int = Field(default=200, ge=1)

    # --- snapshots ----------------------------------------------------------
    store_snapshots: bool = True
    snapshot_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)

    # --- service ------------------------------------------------------------
    api_title: str = "JobScout API"
    metrics_enabled: bool = True
    dashboard_api_url: str = "http://localhost:8000"

    # --- observability ------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- tests --------------------------------------------------------------
    test_database_url: str | None = None

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("user_agent")
    @classmethod
    def _require_contactable_agent(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user_agent must not be empty")
        return value.strip()

    @property
    def sync_database_url(self) -> str:
        """Sync SQLAlchemy URL, used by Alembic and the CLI.

        Derived from :attr:`database_url` when not set explicitly, so a single
        ``JOBSCOUT_DATABASE_URL`` is enough to run migrations.
        """
        if self.database_url_sync:
            return self.database_url_sync
        prefix, _, _ = self.database_url.partition("://")
        driver = SYNC_DRIVERS.get(prefix)
        if driver is None:
            return self.database_url
        return f"{driver}://{self.database_url.split('://', 1)[1]}"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
