import asyncio
import logging

import httpx
import pytest
from aiogram.exceptions import TelegramNetworkError
from bson import ObjectId

from app import repositories as repo
from app import main as app_main
from app.services import analysis_service as svc
from app.services.ai_service import AIError, AIService, ModelUnavailableError, validate_result
from app.services.resume_parser import ResumeParseError, ResumeParser
from app.services.scraper import HHError, HHScraper, build_hh_query


def test_hh_query():
    assert build_hh_query(['Python "стажер"', "Разнорабочий", " "]) == "(Python стажер) OR (Разнорабочий)"


@pytest.mark.parametrize("location", ["Ташкент", "ташкент"])
def test_hh_scraper_searches_real_vacancies(monkeypatch, location):
    scraper = HHScraper()
    request_params = {}
    saved = []

    async def get(_path, *, params):
        request_params.update(params)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "123",
                        "name": "Python-разработчик",
                        "employer": {"name": "Компания"},
                        "snippet": {"requirement": "Опыт Python", "responsibility": "Разработка"},
                        "alternate_url": "https://hh.uz/vacancy/123",
                        "key_skills": [{"name": "Python"}],
                    }
                ]
            },
        )

    async def save(vacancy):
        saved.append(vacancy)
        return {"_id": ObjectId(), **vacancy}

    monkeypatch.setattr(scraper._client, "get", get)
    monkeypatch.setattr(repo, "save_vacancy", save)

    async def run():
        try:
            return await scraper.search(
                {"keywords": ["Python"], "location": location, "employment_type": "полная"}
            )
        finally:
            await scraper.aclose()

    results = asyncio.run(run())
    assert request_params["area"] == "2759"
    assert request_params["employment"] == "full"
    assert results[0]["source_id"] == "hh_123"
    assert results[0]["skills"] == ["Python"]
    assert len(saved) == 1


def test_hh_scraper_loads_full_vacancy_details(monkeypatch):
    scraper = HHScraper()

    async def get(path):
        assert path == "/vacancies/123"
        return httpx.Response(
            200,
            json={
                "name": "Python-разработчик",
                "description": "<p>Разработка API</p><ul><li>Python</li></ul>",
                "employer": {"name": "Компания"},
                "address": {"raw": "Ташкент"},
                "experience": {"name": "1–3 года"},
                "employment": {"name": "Полная занятость"},
                "schedule": {"name": "Полный день"},
                "key_skills": [{"name": "Python"}],
                "professional_roles": [{"name": "Программист"}],
                "alternate_url": "https://hh.uz/vacancy/123",
            },
        )

    monkeypatch.setattr(scraper._client, "get", get)

    async def run():
        try:
            return await scraper.get_vacancy_details({
                "source_id": "hh_123", "title": "Вакансия", "skills": [],
            })
        finally:
            await scraper.aclose()

    details = asyncio.run(run())
    assert details["description"] == "Разработка API\nPython"
    assert details["experience"] == "1–3 года"
    assert details["professional_roles"] == ["Программист"]


def test_hh_scraper_explains_forbidden_response(monkeypatch, caplog):
    scraper = HHScraper()

    async def get(_path, *, params):
        return httpx.Response(403, json={"errors": [{"type": "forbidden"}], "request_id": "test-request-id"})

    monkeypatch.setattr(scraper._client, "get", get)

    async def run():
        try:
            await scraper.search({"keywords": ["Python"], "location": "Ташкент"})
        finally:
            await scraper.aclose()

    with caplog.at_level(logging.ERROR, logger="app.services.scraper"):
        with pytest.raises(HHError, match="HH_USER_AGENT.*test-request-id"):
            asyncio.run(run())

    assert "status=403 reason=forbidden" in caplog.text
    assert "request_id=test-request-id" in caplog.text


def test_ai_service_uses_fallback_model(monkeypatch):
    service = AIService()
    service._models = ["unavailable", "working"]
    called = []

    async def complete(model, _prompt, **_kw):
        called.append(model)
        if model == "unavailable":
            raise ModelUnavailableError("model unavailable")
        return '{"match_score": 80, "missing_skills": [], "cover_letter": "' + "x" * 60 + '"}'

    monkeypatch.setattr(service, "_complete", complete)

    async def run():
        try:
            return await service.analyze("резюме", {"title": "Вакансия"})
        finally:
            await service.aclose()

    result = asyncio.run(run())
    assert called == ["unavailable", "working"]
    assert result["match_score"] == 80


def test_validate_result_ok_and_clamp():
    r = validate_result({"match_score": 140, "missing_skills": ["FastAPI", "fastapi", " "], "cover_letter": "x" * 60})
    assert r["match_score"] == 100 and r["missing_skills"] == ["FastAPI"]


@pytest.mark.parametrize(
    "bad",
    [{"match_score": 5, "missing_skills": "git", "cover_letter": "x" * 60},
     {"match_score": 5, "missing_skills": [], "cover_letter": "коротко"},
     {"missing_skills": [], "cover_letter": "x" * 60}],
)
def test_validate_result_rejects(bad):
    with pytest.raises((ValueError, KeyError, TypeError)):
        validate_result(bad)


def test_resume_parser_txt_cp1251_and_errors():
    p = ResumeParser()
    text = ("Опыт работы: Python, SQL, Git. " * 5).encode("cp1251")
    assert "Python" in asyncio.run(p.parse(text, "cv.txt"))
    with pytest.raises(ResumeParseError):
        asyncio.run(p.parse(b"x", "cv.exe"))
    with pytest.raises(ResumeParseError):
        asyncio.run(p.parse(b"short", "cv.txt"))


def test_analysis_flow_reuses_cache_and_reports_errors(monkeypatch):
    user = {"_id": ObjectId(), "resume_text": "резюме " * 20, "filters": {"keywords": ["py"], "location": "Ташкент", "employment_type": "полная"}}
    vacs = [{"_id": ObjectId(), "source_id": f"hh_{i}", "title": f"V{i}", "company": "C"} for i in range(4)]
    saved: dict = {}

    async def get_user(_tid): return user
    async def get_map(_uid, ids, _h): return {k: v for k, v in saved.items() if k in ids}
    async def save(_uid, vid, data):
        saved[vid] = {"vacancy_id": vid, **data}
        return saved[vid]

    monkeypatch.setattr(repo, "get_or_create_user", get_user)
    monkeypatch.setattr(repo, "get_analyses_map", get_map)
    monkeypatch.setattr(repo, "save_analysis", save)

    class FakeScraper:
        async def search(self, _f): return vacs

    class FakeAI:
        calls = 0
        async def analyze(self, _r, v):
            FakeAI.calls += 1
            if v["title"] == "V1":
                raise AIError("boom")
            return {"match_score": 80, "missing_skills": ["Git"], "cover_letter": "x" * 60}

    service = svc.AnalysisService(FakeAI(), FakeScraper())

    async def collect(on_plan=None):
        return [i async for i in service.search(1, on_plan)]

    plans = []
    async def on_plan(n, c): plans.append((n, c))

    first = asyncio.run(collect(on_plan))
    assert plans[-1] == (4, 0) and len(first) == 4
    assert sum(1 for i in first if i.analysis is None) == 1          # V1 упала, но остальные дошли
    assert FakeAI.calls == 4 and not service._busy                   # lock снят

    calls_before = FakeAI.calls
    second = asyncio.run(collect(on_plan))                           # V1 не сохранена -> анализируется заново
    assert FakeAI.calls == calls_before + 1
    assert plans[-1] == (1, 0) and len(second) == 1


def test_profile_error(monkeypatch):
    async def get_user(_t): return {"_id": ObjectId(), "resume_text": "", "filters": {"keywords": []}}
    monkeypatch.setattr(repo, "get_or_create_user", get_user)
    service = svc.AnalysisService(object(), object())

    async def run():
        async for _ in service.search(1):
            pass

    with pytest.raises(svc.ProfileError):
        asyncio.run(run())
    assert not service._busy


def test_long_polling_retries_telegram_network_error(monkeypatch, caplog):
    class FakeBot:
        attempts = 0

        async def delete_webhook(self, *, drop_pending_updates):
            assert drop_pending_updates is False
            self.attempts += 1
            if self.attempts == 1:
                raise TelegramNetworkError(method="deleteWebhook", message="connection reset")

    class FakeDispatcher:
        started = False

        def resolve_used_update_types(self):
            return []

        async def start_polling(self, *_args, **_kwargs):
            self.started = True

    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(app_main.asyncio, "sleep", fake_sleep)
    bot, dispatcher = FakeBot(), FakeDispatcher()

    with caplog.at_level(logging.WARNING, logger="app.main"):
        asyncio.run(app_main._run_long_polling(bot, dispatcher))

    assert bot.attempts == 2
    assert dispatcher.started
    assert delays == [5]
    assert "Нет соединения с Telegram API" in caplog.text


def test_mongo_connection_retries_until_ready(monkeypatch):
    attempts = []
    delays = []

    async def fake_connect():
        attempts.append(None)
        return len(attempts) == 2

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(app_main.mongo, "connect", fake_connect)
    monkeypatch.setattr(app_main.asyncio, "sleep", fake_sleep)
    ready = asyncio.Event()

    asyncio.run(app_main._connect_mongo_with_retry(ready))

    assert ready.is_set()
    assert len(attempts) == 2
    assert delays == [5]
