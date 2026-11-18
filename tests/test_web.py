import json

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import container
from app import repositories as repo
from app.services.ai_service import AIError
from app.services.analysis_service import AnalysisItem, ProfileError
from app.services.chat_service import ChatInputError, ChatResult
from app.web import accounts as acc_mod
from app.web import api as web


@pytest.fixture()
def client(monkeypatch):
    users: dict[int, dict] = {}
    vacancy = {"_id": ObjectId(), "source_id": "hh_1", "title": "Python-разработчик", "company": "Acme", "url": "https://hh.uz/vacancy/1", "skills": ["Python"]}

    async def get_or_create_user(uid):
        return users.setdefault(uid, {"_id": ObjectId(), "telegram_id": uid, "resume_text": "", "filters": dict(repo.DEFAULT_FILTERS)})

    async def set_resume(uid, text):
        users[uid]["resume_text"] = text

    async def set_resume_if_empty(uid, text, name=""):
        if users[uid]["resume_text"]:
            return False
        users[uid]["resume_text"] = text
        users[uid]["resume_name"] = name
        return True

    async def clear_resume(uid):
        users[uid]["resume_text"] = ""

    async def update_filters(uid, **fields):
        users[uid]["filters"].update(fields)

    async def search(uid, on_plan=None, on_stage=None):
        if on_plan:
            await on_plan(2, 0)
        yield AnalysisItem(vacancy, {"match_score": 55, "missing_skills": ["Docker"]})
        yield AnalysisItem(vacancy, {"match_score": 91, "missing_skills": []})

    async def get_cover_letter(uid, vid):
        return (vacancy, {"generated_cover_letter": "Здравствуйте! [Ваше имя]"}) if vid == vacancy["_id"] else None

    accts: dict[str, dict] = {}
    sessions: dict[str, dict] = {}

    async def create_account(name, email, password="", login=None, **_kw):
        if email in accts:
            raise acc_mod.AccountConflict("email")
        if login and any(a.get("login") == login for a in accts.values()):
            raise acc_mod.AccountConflict("login")
        a = {"_id": ObjectId(), "email": email, "name": name, "login": login, "user_key": -(len(accts) + 1),
             "password_hash": await acc_mod.hash_password(password)}
        accts[email] = a
        return a

    async def get_account_by_email(email):
        return accts.get(email)

    async def claim_login(account_id, login):
        acct = next(a for a in accts.values() if a["_id"] == account_id)
        if acct.get("login") or any(a.get("login") == login for a in accts.values()):
            return False
        acct["login"] = login
        return True

    async def get_account_by_identifier(identifier):
        value = (identifier or "").strip().lower()
        if "@" in value:
            return accts.get(value)
        return next((a for a in accts.values() if a.get("login") == value), None)

    async def create_session(account_id):
        token = f"tok-{len(sessions):040d}"
        sessions[token] = next(a for a in accts.values() if a["_id"] == account_id)
        return token

    async def account_for_token(token):
        return sessions.get(token)

    async def delete_session(token):
        sessions.pop(token, None)

    monkeypatch.setattr(acc_mod, "create_account", create_account)
    monkeypatch.setattr(acc_mod, "get_account_by_email", get_account_by_email)
    monkeypatch.setattr(acc_mod, "get_account_by_identifier", get_account_by_identifier)
    monkeypatch.setattr(acc_mod, "claim_login", claim_login)
    monkeypatch.setattr(acc_mod, "create_session", create_session)
    monkeypatch.setattr(acc_mod, "account_for_token", account_for_token)
    monkeypatch.setattr(acc_mod, "delete_session", delete_session)
    monkeypatch.setattr(repo, "get_or_create_user", get_or_create_user)
    monkeypatch.setattr(repo, "set_resume", set_resume)
    monkeypatch.setattr(repo, "set_resume_if_empty", set_resume_if_empty)
    monkeypatch.setattr(repo, "clear_resume", clear_resume)
    monkeypatch.setattr(repo, "update_filters", update_filters)
    monkeypatch.setattr(container.analysis, "search", search)
    monkeypatch.setattr(container.analysis, "get_cover_letter", get_cover_letter)
    web._hits.clear()
    web._featured_cache = (0, {})

    app = FastAPI()
    app.include_router(web.router)
    c = TestClient(app)
    c.register = lambda email="ann@example.com", client=None, login=None: (client or c).post(
        "/api/auth/register",
        json={"login": login or email.split("@")[0], "name": "Анна", "email": email, "password": "correct-horse"})
    c.vacancy_id = str(vacancy["_id"])
    c.users = users
    return c


def test_endpoints_require_login(client):
    calls = [
        client.get("/api/me"), client.get("/api/search"), client.get("/api/letter/x"),
        client.put("/api/resume", json={"text": "x" * 80}), client.put("/api/filters", json={"keywords": ["a"]}),
        client.post("/api/resume/file?filename=a.txt", content=b"x" * 80), client.delete("/api/resume"),
    ]
    assert [r.status_code for r in calls] == [401] * 7


def test_register_validation(client):
    bad = lambda **kw: client.post("/api/auth/register", json={"login": "anna_k", "name": "Анна", "email": "a@b.co", "password": "longenough", **kw})
    assert bad(login="ab").status_code == 422             # слишком короткий логин
    assert bad(login="имя пользователя").status_code == 422
    assert bad(login="-bad").status_code == 422
    assert bad(email="not-an-email").status_code == 422
    assert bad(password="short").status_code == 422
    assert bad(name=" ").status_code == 200                # имя необязательно: подставится логин
    assert client.get("/api/me").json()["account"]["name"] == "anna_k"


def test_register_login_logout_flow(client):
    r = client.register()
    assert r.status_code == 200 and "sid" in r.cookies
    body = r.json()
    assert body["account"] == {"name": "Анна", "login": "ann", "email": "ann@example.com", "telegram_linked": False}
    assert body["onboarded"] is False
    assert "password" not in json.dumps(body)
    assert client.get("/api/me").status_code == 200
    assert client.register().status_code == 409                      # email занят
    assert client.register("ANN@Example.com").status_code == 409     # регистр не важен
    taken = client.register("other@example.com", login="ANN")        # логин тоже уникален без учёта регистра
    assert taken.status_code == 409 and "логин" in taken.json()["detail"].lower()

    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401

    login = lambda **kw: client.post("/api/auth/login", json={"login": "ann", "email": "ann@example.com", "password": "correct-horse", **kw})
    assert login(password="wrong-pass").status_code == 401
    assert login(email="nobody@example.com").status_code == 401
    assert login(login="someone-else").status_code == 401          # логин от другого аккаунта: все три поля должны совпасть
    assert login(email="other@example.com").status_code == 401
    ok = login(login=" ANN ", email=" Ann@example.com ")           # регистр и пробелы не важны
    assert ok.status_code == 200 and client.get("/api/me").status_code == 200
    # вход только по логину или только по почте больше не принимается
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"login": "ann", "password": "correct-horse"}).status_code == 422
    assert client.post("/api/auth/login", json={"email": "ann@example.com", "password": "correct-horse"}).status_code == 422


def test_auth_rate_limit(client):
    codes = [client.post("/api/auth/login", json={"login": "xx", "email": "x@y.zz", "password": "bad-password"}).status_code for _ in range(14)]
    assert codes[0] == 401 and codes[-1] == 429


def test_users_are_isolated_and_negative(client):
    client.register()
    client.put("/api/resume", json={"text": "Python разработчик. " * 10})
    other = TestClient(client.app)
    client.register("bob@example.com", other)
    assert other.get("/api/me").json()["resume"]["has"] is False
    assert all(uid < 0 for uid in client.users)


def test_onboarded_flag(client):
    client.register()
    client.put("/api/resume", json={"text": "Опыт Python и SQL. " * 10})
    assert client.get("/api/me").json()["onboarded"] is False
    client.put("/api/filters", json={"keywords": ["Python"]})
    assert client.get("/api/me").json()["onboarded"] is True


def test_resume_text_and_validation(client):
    client.register()
    assert client.put("/api/resume", json={"text": "коротко"}).status_code == 422
    r = client.put("/api/resume", json={"text": "Опыт Python и SQL. " * 10})
    assert r.status_code == 200 and r.json()["resume"]["has"]
    assert client.put("/api/resume", json={"text": "Другой текст. " * 10}).status_code == 409
    deleted = client.delete("/api/resume")
    assert deleted.status_code == 200 and deleted.json()["resume"]["has"] is False
    assert client.put("/api/resume", json={"text": "Новое резюме. " * 10}).status_code == 200


def test_resume_file_txt_and_bad_ext(client):
    client.register()
    assert client.post("/api/resume/file?filename=cv.exe", content=b"x" * 100).status_code == 422
    assert client.post("/api/resume/file?filename=cv.txt", content=b"x" * (5 * 1024 * 1024 + 1)).status_code == 413
    ok = client.post("/api/resume/file?filename=cv.txt", content=("Опыт Python. " * 20).encode())
    assert ok.status_code == 200 and ok.json()["resume"]["chars"] > 50
    assert client.post("/api/resume/file?filename=cv.txt", content=("Другое резюме. " * 20).encode()).status_code == 409


def test_filters(client):
    client.register()
    assert client.put("/api/filters", json={"keywords": [" "], "location": "Ташкент"}).status_code == 422
    r = client.put("/api/filters", json={"keywords": ["Python", " QA "], "location": "Самарканд", "employment_type": "Полная"})
    assert r.json()["filters"] == {"keywords": ["Python", "QA"], "location": "Самарканд", "employment_type": "полная"}


def test_search_requires_profile_then_streams(client):
    client.register()
    assert client.get("/api/search").status_code == 422
    client.put("/api/resume", json={"text": "Опыт Python и SQL. " * 10})
    assert client.get("/api/search").status_code == 422  # нет должностей
    client.put("/api/filters", json={"keywords": ["Python"], "location": "Ташкент", "employment_type": "полная"})

    r = client.get("/api/search")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = []
    for block in r.text.strip().split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.split("\n") if ": " in l)
        events.append((lines["event"], json.loads(lines["data"])))
    assert [e for e, _ in events] == ["plan", "card", "card", "done"]
    assert events[1][1]["match_score"] == 55 and events[-1][1]["shown"] == 2


def test_search_rate_limit(client, monkeypatch):
    client.register()
    monkeypatch.setattr(web.settings, "web_searches_per_hour", 1)
    client.put("/api/resume", json={"text": "Опыт Python и SQL. " * 10})
    client.put("/api/filters", json={"keywords": ["Python"]})
    assert client.get("/api/search").status_code == 200
    assert client.get("/api/search").status_code == 429


def test_search_reports_hh_block_for_demo_fallback(client, monkeypatch):
    async def blocked_search(_uid, on_plan=None, on_stage=None):
        if False:
            yield None
        raise web.HHError("HH API вернул HTTP 403: forbidden")

    client.register()
    client.put("/api/resume", json={"text": "Опыт Python и SQL. " * 10})
    client.put("/api/filters", json={"keywords": ["Python"]})
    monkeypatch.setattr(container.analysis, "search", blocked_search)

    response = client.get("/api/search")
    assert response.status_code == 200
    assert '"code": "hh_unavailable"' in response.text


def test_letter(client):
    client.register()
    assert client.get(f"/api/letter/{client.vacancy_id}").json()["text"] == "Здравствуйте! Анна"
    assert client.get(f"/api/letter/{ObjectId()}").status_code == 409
    assert client.get("/api/letter/not-an-id").status_code == 404


def test_vacancy_details(client, monkeypatch):
    details = {
        "title": "Python-разработчик", "company": "Acme", "description": "Разработка сервисов",
        "skills": ["Python"], "location": "Ташкент", "salary": None, "experience": "1–3 года",
        "employment": "Полная занятость", "schedule": "Полный день", "published_at": "",
        "url": "https://hh.uz/vacancy/1", "professional_roles": ["Программист"],
    }

    async def get_details(vacancy):
        assert vacancy["source_id"] == "hh_1"
        return details

    async def get_vacancy(vacancy_id):
        return {"_id": vacancy_id, "source_id": "hh_1"} if str(vacancy_id) == client.vacancy_id else None

    monkeypatch.setattr(container.scraper, "get_vacancy_details", get_details)
    monkeypatch.setattr(repo, "get_vacancy", get_vacancy)
    response = client.get(f"/api/vacancy/{client.vacancy_id}")
    assert response.status_code == 200
    assert response.json()["description"] == "Разработка сервисов"
    assert response.json()["id"] == client.vacancy_id
    assert client.get("/api/vacancy/not-an-id").status_code == 404
    assert client.get(f"/api/vacancy/{ObjectId()}").status_code == 404

    async def unavailable(_vacancy):
        return None

    monkeypatch.setattr(container.scraper, "get_vacancy_details", unavailable)
    assert client.get(f"/api/vacancy/{client.vacancy_id}").status_code == 404


def test_featured_vacancies_come_from_hh_search(client, monkeypatch):
    async def search(filters, limit=None):
        assert filters["keywords"] and filters["location"] == "Узбекистан"
        assert limit == 8
        return [{
            "_id": ObjectId(), "title": "QA Engineer", "company": "Real Company",
            "url": "https://hh.uz/vacancy/123", "skills": ["SQL"], "description": "Тестирование",
        }]

    monkeypatch.setattr(container.scraper, "search", search)
    response = client.get("/api/vacancies/featured")
    assert response.status_code == 200
    assert response.json()["items"][0]["title"] == "QA Engineer"
    assert response.json()["items"][0]["url"] == "https://hh.uz/vacancy/123"


def test_featured_uses_labeled_original_demos_when_hh_is_blocked(client, monkeypatch):
    async def blocked_search(_filters, limit=None):
        raise web.HHError("HH API вернул HTTP 403: forbidden")

    monkeypatch.setattr(container.scraper, "search", blocked_search)
    response = client.get("/api/vacancies/featured")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "demo"
    assert len(body["items"]) >= 3
    assert all(item["is_demo"] for item in body["items"])

    demo_id = body["items"][0]["id"]
    details = client.get(f"/api/vacancy/{demo_id}").json()
    assert details["is_demo"] is True
    assert "не открытая вакансия" in details["description"]


def test_featured_falls_back_to_demos_on_unexpected_error(client, monkeypatch):
    async def broken_search(_filters, limit=None):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(container.scraper, "search", broken_search)
    response = client.get("/api/vacancies/featured")
    assert response.status_code == 200
    assert response.json()["source"] == "demo"


def test_featured_falls_back_to_demos_when_hh_returns_nothing(client, monkeypatch):
    async def empty_search(_filters, limit=None):
        return []

    monkeypatch.setattr(container.scraper, "search", empty_search)
    body = client.get("/api/vacancies/featured").json()
    assert body["source"] == "demo"
    assert len(body["items"]) >= 3


def test_demo_vacancies_endpoint_needs_no_hh(client, monkeypatch):
    async def must_not_be_called(*_a, **_k):
        raise AssertionError("hh.ru не должен вызываться")

    monkeypatch.setattr(container.scraper, "search", must_not_be_called)
    body = client.get("/api/vacancies/demo").json()
    assert body["source"] == "demo"
    assert all(item["is_demo"] for item in body["items"])


def test_resume_keeps_file_name_and_full_text(client):
    client.register()
    text = "Опыт Python. " * 120
    ok = client.post("/api/resume/file?filename=C:%5Cdocs%5Cmy cv.txt", content=text.encode())
    assert ok.status_code == 200
    resume = ok.json()["resume"]
    assert resume["name"] == "my cv.txt"
    assert len(resume["preview"]) < resume["chars"]
    full = client.get("/api/resume").json()
    assert full["name"] == "my cv.txt" and full["text"].startswith("Опыт Python.") and full["chars"] == resume["chars"]
    client.delete("/api/resume")
    assert client.get("/api/resume").status_code == 404


def test_resume_full_text_requires_login(client):
    assert client.get("/api/resume").status_code == 401


def test_vacancy_details_fall_back_to_saved_data_when_hh_blocks(client, monkeypatch):
    async def blocked(_vacancy):
        raise web.HHError("HH API отклонил запрос (403)")

    async def get_vacancy(vacancy_id):
        return {
            "_id": vacancy_id, "source_id": "hh_1", "title": "Frontend", "company": "Acme",
            "requirements": "React", "responsibilities": "Верстка", "skills": ["React"],
            "url": "https://hh.uz/vacancy/1",
        }

    monkeypatch.setattr(container.scraper, "get_vacancy_details", blocked)
    monkeypatch.setattr(repo, "get_vacancy", get_vacancy)
    response = client.get(f"/api/vacancy/{client.vacancy_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Frontend" and "Верстка" in body["description"] and "React" in body["description"]
    assert body["notice"] and body["url"] == "https://hh.uz/vacancy/1"


# ---------- чат с ИИ ----------
class FakeChat:
    def __init__(self):
        self.asked, self.error, self.messages = [], None, []

    async def ask(self, uid, text):
        self.asked.append((uid, text))
        if self.error:
            raise self.error
        return ChatResult("Ответ по теме.", [{"id": "v1", "title": "Python Dev", "company": "Acme", "url": "", "skills": [],
                                                  "salary": "1 000 UZS", "match_score": 80, "is_demo": False}], notice="заметка")

    async def history(self, uid):
        return self.messages

    async def clear(self, uid):
        self.messages = []


@pytest.fixture
def fake_chat(monkeypatch):
    fake = FakeChat()
    monkeypatch.setattr(container, "chat", fake)
    return fake


def test_chat_requires_login(client, fake_chat):
    assert [client.get("/api/chat").status_code, client.post("/api/chat", json={"message": "привет"}).status_code,
            client.delete("/api/chat").status_code] == [401, 401, 401]
    assert fake_chat.asked == []


def test_chat_returns_reply_and_vacancies(client, fake_chat):
    client.register()
    r = client.post("/api/chat", json={"message": "Какие работы с высокой зарплатой?"})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "Ответ по теме." and body["off_topic"] is False and body["notice"] == "заметка"
    assert body["vacancies"][0]["title"] == "Python Dev" and body["vacancies"][0]["match_score"] == 80
    assert fake_chat.asked == [(client.users and next(iter(client.users)), "Какие работы с высокой зарплатой?")]


def test_chat_errors_are_reported_without_breaking(client, fake_chat):
    client.register()
    for error, code in ((ChatInputError("Слишком длинное сообщение."), 422), (ProfileError("Сначала добавьте резюме."), 422),
                        (AIError("лимит"), 503)):
        fake_chat.error = error
        r = client.post("/api/chat", json={"message": "вопрос"})
        assert r.status_code == code and r.json()["detail"]
    assert "недоступен" in r.json()["detail"] and "лимит" not in r.json()["detail"]   # внутренняя причина не раскрывается
    assert client.post("/api/chat", json={"message": "x" * 2001}).status_code == 422
    assert client.post("/api/chat", json={}).status_code == 422


def test_chat_is_rate_limited(client, fake_chat):
    client.register()
    codes = [client.post("/api/chat", json={"message": "вопрос"}).status_code for _ in range(22)]
    assert codes[:20] == [200] * 20 and 429 in codes[20:]


def test_chat_history_and_clear(client, fake_chat):
    client.register()
    fake_chat.messages = [{"role": "user", "text": "привет", "vacancies": [], "notice": ""}]
    assert client.get("/api/chat").json()["messages"][0]["text"] == "привет"
    assert client.delete("/api/chat").json() == {"messages": []}
    assert client.get("/api/chat").json()["messages"] == []


def test_search_stream_reports_stages_and_rejected(client, monkeypatch):
    async def staged(uid, on_plan=None, on_stage=None):
        await on_stage("Читаю ваше резюме…")
        await on_stage("Подбираю подходящие под резюме…")
        await on_plan(0, 0)
        yield AnalysisItem({"_id": ObjectId(), "title": "Повар", "company": "X"}, None, rejected=True)

    client.register()
    client.put("/api/resume", json={"text": "Опыт Python и SQL. " * 10})
    client.put("/api/filters", json={"keywords": ["Python"]})
    monkeypatch.setattr(container.analysis, "search", staged)
    text = client.get("/api/search").text
    assert "event: stage" in text and "Читаю ваше резюме" in text and "Подбираю подходящие под резюме" in text
    assert text.index("Читаю ваше резюме") < text.index("Подбираю подходящие")
    assert '"rejected": 1' in text and '"shown": 0' in text and '"failed": 0' in text and "event: card" not in text
