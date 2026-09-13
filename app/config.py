from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./radar.db"
    app_secret_key: str = "change-me-before-production"
    youtube_api_key: str = ""
    youtube_region: str = "RU"
    youtube_default_language: str = "ru"
    youtube_max_results: int = 25
    ai_api_key: str = ""

    scheduler_enabled: bool = True
    search_interval_hours: int = 12
    metrics_interval_hours: int = 6

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
