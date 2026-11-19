from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все секреты читаются только из переменных окружения / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    mongodb_uri: str
    mongodb_db: str = "job_assistant"

    openrouter_api_key: str
    openrouter_model: str = "google/gemma-4-31b-it:free"
    openrouter_fallback_model: str = "google/gemma-4-26b-a4b-it:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # обязателен: hh.ru блокирует анонимные запросы
    hh_user_agent: str

    webhook_base_url: str = ""
    webhook_secret: str = ""

    max_vacancies_per_search: int = 5   # сколько карточек показываем за один поиск
    hh_candidates: int = 20             # сколько вакансий запрашиваем у hh.ru (с запасом на дубликаты)
    min_match_score: int = 40           # вакансии с оценкой ИИ ниже этого порога не показываем (0 = показывать все)
    ai_concurrency: int = 3             # одновременные запросы к ИИ (free-модели быстро упираются в лимиты)
    web_enabled: bool = True            # веб-интерфейс на / и API на /api
    web_searches_per_hour: int = 10     # лимит поисков с одного IP (0 = без лимита)
    log_level: str = "INFO"

    @field_validator("mongodb_uri", "telegram_bot_token", "openrouter_api_key", mode="before")
    @classmethod
    def clean_secrets(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.strip().strip('"').strip("'")
            # на случай если при копировании из .env попало "MONGODB_URI=mongodb+srv://..."
            if "=" in v and v.split("=", 1)[0].isupper():
                v = v.split("=", 1)[1].strip().strip('"').strip("'")
        return v


settings = Settings()
