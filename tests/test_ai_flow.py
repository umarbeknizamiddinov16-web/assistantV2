"""ИИ читает резюме до поиска, отбирает вакансии по резюме, чат отвечает строго по теме работы."""
import asyncio

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app import repositories as repo
from app.db import mongo
from app.services import analysis_service as svc
from app.services import chat_service as chat_mod
from app.services.ai_service import (
    CHAT_PROMPT, OFF_TOPIC_REPLY, PROFILE_PROMPT, SCREEN_PROMPT, AIError, AIService,
    validate_chat, validate_profile, validate_screen,
)
from app.services.scraper import HHError

PROFILE = {"summary": "Junior Python-разработчик, FastAPI и PostgreSQL.", "level": "junior",
           "roles": ["Python-разработчик"], "skills": ["Python", "FastAPI"], "search_queries": ["Python junior"]}
RESUME = "Python, FastAPI, PostgreSQL, pytest. " * 8


def run(coro):
    return asyncio.run(coro)


# ---------- проверка ответов модели ----------
def test_validate_profile_ok_and_rejects():
    p = validate_profile({"summary": "Python-разработчик с опытом API", "level": "Стажер", "roles": ["Backend", "backend"],
                          "skills": ["Python", " ", "SQL"], "search_queries": []})
    assert p["level"] == "стажёр" and p["roles"] == ["Backend"] and p["search_queries"] == ["Backend"] and p["skills"] == ["Python", "SQL"]
    for bad in ({"summary": "коротко", "roles": ["x"]}, {"summary": "достаточно длинное описание"}, {"roles": ["x"]}):
        with pytest.raises((ValueError, KeyError)):
            validate_profile(bad)


def test_validate_screen_maps_indexes_and_clamps():
    assert validate_screen({"scores": [{"n": 1, "score": 140}, {"n": "2", "score": -5}, {"n": 9, "score": 50}]}, 2) == {0: 100, 1: 0}
    for bad in ({"scores": []}, {"scores": [{"n": 7, "score": 1}]}, {"scores": "x"}):
        with pytest.raises((ValueError, KeyError, TypeError)):
            validate_screen(bad, 2)


def test_validate_chat_off_topic_drops_model_text():
    assert validate_chat({"on_topic": False, "reply": "Вот рецепт борща...", "search": {"terms": ["x"]}}) == {
        "on_topic": False, "reply": "", "search": None}
    assert validate_chat({"on_topic": "false", "reply": "стихи"})["on_topic"] is False
    ok = validate_chat({"on_topic": True, "reply": "Ищу.", "search": {"terms": ["Python", " ", "QA"], "high_salary": True}})
    assert ok["search"] == {"terms": ["Python", "QA"], "high_salary": True}
    assert validate_chat({"on_topic": True, "reply": "", "search": {"terms": ["QA"]}})["search"]["high_salary"] is False
    with pytest.raises(ValueError):
        validate_chat({"on_topic": True, "reply": "", "search": None})


# ---------- вызовы ИИ используют ключ и модели из настроек ----------
def make_service(answers: list[str]):
    service = AIService()
    service._models = ["main", "backup"]
    calls = []

    async def complete(model, prompt, **kw):
        calls.append({"model": model, "prompt": prompt, **kw})
        return answers.pop(0)

    service._complete = complete
    return service, calls


def test_ai_reads_resume_with_profile_prompt():
    service, calls = make_service(['{"summary": "Python-разработчик с опытом", "roles": ["Backend"], "skills": ["Python"], "search_queries": ["Python"]}'])
    profile = run(service.profile_resume("Мой опыт Python " * 10))
    assert profile["roles"] == ["Backend"]
    assert calls[0]["system"] == PROFILE_PROMPT and "<resume>" in calls[0]["prompt"] and "Мой опыт Python" in calls[0]["prompt"]


def test_ai_screens_numbered_vacancies_in_one_call():
    service, calls = make_service(['{"scores": [{"n": 1, "score": 80}, {"n": 2, "score": 10}]}'])
    vacancies = [{"title": "Python Dev", "company": "A", "skills": ["Python"]}, {"title": "Повар", "company": "B"}]
    assert run(service.screen_vacancies(PROFILE, RESUME, vacancies)) == {0: 80, 1: 10}
    assert len(calls) == 1 and calls[0]["system"] == SCREEN_PROMPT
    assert "1. Python Dev" in calls[0]["prompt"] and "2. Повар" in calls[0]["prompt"] and "Junior Python-разработчик" in calls[0]["prompt"]
    assert run(service.screen_vacancies(PROFILE, RESUME, [])) == {}


def test_ai_chat_prompt_has_rules_resume_history_and_trimmed_message():
    service, calls = make_service(['{"on_topic": true, "reply": "Ок", "search": null}'])
    history = [{"role": "user", "text": "привет"}, {"role": "assistant", "text": "Здравствуйте"}]
    run(service.chat(PROFILE, RESUME, history, "я" * 900))
    c = calls[0]
    assert c["system"] == CHAT_PROMPT and "ТОЛЬКО работу и карьеру" in CHAT_PROMPT
    assert "<resume>" in c["prompt"] and "Пользователь: привет" in c["prompt"] and "Ассистент: Здравствуйте" in c["prompt"]
    assert "я" * 500 in c["prompt"] and "я" * 501 not in c["prompt"]


def test_ai_chat_falls_back_to_second_model_and_reports_error():
    service = AIService()
    service._models = ["main", "backup"]
    used = []

    async def complete(model, prompt, **kw):
        used.append(model)
        if model == "main":
            raise AIError("лимит")
        return '{"on_topic": false}'

    service._complete = complete
    assert run(service.chat(None, RESUME, [], "рецепт борща"))["on_topic"] is False and used == ["main", "backup"]

    async def always_fail(model, prompt, **kw):
        raise AIError("нет связи")

    service._complete = always_fail
    with pytest.raises(AIError):
        run(service.chat(None, RESUME, [], "вакансии"))


# ---------- поиск: сначала резюме, потом hh.ru, потом отбор ----------
class World:
    def __init__(self, monkeypatch, scores, final=None, screen_error=False, profile_error=False, cached_profile=False):
        self.events, self.stages, self.plans = [], [], []
        self.vacs = [{"_id": ObjectId(), "source_id": f"hh_{t}", "title": t, "company": "C"} for t in scores]
        self.scores, self.final = scores, final or {}
        self.user = {"_id": ObjectId(), "resume_text": RESUME, "filters": {"keywords": ["py"], "location": "Ташкент", "employment_type": "полная"}}
        if cached_profile:
            self.user.update(resume_profile=PROFILE, resume_profile_hash=repo.resume_hash(RESUME))
        self.saved: dict = {}
        world = self

        async def get_user(_t): return world.user
        async def get_map(_u, ids, _h): return {k: v for k, v in world.saved.items() if k in ids}
        async def save(_u, vid, data):
            world.saved[vid] = {"vacancy_id": vid, **data}
            return world.saved[vid]
        async def save_profile(_u, rhash, profile):
            world.events.append("save_profile")
            world.user.update(resume_profile=profile, resume_profile_hash=rhash)

        monkeypatch.setattr(repo, "get_or_create_user", get_user)
        monkeypatch.setattr(repo, "get_analyses_map", get_map)
        monkeypatch.setattr(repo, "save_analysis", save)
        monkeypatch.setattr(repo, "save_resume_profile", save_profile)
        monkeypatch.setattr(svc.settings, "min_match_score", 40)
        monkeypatch.setattr(svc.settings, "max_vacancies_per_search", 5)

        class Scraper:
            async def search(self, _f):
                world.events.append("hh")
                return world.vacs

        class AI:
            async def profile_resume(self, resume):
                world.events.append("profile")
                if profile_error:
                    raise AIError("модель недоступна")
                return PROFILE

            async def screen_vacancies(self, profile, resume, vacancies):
                world.events.append("screen")
                world.profile_seen_by_screen = profile
                if screen_error:
                    raise AIError("лимит")
                return {i: world.scores[v["title"]] for i, v in enumerate(vacancies)}

            async def analyze(self, resume, v):
                world.events.append(f"analyze:{v['title']}")
                return {"match_score": world.final.get(v["title"], world.scores[v["title"]]), "missing_skills": [], "cover_letter": "x" * 60}

        self.service = svc.AnalysisService(AI(), Scraper())

    def search(self):
        async def on_plan(n, c): self.plans.append((n, c))
        async def on_stage(t): self.stages.append(t)

        async def collect():
            return [i async for i in self.service.search(1, on_plan, on_stage)]

        return run(collect())


def test_resume_is_read_before_hh_search_and_only_fitting_vacancies_are_analyzed(monkeypatch):
    w = World(monkeypatch, {"A": 90, "B": 20, "C": 60, "D": 10})
    items = w.search()
    order = [e for e in w.events if e != "save_profile"]
    assert order[:3] == ["profile", "hh", "screen"], "сначала ИИ читает резюме, потом идёт поиск и отбор"
    assert w.stages == [svc.STAGE_READING, svc.STAGE_SEARCHING, svc.STAGE_MATCHING]
    assert w.profile_seen_by_screen == PROFILE                       # отбор опирается на прочитанное резюме
    assert sorted(e for e in w.events if e.startswith("analyze")) == ["analyze:A", "analyze:C"]
    assert w.plans == [(2, 0)]
    shown = [i.vacancy["title"] for i in items if not i.rejected and i.analysis]
    rejected = [i.vacancy["title"] for i in items if i.rejected]
    assert sorted(shown) == ["A", "C"] and sorted(rejected) == ["B", "D"]
    assert all(i.analysis is None for i in items if i.rejected)      # отсеянные не стоили полного разбора


def test_best_matches_are_analyzed_first_and_limit_applies(monkeypatch):
    monkeypatch.setattr(svc.settings, "max_vacancies_per_search", 2)
    w = World(monkeypatch, {"A": 50, "B": 95, "C": 70, "D": 45})
    monkeypatch.setattr(svc.settings, "max_vacancies_per_search", 2)
    w.search()
    assert [e for e in w.events if e.startswith("analyze")] == ["analyze:B", "analyze:C"]


def test_profile_is_read_once_per_resume_version(monkeypatch):
    w = World(monkeypatch, {"A": 90})
    w.search()
    w.search()
    assert w.events.count("profile") == 1 and w.events.count("save_profile") == 1
    w.user["resume_text"] = "Совсем другое резюме. " * 10                # новая версия резюме читается заново
    w.search()
    assert w.events.count("profile") == 2


def test_cached_profile_is_not_read_again(monkeypatch):
    w = World(monkeypatch, {"A": 90}, cached_profile=True)
    w.search()
    assert "profile" not in w.events and w.stages[0] == svc.STAGE_READING


def test_search_still_works_when_ai_cannot_screen_or_read(monkeypatch):
    w = World(monkeypatch, {"A": 90, "B": 70}, screen_error=True, profile_error=True)
    items = w.search()
    assert not any(i.rejected for i in items)                         # без оценок ИИ отбор не применяется
    assert sorted(e for e in w.events if e.startswith("analyze")) == ["analyze:A", "analyze:B"]


def test_detailed_analysis_can_still_reject(monkeypatch):
    w = World(monkeypatch, {"A": 80, "B": 75}, final={"B": 20})
    items = {i.vacancy["title"]: i for i in w.search()}
    assert items["A"].rejected is False and items["B"].rejected is True and items["B"].analysis["match_score"] == 20


def test_nothing_fits_yields_only_rejected(monkeypatch):
    w = World(monkeypatch, {"A": 5, "B": 10})
    items = w.search()
    assert w.plans == [(0, 0)] and all(i.rejected for i in items) and len(items) == 2
    assert not any(e.startswith("analyze") for e in w.events)


# ---------- чат ----------
@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(mongo, "db", AsyncMongoMockClient(tz_aware=True)["ai-flow"])


class ChatWorld:
    def __init__(self, scores=None, answer=None, hh_error=False, no_vacancies=False):
        self.events, self.queries = [], []
        self.answer = answer or {"on_topic": True, "reply": "Вот варианты.", "search": {"terms": ["Python"], "high_salary": True}}
        self.scores = scores or {}
        self.hh_error, self.no_vacancies = hh_error, no_vacancies
        world = self

        class Scraper:
            async def search(self, f):
                world.events.append("hh")
                world.queries.append(f)
                if world.hh_error:
                    raise HHError("HH API отклонил запрос (403)")
                if world.no_vacancies:
                    return []
                return [await repo.save_vacancy({"source_id": f"hh_{t}", "title": t, "company": "Acme", "skills": ["Python"],
                                                 "salary": {"from": 1000000, "to": 2000000, "currency": "UZS"}, "url": f"https://hh.uz/{t}"})
                        for t in world.scores]

        class AI:
            async def profile_resume(self, resume):
                world.events.append("profile")
                return PROFILE

            async def screen_vacancies(self, profile, resume, vacancies):
                world.events.append("screen")
                return {i: world.scores[v["title"]] for i, v in enumerate(vacancies)}

            async def chat(self, profile, resume, history, message):
                world.events.append("chat")
                world.last = {"profile": profile, "history": history, "message": message}
                return world.answer

        scraper, ai = Scraper(), AI()
        self.service = chat_mod.ChatService(ai, scraper, svc.AnalysisService(ai, scraper))


def seed_user(resume=RESUME):
    user = run(repo.get_or_create_user(1))
    if resume:
        run(repo.set_resume(1, resume))
    run(repo.update_filters(1, keywords=["QA"], location="Ташкент"))
    return user


def test_chat_needs_resume_and_valid_message(db):
    seed_user(resume="")
    w = ChatWorld()
    with pytest.raises(svc.ProfileError):
        run(w.service.ask(1, "Какие вакансии есть?"))
    assert w.events == []                                             # без резюме ИИ вообще не вызывается
    seed_user()
    for bad in ("", "   "):
        with pytest.raises(chat_mod.ChatInputError):
            run(w.service.ask(1, bad))
    with pytest.raises(chat_mod.ChatInputError, match="500"):
        run(w.service.ask(1, "а" * 501))


def test_chat_off_topic_gets_fixed_refusal_and_does_nothing_else(db):
    seed_user()
    w = ChatWorld(answer={"on_topic": False, "reply": "", "search": None})
    r = run(w.service.ask(1, "Напиши стихотворение про осень"))
    assert r.off_topic and r.reply == OFF_TOPIC_REPLY and r.vacancies == []
    assert "hh" not in w.events and "screen" not in w.events          # поиска вакансий нет
    assert run(w.service.history(1)) == []                            # лишний вопрос не попадает в историю


def test_chat_reads_resume_first_then_finds_vacancies_by_user_terms(db):
    seed_user()
    w = ChatWorld(scores={"Python Backend": 85, "Повар": 15, "Python Dev": 60})
    r = run(w.service.ask(1, "Какие работы с большой зарплатой мне подойдут?"))
    assert w.events[:2] == ["profile", "chat"], "резюме читается до ответа"
    assert w.last["profile"] == PROFILE
    assert w.queries[0]["keywords"] == ["Python"] and w.queries[0]["high_salary"] is True and w.queries[0]["location"] == "Ташкент"
    assert [c["title"] for c in r.vacancies] == ["Python Backend", "Python Dev"]   # «Повар» не подходит под резюме
    assert r.vacancies[0]["salary"] == "1\u00a0000\u00a0000–2\u00a0000\u00a0000 UZS" and r.vacancies[0]["match_score"] == 85
    assert r.reply == "Вот варианты." and not r.notice


def test_chat_without_high_salary_orders_by_resume_fit(db):
    seed_user()
    w = ChatWorld(scores={"A": 50, "B": 90}, answer={"on_topic": True, "reply": "Ок", "search": {"terms": ["QA"], "high_salary": False}})
    r = run(w.service.ask(1, "найди QA"))
    assert [c["title"] for c in r.vacancies] == ["B", "A"] and w.queries[0]["high_salary"] is False


def test_chat_uses_resume_profile_when_model_gives_no_terms(db):
    seed_user()
    w = ChatWorld(scores={"A": 70}, answer={"on_topic": True, "reply": "", "search": {"terms": [], "high_salary": True}})
    r = run(w.service.ask(1, "где больше платят?"))
    assert w.queries[0]["keywords"] == ["Python junior"] and r.vacancies and r.reply


def test_chat_reports_when_nothing_fits_or_found(db):
    seed_user()
    r = run(ChatWorld(scores={"A": 5}).service.ask(1, "вакансии"))
    assert r.vacancies == [] and "ни одна не подходит под ваше резюме" in r.notice
    r = run(ChatWorld(no_vacancies=True).service.ask(1, "вакансии"))
    assert r.vacancies == [] and "ничего не нашлось" in r.notice


def test_chat_shows_matching_demo_vacancies_when_hh_is_blocked(db):
    seed_user()
    w = ChatWorld(hh_error=True, answer={"on_topic": True, "reply": "Смотрим.", "search": {"terms": ["Python"], "high_salary": False}})
    r = run(w.service.ask(1, "вакансии по Python"))
    assert r.vacancies and all(c["is_demo"] for c in r.vacancies) and "hh.ru сейчас недоступен" in r.notice
    assert all("python" in (c["title"] + " ".join(c["skills"])).lower() for c in r.vacancies)
    other = ChatWorld(hh_error=True, answer={"on_topic": True, "reply": "Смотрим.", "search": {"terms": ["Бухгалтер"], "high_salary": False}})
    r = run(other.service.ask(1, "вакансии бухгалтера"))
    assert r.vacancies == [] and "нет" in r.notice


def test_chat_history_is_saved_for_on_topic_only_and_fed_back_to_ai(db):
    seed_user()
    w = ChatWorld(scores={"A": 70})
    run(w.service.ask(1, "Какие вакансии есть?"))
    w.answer = {"on_topic": True, "reply": "Добавьте Docker.", "search": None}
    run(w.service.ask(1, "Что добавить в резюме?"))
    hist = run(w.service.history(1))
    assert [m["role"] for m in hist] == ["user", "assistant", "user", "assistant"]
    assert hist[1]["vacancies"] and hist[3]["text"] == "Добавьте Docker."
    assert [m["text"] for m in w.last["history"]][:2] == ["Какие вакансии есть?", "Вот варианты."]
    run(w.service.clear(1))
    assert run(w.service.history(1)) == []


def test_clearing_resume_drops_cached_profile(db):
    user = seed_user()
    run(repo.save_resume_profile(user["_id"], repo.resume_hash(RESUME), PROFILE))
    assert run(repo.get_or_create_user(1)).get("resume_profile") == PROFILE
    run(repo.clear_resume(1))
    assert "resume_profile" not in run(repo.get_or_create_user(1))
