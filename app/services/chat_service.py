"""Чат с ИИ строго о работе и карьере.

ИИ получает резюме и профиль кандидата, отвечает по теме и, если пользователь хочет увидеть вакансии,
сообщает термины для поиска. Вакансии ищутся на hh.ru по этим терминам, сверяются с резюме, и показываются
только подходящие. Вопросы не по теме получают фиксированный отказ: текст модели для них не используется.
"""
import logging
from dataclasses import dataclass, field

from app import repositories as repo
from app.demo_vacancies import DEMO_VACANCIES
from app.services.ai_service import OFF_TOPIC_REPLY, AIService
from app.services.analysis_service import AnalysisService, ProfileError
from app.services.scraper import HHError, HHScraper
from app.utils import format_salary

log = logging.getLogger(__name__)

MAX_MESSAGE = 500   # символов в одном сообщении пользователя
MAX_CARDS = 5       # вакансий в одном ответе


class ChatInputError(Exception):
    """Сообщение пользователя не принято (пустое или слишком длинное)."""


@dataclass
class ChatResult:
    reply: str
    vacancies: list[dict] = field(default_factory=list)
    off_topic: bool = False
    notice: str = ""


def _card(vacancy: dict, score: int | None) -> dict:
    return {
        "id": str(vacancy["_id"]),
        "title": vacancy.get("title") or "",
        "company": vacancy.get("company") or "",
        "url": vacancy.get("url") or "",
        "skills": (vacancy.get("skills") or [])[:6],
        "salary": format_salary(vacancy.get("salary")),
        "match_score": score,
        "is_demo": False,
    }


def demo_cards(terms: list[str]) -> list[dict]:
    """Демонстрационные примеры, подходящие по терминам (запасной вариант, когда hh.ru недоступен)."""
    needles = [t.casefold() for t in terms if t]
    cards = []
    for v in DEMO_VACANCIES.values():
        haystack = " ".join([v["title"], *v.get("skills", [])]).casefold()
        if not needles or any(n in haystack or haystack_word_match(n, haystack) for n in needles):
            cards.append({
                "id": v["id"], "title": v["title"], "company": v.get("company") or "", "url": "",
                "skills": v.get("skills", [])[:6], "salary": "", "match_score": None, "is_demo": True,
            })
    return cards[:MAX_CARDS]


def haystack_word_match(needle: str, haystack: str) -> bool:
    """Совпадение по отдельному слову запроса («python backend» находит вакансию про Python)."""
    return any(len(word) > 2 and word in haystack for word in needle.split())


class ChatService:
    def __init__(self, ai: AIService, scraper: HHScraper, analysis: AnalysisService) -> None:
        self.ai = ai
        self.scraper = scraper
        self.analysis = analysis

    async def ask(self, user_key: int, text: str) -> ChatResult:
        """Исключения: ChatInputError, ProfileError (нет резюме), AIError (ИИ недоступен)."""
        text = " ".join((text or "").split())
        if not text:
            raise ChatInputError("Напишите вопрос.")
        if len(text) > MAX_MESSAGE:
            raise ChatInputError(f"Слишком длинное сообщение: не больше {MAX_MESSAGE} символов.")

        user = await repo.get_or_create_user(user_key)
        if not (user.get("resume_text") or "").strip():
            raise ProfileError("Сначала добавьте резюме: ИИ отвечает с учётом вашего резюме.")

        profile = await self.analysis.ensure_profile(user)  # сначала ИИ читает резюме
        history = [
            {"role": m.get("role"), "text": m.get("text", "")}
            for m in await repo.get_chat_history(user["_id"], 8)
        ]
        answer = await self.ai.chat(profile, user["resume_text"], history, text)

        if not answer["on_topic"]:
            # текст отказа наш, а не модели; сам вопрос в историю не попадает и контекст не засоряет
            return ChatResult(OFF_TOPIC_REPLY, off_topic=True)

        reply = answer["reply"]
        cards: list[dict] = []
        notice = ""
        if answer["search"]:
            cards, notice = await self._find_vacancies(user, profile, answer["search"])
            if not reply:
                reply = "Вот что нашёл по вашему запросу." if cards else "Подходящих вакансий по запросу не нашёл."

        await repo.add_chat_messages(
            user["_id"],
            [
                {"role": "user", "text": text},
                {"role": "assistant", "text": reply, "vacancies": cards, "notice": notice},
            ],
        )
        return ChatResult(reply, cards, notice=notice)

    async def _find_vacancies(self, user: dict, profile: dict | None, search: dict) -> tuple[list[dict], str]:
        filters = user.get("filters") or {}
        terms = search["terms"] or (profile or {}).get("search_queries") or filters.get("keywords") or []
        if not terms:
            return [], "Не понял, какие вакансии искать. Назовите должность или технологию."

        query = {
            "keywords": terms,
            "location": filters.get("location"),
            "employment_type": filters.get("employment_type"),
            "high_salary": bool(search.get("high_salary")),
        }
        try:
            vacancies = await self.scraper.search(query)
        except HHError as e:
            log.warning("Чат: hh.ru недоступен: %s", e)
            demo = demo_cards(terms)
            return demo, (
                "hh.ru сейчас недоступен. "
                + ("Показаны демонстрационные примеры по вашим терминам: это не реальные вакансии."
                   if demo else "Демонстрационных примеров по этим терминам нет.")
            )
        except Exception:  # noqa: BLE001
            log.exception("Чат: ошибка поиска вакансий")
            return [], "Не удалось получить вакансии. Попробуйте позже."

        if not vacancies:
            return [], "По этим терминам на hh.ru ничего не нашлось."

        ranking = await self.analysis.rank_vacancies(profile, user, vacancies)
        fit = {v["_id"] for v in ranking.ranked}
        if query["high_salary"]:
            picked = [v for v in vacancies if v["_id"] in fit]  # hh.ru уже отсортировал по зарплате
        else:
            picked = ranking.ranked
        cards = [_card(v, ranking.scores.get(v["_id"])) for v in picked[:MAX_CARDS]]

        if not ranking.screened:
            return cards, "Не удалось сверить вакансии с резюме: показаны найденные по вашим терминам."
        if not cards:
            return [], "Вакансии нашлись, но ни одна не подходит под ваше резюме."
        return cards, ""

    async def history(self, user_key: int) -> list[dict]:
        user = await repo.get_or_create_user(user_key)
        docs = await repo.get_chat_history(user["_id"], 30)
        return [
            {
                "role": m.get("role"),
                "text": m.get("text", ""),
                "vacancies": m.get("vacancies") or [],
                "notice": m.get("notice") or "",
            }
            for m in docs
        ]

    async def clear(self, user_key: int) -> None:
        user = await repo.get_or_create_user(user_key)
        await repo.clear_chat(user["_id"])
