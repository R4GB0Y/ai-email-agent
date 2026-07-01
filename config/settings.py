"""Typed application configuration loaded from environment variables.

All knobs a non-engineer might want to turn live here. Anything marked Field(...)
without a default is *required*; missing vars fail at startup, not at runtime.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


Tone = Literal["formal", "casual", "brief"]


class AppConfig(BaseSettings):
    # --- secrets (required, no defaults) ---
    openai_api_key: str = Field(..., description="OpenAI API key")
    slack_webhook_url: str = Field(..., description="Slack incoming webhook URL")
    gmail_credentials_path: str = Field("config/credentials.json")

    # --- user-tunable behavior ---
    categories: list[str] = Field(
        default=["urgent", "client", "internal", "newsletter", "spam"],
        description="Comma-separated list of classification categories.",
    )
    response_tone: Tone = Field(default="formal")
    max_emails_per_run: int = Field(default=20, ge=1, le=100)
    min_priority_to_surface: Literal["low", "medium", "high"] = Field(default="medium")
    schedule_interval_minutes: int = Field(default=15, ge=1, le=1440)

    # --- runtime knobs ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    env: Literal["dev", "prod"] = Field(default="dev")
    run_mode: Literal["pipeline", "server"] = Field(default="pipeline")
    port: int = Field(default=8080, ge=1, le=65535)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,   # so OPENAI_API_KEY matches openai_api_key
        extra="ignore",         # don't choke on unrelated env vars on Railway
    )

    @field_validator("categories", mode="before")
    @classmethod
    def _split_categories(cls, v):
        """Allow CATEGORIES='urgent,client,internal' as a single env string."""
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v


# Lazy singleton — don't load on import (that would make tests a pain).
_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()
    return _config

def reset_config_for_testing() -> None:
    """Tests call this after patching env vars."""
    global _config
    _config = None