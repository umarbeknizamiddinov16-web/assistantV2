import os
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все секреты читаются только из переменных окружения / .env."""

    model_config = SettingsConfigDict(
        env_file=None if os.getenv("VERCEL") else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str
    mongodb_uri: str = Field(validation_alias=AliasChoices("DB_CONNECTION_STRING", "MONGODB_URI"))
    mongodb_db: str = "job_assistant"

    openrouter_api_key: str
    openrouter_model: str = "google/gemma-4-31b-it:free"
    openrouter_fallback_model: str = "google/gemma-4-26b-a4b-it:free"
    openrouter_base_url: str = "https://openrouter.ai"

    # обязателен: hh.ru блокирует анонимные запросы
    hh_user_agent: str

    webhook_base_url: str = ""
    webhook_secret: str = ""

    max_vacancies_per_search: int = 5
    hh_candidates: int = 20
    min_match_score: int = 40
    ai_concurrency: int = 3
    web_enabled: bool = True
    web_searches_per_hour: int = 10
    log_level: str = "INFO"

    @field_validator("mongodb_uri", "telegram_bot_token", "openrouter_api_key", mode="before")
    @classmethod
    def clean_secrets(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.strip().strip('"').strip("'")
            if "=" in v and v.split("=", 1)[0].isupper():
                v = v.split("=", 1)[1].strip().strip('"').strip("'")
        return v


settings = Settings()
