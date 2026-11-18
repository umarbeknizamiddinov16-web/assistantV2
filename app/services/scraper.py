import logging
import re
from datetime import datetime, timezone

import httpx

from app import repositories as repo
from app.config import settings
from app.utils import strip_html

log = logging.getLogger(__name__)

HH_API_URL = "https://api.hh.ru"
HH_AREAS = {"Ташкент": "2759", "Узбекистан": "97"}
HH_EMPLOYMENT = {"полная": "full", "частичная": "part", "стажировка": "probation"}


class HHError(Exception):
    """Ошибка поиска вакансий на hh.ru."""


def build_hh_query(keywords: list[str]) -> str:
    terms = [re.sub(r'["“”]', "", keyword).strip() for keyword in keywords]
    return " OR ".join(f"({term})" for term in terms if term)


class HHScraper:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=HH_API_URL,
            timeout=httpx.Timeout(25.0),
            headers={
                "User-Agent": settings.hh_user_agent,
                "Accept": "application/json",
            },
        )

    async def search(self, filters: dict, limit: int | None = None) -> list[dict]:
        query = build_hh_query(filters.get("keywords") or [])
        if not query:
            return []

        per_page = settings.hh_candidates if limit is None else limit
        if per_page <= 0:
            return []
        per_page = min(per_page, 100)

        params: dict[str, str | int] = {
            "text": query,
            "per_page": per_page,
            "page": 0,
        }
        location = (filters.get("location") or "").strip()
        area = next(
            (area_id for name, area_id in HH_AREAS.items() if name.casefold() == location.casefold()),
            None,
        )
        if area:
            params["area"] = area
        elif location:
            params["text"] = f"{query} {location}"

        employment = HH_EMPLOYMENT.get((filters.get("employment_type") or "").strip().lower())
        if employment:
            params["employment"] = employment

        if filters.get("high_salary"):  # «где больше платят»: только вакансии с зарплатой, сначала самые высокие
            params["only_with_salary"] = "true"
            params["order_by"] = "salary_desc"

        try:
            response = await self._client.get("/vacancies", params=params)
        except httpx.HTTPError as e:
            raise HHError(f"Не удалось связаться с hh.ru: {e}") from e
        if response.status_code == 403:
            payload = {}
            try:
                body = response.json()
                if isinstance(body, dict):
                    payload = body
            except ValueError:
                pass

            request_id = payload.get("request_id")
            errors = payload.get("errors") or []
            reason = ", ".join(
                str(error["type"])
                for error in errors
                if isinstance(error, dict) and error.get("type")
            ) or "unknown"
            log.error(
                "HH API rejected request: status=403 reason=%s area=%s "
                "user_agent_configured=%s request_id=%s",
                reason,
                area or "not_set",
                bool(settings.hh_user_agent.strip()),
                request_id or "not_provided",
            )
            message = (
                f"HH API отклонил запрос (403: {reason}). Бот работает, но сервер HH запретил запрос. "
                "Проверьте, что HH_USER_AGENT содержит название приложения и действующий контактный email. "
                "Если данные верны, передайте ID запроса в поддержку HH для проверки ограничений API или сети."
            )
            if request_id:
                message += f" ID запроса: {request_id}."
            raise HHError(message)
        if response.is_error:
            raise HHError(f"hh.ru вернул HTTP {response.status_code}: {response.text[:200]}")

        try:
            items = response.json()["items"]
            if not isinstance(items, list):
                raise TypeError("items должен быть массивом")
        except (ValueError, KeyError, TypeError) as e:
            raise HHError("hh.ru вернул некорректный ответ.") from e

        vacancies = []
        for item in items[:per_page]:
            snippet = item.get("snippet") or {}
            requirements = snippet.get("requirement") or ""
            responsibilities = snippet.get("responsibility") or ""
            employer = item.get("employer") or {}
            vacancies.append(
                await repo.save_vacancy(
                    {
                        "source_id": f"hh_{item['id']}",
                        "title": item["name"],
                        "company": employer.get("name") or "",
                        "requirements": requirements,
                        "responsibilities": responsibilities,
                        "description": "\n".join(filter(None, (requirements, responsibilities))),
                        "skills": [
                            skill["name"]
                            for skill in item.get("key_skills", [])
                            if skill.get("name")
                        ],
                        "url": item.get("alternate_url") or "",
                        "salary": item.get("salary"),
                        "scraped_at": datetime.now(timezone.utc),
                    }
                )
            )
        log.info("hh.ru: найдено вакансий: %d", len(vacancies))
        return vacancies

    async def get_vacancy_details(self, vacancy: dict) -> dict | None:
        source_id = vacancy.get("source_id") or ""
        match = re.fullmatch(r"hh_(\d+)", source_id)
        if not match:
            return None

        try:
            response = await self._client.get(f"/vacancies/{match.group(1)}")
        except httpx.HTTPError as e:
            raise HHError(f"Не удалось связаться с hh.ru: {e}") from e
        if response.status_code == 404:
            return None
        if response.is_error:
            raise HHError(f"hh.ru вернул HTTP {response.status_code}: {response.text[:200]}")

        try:
            item = response.json()
            if not isinstance(item, dict):
                raise TypeError("ответ должен быть объектом")
        except (ValueError, TypeError) as e:
            raise HHError("hh.ru вернул некорректное описание вакансии.") from e

        employer = item.get("employer") or {}
        address = item.get("address") or {}
        salary = item.get("salary")
        return {
            "title": item.get("name") or vacancy.get("title") or "",
            "company": employer.get("name") or vacancy.get("company") or "",
            "description": strip_html(item.get("description") or ""),
            "skills": [
                skill["name"]
                for skill in item.get("key_skills", [])
                if isinstance(skill, dict) and skill.get("name")
            ] or vacancy.get("skills") or [],
            "location": address.get("raw") or (item.get("area") or {}).get("name") or "",
            "salary": salary,
            "experience": (item.get("experience") or {}).get("name") or "",
            "employment": (item.get("employment") or {}).get("name") or "",
            "schedule": (item.get("schedule") or {}).get("name") or "",
            "published_at": item.get("published_at") or "",
            "url": item.get("alternate_url") or vacancy.get("url") or "",
            "professional_roles": [
                role["name"]
                for role in item.get("professional_roles", [])
                if isinstance(role, dict) and role.get("name")
            ],
        }

    async def aclose(self) -> None:
        await self._client.aclose()
