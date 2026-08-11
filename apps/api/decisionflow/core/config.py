"""Application settings, loaded once from the environment.

Everything configurable lives here. Modules import the `settings` singleton
rather than reading os.environ directly, so there is exactly one place where a
missing or malformed value can blow up — at import time, loudly, instead of
deep inside a request handler.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# .../apps/api/decisionflow/core/config.py -> repo root is 4 levels up from the
# package directory.
API_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = API_ROOT.parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", API_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Runtime ----------------------------------------------------------
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False

    # --- Security ---------------------------------------------------------
    secret_key: SecretStr
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    jwt_algorithm: str = "HS256"
    # Defaults on. Turned off only for end-to-end runs, which sign in many
    # times in quick succession from one address and would otherwise be
    # throttled by a control that is already covered by its own unit tests.
    # Never disable this in a deployed environment.
    rate_limit_enabled: bool = True

    # --- Postgres ---------------------------------------------------------
    postgres_user: str = "decisionflow"
    postgres_password: SecretStr = SecretStr("decisionflow")
    postgres_db: str = "decisionflow"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # Non-owner runtime role. RLS policies do not apply to a table's owner,
    # so the API must never connect as `postgres_user`.
    app_db_user: str = "decisionflow_app"
    app_db_password: SecretStr = SecretStr("decisionflow_app")

    # --- Redis ------------------------------------------------------------
    redis_host: str = "localhost"
    redis_port: int = 6379
    # Logical database index. The test suite overrides this so its jobs and
    # rate-limit counters never land in the queue a running worker is draining
    # — otherwise a dev worker picks up jobs for a test database that has since
    # been dropped, and fails them noisily.
    redis_db: int = 0

    # --- Object storage ---------------------------------------------------
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: SecretStr = SecretStr("minioadmin")
    s3_secret_key: SecretStr = SecretStr("minioadmin")
    s3_bucket: str = "decisionflow-uploads"
    s3_region: str = "us-east-1"

    # --- Data plane -------------------------------------------------------
    duckdb_storage_path: Path = Path("./storage/duckdb")
    max_upload_bytes: int = 200 * 1024 * 1024

    # --- LLM --------------------------------------------------------------
    # Verified callable against a live key on 2026-08-09. Note that the models
    # *list* endpoint is not proof of access: gemini-2.5-flash is still listed
    # but returns 404 "no longer available to new users" when called, so model
    # choice here is based on actually invoking each candidate.
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.6-flash"
    # Deliberately also a Flash model. Pro is quota-restricted on the current
    # key (immediate 429 on every Pro variant), and SQL generation does not
    # need it. The provider falls back to `gemini_model` if this one is
    # throttled, so raising this to a Pro model later needs no code change.
    gemini_reasoning_model: str = "gemini-3.6-flash"
    gemini_timeout_seconds: int = 60

    # --- Frontend ---------------------------------------------------------
    # Kept as a raw string: pydantic-settings tries to JSON-decode env values
    # for list-typed fields, which turns a plain comma-separated value into a
    # confusing validation error. Parsed by `cors_origin_list` instead.
    cors_origins: str = "http://localhost:3000"

    # These are plain properties, never `computed_field`. A computed field is
    # included in `model_dump()`, which would serialise the fully-formed DSNs —
    # passwords and all — in cleartext, defeating the SecretStr wrappers above
    # the moment anything logs or reports the settings object.
    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def duckdb_root(self) -> Path:
        """Absolute path to the per-workspace DuckDB store."""
        path = self.duckdb_storage_path
        if not path.is_absolute():
            path = REPO_ROOT / path
        return path.resolve()

    def _dsn(self, user: str, password: SecretStr) -> str:
        return (
            f"postgresql+asyncpg://{user}:{quote_plus(password.get_secret_value())}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url(self) -> str:
        """DSN used by the API at runtime — the RLS-constrained, non-owner role."""
        return self._dsn(self.app_db_user, self.app_db_password)

    @property
    def migration_database_url(self) -> str:
        """DSN used by Alembic — the owner role, the only one that may run DDL.

        Deliberately the same asyncpg driver as the runtime DSN so the project
        carries one Postgres driver rather than two.
        """
        return self._dsn(self.postgres_user, self.postgres_password)

    @property
    def redis_dsn(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def llm_configured(self) -> bool:
        return bool(self.gemini_api_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
