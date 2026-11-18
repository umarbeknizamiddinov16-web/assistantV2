import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from bson import ObjectId

from app import repositories as repo
from app.config import settings
from app.services.ai_service import AIError, AIService
from app.services.scraper import HHScraper

log = logging.getLogger(__name__)


class ProfileError(Exception):
    """Профиль не готов к поиску (нет резюме / должностей)."""


class SearchBusyError(Exception):
    """У пользователя уже идёт поиск."""


@dataclass
class AnalysisItem:
    vacancy: dict
    analysis: dict | None        # None, если анализ не удался
    cached: bool = False         # True, если результат взят из analysis_logs без обращения к ИИ
    error: str | None = None
    rejected: bool = False       # вакансия найдена, но ИИ счёл её неподходящей под резюме: не показывать


@dataclass
class Ranking:
    ranked: list[dict]           # подходящие под резюме, лучшие первыми
    rejected: list[dict]         # отсеянные: оценка ниже порога
    scores: dict                 # {vacancy _id: оценка 0-100}
    screened: bool               # False, если ИИ не смог оценить (тогда отбор не применялся)


PlanCallback = Callable[[int, int], Awaitable[None]]
StageCallback = Callable[[str], Awaitable[None]]

STAGE_READING = "Читаю ваше резюме…"
STAGE_SEARCHING = "Ищу вакансии на hh.ru…"
STAGE_MATCHING = "Подбираю подходящие под резюме…"


class AnalysisService:
    """Оркестратор: ИИ читает резюме -> вакансии hh.ru -> отбор по резюме -> подробный разбор пар «резюме-вакансия»."""

    def __init__(self, ai: AIService, scraper: HHScraper) -> None:
        self.ai = ai
        self.scraper = scraper
        self._sem = asyncio.Semaphore(settings.ai_concurrency)
        self._busy: set[int] = set()

    async def ensure_profile(self, user: dict) -> dict | None:
        """Шаг 1: ИИ читает резюме и составляет профиль кандидата. Читается один раз на версию резюме.

        None, если ИИ сейчас недоступен: тогда поиск и чат продолжат работать без профиля.
        """
        rhash = repo.resume_hash(user["resume_text"])
        if user.get("resume_profile") and user.get("resume_profile_hash") == rhash:
            return user["resume_profile"]
        try:
            profile = await self.ai.profile_resume(user["resume_text"])
        except AIError as e:
            log.warning("ИИ не смог прочитать резюме: %s", e)
            return None
        except Exception:  # noqa: BLE001
            log.exception("Неожиданная ошибка при чтении резюме")
            return None
        try:
            await repo.save_resume_profile(user["_id"], rhash, profile)
        except Exception:  # noqa: BLE001 — профиль не критичен, просто прочитаем заново в следующий раз
            log.exception("Не удалось сохранить профиль резюме")
        return profile

    async def rank_vacancies(
        self, profile: dict | None, user: dict, vacancies: list[dict], done: dict | None = None
    ) -> Ranking:
        """Шаг 3: одним запросом оценивает найденные вакансии по резюме и оставляет подходящие."""
        done = done or {}
        scores: dict = {v["_id"]: done[v["_id"]]["match_score"] for v in vacancies if v["_id"] in done}
        unscored = [v for v in vacancies if v["_id"] not in done]
        screened = True
        if unscored:
            raw: dict[int, int] = {}
            try:
                raw = await self.ai.screen_vacancies(profile, user["resume_text"], unscored)
            except AIError as e:
                log.warning("ИИ не смог отобрать вакансии: %s", e)
            except Exception:  # noqa: BLE001
                log.exception("Неожиданная ошибка отбора вакансий")
            if not raw:
                return Ranking(list(vacancies), [], scores, screened=False)  # без оценок оставляем порядок hh.ru
            for i, score in raw.items():
                scores[unscored[i]["_id"]] = score

        threshold = settings.min_match_score
        ranked, rejected = [], []
        for idx, v in enumerate(vacancies):
            score = scores.get(v["_id"])
            (rejected if score is not None and score < threshold else ranked).append((score, idx, v))
        # лучшие первыми; у вакансий, которые ИИ пропустил, оценки нет: они в конце и проверятся подробным разбором
        ranked.sort(key=lambda t: (t[0] is None, -(t[0] or 0), t[1]))
        return Ranking([v for *_, v in ranked], [v for *_, v in rejected], scores, screened)

    async def search(
        self,
        telegram_id: int,
        on_plan: PlanCallback | None = None,
        on_stage: StageCallback | None = None,
    ) -> AsyncIterator[AnalysisItem]:
        """Асинхронно отдаёт результаты по мере готовности.

        Порядок: ИИ читает резюме -> поиск на hh.ru по вашим должностям -> отбор по резюме -> подробный разбор лучших.
        on_stage(text) сообщает, на каком этапе поиск. on_plan(n_to_analyze, n_from_cache) вызывается один раз
        до начала подробного разбора. Отсеянные вакансии приходят как AnalysisItem(rejected=True).
        Исключения: ProfileError, SearchBusyError, HHError.
        """
        if telegram_id in self._busy:
            raise SearchBusyError
        self._busy.add(telegram_id)
        tasks: list[asyncio.Task] = []

        async def stage(text: str) -> None:
            if on_stage:
                await on_stage(text)

        try:
            user = await repo.get_or_create_user(telegram_id)
            if not (user.get("resume_text") or "").strip():
                raise ProfileError("Сначала добавьте резюме: «📄 Резюме».")
            if not user["filters"].get("keywords"):
                raise ProfileError("Сначала задайте должности в «⚙️ Фильтры».")

            rhash = repo.resume_hash(user["resume_text"])
            await stage(STAGE_READING)
            profile = await self.ensure_profile(user)

            await stage(STAGE_SEARCHING)
            vacancies = await self.scraper.search(user["filters"])
            done = await repo.get_analyses_map(user["_id"], [v["_id"] for v in vacancies], rhash)

            await stage(STAGE_MATCHING)
            ranking = await self.rank_vacancies(profile, user, vacancies, done)

            limit = settings.max_vacancies_per_search
            fresh = [v for v in ranking.ranked if v["_id"] not in done][:limit]
            cached = [] if fresh else [AnalysisItem(v, done[v["_id"]], cached=True) for v in ranking.ranked[:limit]]

            if on_plan:
                await on_plan(len(fresh), len(cached))

            for v in ranking.rejected:
                yield AnalysisItem(v, None, rejected=True)
            for item in cached:
                yield item

            tasks = [asyncio.create_task(self._analyze_one(user, rhash, v)) for v in fresh]
            for fut in asyncio.as_completed(tasks):
                item = await fut
                if item.analysis and item.analysis["match_score"] < settings.min_match_score:
                    item.rejected = True  # подробный разбор показал, что вакансия не подходит
                yield item
        finally:
            for t in tasks:
                t.cancel()
            self._busy.discard(telegram_id)

    async def _analyze_one(self, user: dict, rhash: str, vacancy: dict) -> AnalysisItem:
        async with self._sem:
            try:
                result = await self.ai.analyze(user["resume_text"], vacancy)
            except AIError as e:
                log.error("Анализ вакансии %s не удался: %s", vacancy["source_id"], e)
                return AnalysisItem(vacancy, None, error=str(e))
            except Exception as e:  # noqa: BLE001
                log.exception("Неожиданная ошибка анализа %s", vacancy["source_id"])
                return AnalysisItem(vacancy, None, error=str(e))
        doc = await repo.save_analysis(
            user["_id"],
            vacancy["_id"],
            {
                "match_score": result["match_score"],
                "missing_skills": result["missing_skills"],
                "generated_cover_letter": result["cover_letter"],
                "resume_hash": rhash,
                "model": settings.openrouter_model,
            },
        )
        return AnalysisItem(vacancy, doc)

    async def get_cover_letter(self, telegram_id: int, vacancy_id: ObjectId) -> tuple[dict, dict] | None:
        """Готовое письмо из analysis_logs (без обращения к ИИ). None, если анализа нет или резюме сменилось."""
        user = await repo.get_or_create_user(telegram_id)
        vacancy = await repo.get_vacancy(vacancy_id)
        if not vacancy or not user.get("resume_text"):
            return None
        analysis = await repo.get_analysis(user["_id"], vacancy_id, repo.resume_hash(user["resume_text"]))
        return (vacancy, analysis) if analysis else None
