"""
┌─ FILE: job_bot/core/config.py
├─ PURPOSE: Load and validate all environment variables; expose a typed, cached Settings singleton.
├─ USED BY: main.py, core/ai_scorer.py, core/browser.py, core/dashboard_launcher.py
├─ DATA FLOW: .env file → pydantic-settings → Settings object → rest of the app
├─ DESIGN DECISIONS: gemini_api_key stored as str="" (not Field(...)) so we can
│                    produce the exact [config.py:load_env] error message on failure
│                    instead of a raw pydantic ValidationError trace.
│                    @lru_cache ensures .env is parsed exactly once per process.
└─ PATTERNS: Singleton via @lru_cache, fail-fast on fatal misconfiguration
"""

import logging
import sys
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """
    All runtime configuration sourced from environment variables / .env file.

    Every field maps 1-to-1 with a key in .env.example.
    Unknown env vars are silently ignored (extra="ignore").
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── AI ────────────────────────────────────────────────────────────────────
    gemini_api_key: str = ""  # validated post-load for a human-readable error

    # ── Job portal credentials (all optional) ─────────────────────────────────
    # Blank username AND password → scraper skips login for that portal
    naukri_username: str = ""
    naukri_password: str = ""

    linkedin_username: str = ""
    linkedin_password: str = ""

    indeed_username: str = ""
    indeed_password: str = ""

    wellfound_username: str = ""
    wellfound_password: str = ""

    # ── Browser behaviour ─────────────────────────────────────────────────────
    headless_mode: bool = False  # false → visible Chromium (required for manual-login fallback)
    min_delay_ms: int = Field(default=800, ge=0)
    max_delay_ms: int = Field(default=2500, ge=0)
    login_wait_timeout_seconds: int = Field(default=300, ge=30)

    @field_validator("max_delay_ms", mode="after")
    @classmethod
    def _max_gte_min(cls, v: int, info) -> int:  # type: ignore[override]
        min_val: int = info.data.get("min_delay_ms", 0)
        if v < min_val:
            raise ValueError(
                f"MAX_DELAY_MS ({v}) must be >= MIN_DELAY_MS ({min_val})"
            )
        return v

    # ── Convenience helpers ───────────────────────────────────────────────────

    def has_portal_credentials(self, portal: str) -> bool:
        """Return True if both username and password are set for *portal*."""
        username = getattr(self, f"{portal}_username", "")
        password = getattr(self, f"{portal}_password", "")
        return bool(username.strip() and password.strip())


@lru_cache(maxsize=1)
def load_env() -> Settings:
    """
    Parse .env, validate, and return the cached Settings singleton.

    This is the ONLY entry point for reading configuration.
    Exits the process (non-zero) if GEMINI_API_KEY is absent — that is the
    single fatal misconfiguration: without it the AI scoring stage cannot run
    and the whole pipeline is meaningless.

    All other missing/empty env vars are tolerated:
      - Missing portal credentials → that scraper skips login
      - Default values are used for delay/timeout settings
    """
    settings = Settings()

    if not settings.gemini_api_key.strip():
        # Use logger.error directly here (logger.py may not be set up yet)
        # so we match the exact format from the spec manually.
        logger.error(
            "[config.py:load_env] GEMINI_API_KEY missing from .env — cannot continue"
        )
        sys.exit(1)

    return settings
