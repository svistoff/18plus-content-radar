from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./radar.db"
    app_secret_key: str = "change-me-before-production"
    youtube_api_key: str = ""
    youtube_region: str = "RU"
    youtube_default_language: str = "ru"
    youtube_max_results: int = 25
    ai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"
    ai_base_url: str = ""

    # Транскрипты через Apify (обходит блокировку IP, дёшево за субтитры).
    apify_token: str = ""
    apify_actor: str = "pintostudio~youtube-transcript-scraper"

    # Аудио-fallback (когда субтитров нет): OpenAI-совместимый STT.
    # Пусто = использовать ai_api_key/ai_base_url. Для Groq: base_url
    # https://api.groq.com/openai/v1 и модель whisper-large-v3-turbo.
    whisper_fallback: bool = True
    whisper_model: str = "whisper-1"
    transcribe_api_key: str = ""
    transcribe_base_url: str = ""

    scheduler_enabled: bool = True
    search_interval_hours: int = 12
    metrics_interval_hours: int = 6
    watchlist_interval_hours: int = 24

    # Отсекаем Shorts и слишком короткие клипы (в них мало материала для статьи).
    min_duration_seconds: int = 180
    # Мягкий фильтр языка по метаданным видео (пусто = не фильтровать).
    allowed_languages: str = "ru,en"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_language_set(self) -> set[str]:
        return {x.strip().lower() for x in self.allowed_languages.split(",") if x.strip()}

    @field_validator("ai_model", mode="before")
    @classmethod
    def _default_ai_model(cls, value):
        # An empty AI_MODEL= line in an existing .env must not blank out the default.
        return value or "gpt-4o-mini"

    def transcribe_credentials(self) -> tuple[str, str]:
        """Credentials for the audio STT step: dedicated keys if set, else the
        main AI ones (so plain OpenAI works with no extra config)."""
        return (self.transcribe_api_key or self.ai_api_key, self.transcribe_base_url or self.ai_base_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
