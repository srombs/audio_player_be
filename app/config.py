"""Environment-based application configuration."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NY_AUDIO_")

    auth_verification_cache_seconds: float = Field(default=60, ge=0, le=300)
    auth_request_timeout_seconds: float = Field(default=10, gt=0, le=120)

    issue_articles_path: Path = Path("data/issue-articles")
    issue_page_capture_path: Path = Path("data/issue-pages")
    issue_index_path: Path = Path("data/newyorker-issues.json")
    session_state_path: Path = Path("data/newyorker-session.json")
    http_verification_path: Path = Path("data/http-verification.json")
    http_verification_result_path: Path = Path("data/http-verification-last-result.json")
    discovery_capture_path: Path = Path("data/subscription-discovery-private.json")


@lru_cache
def get_settings() -> Settings:
    return Settings()
