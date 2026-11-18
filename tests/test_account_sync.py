"""Сквозные проверки: общий аккаунт бота и сайта. Реальные accounts/repositories на поддельной MongoDB."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app import repositories as repo
from app.bot import auth, handlers
from app.db import mongo
from app.web import accounts
from app.web import api as web

RESUME = "Python-разработчик. FastAPI, PostgreSQL, pytest, Docker. " * 5


def run(coro):
    return loop.run_until_complete(coro)


loop = asyncio.new_event_loop()


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.setattr(mongo, "db", AsyncMongoMockClient(tz_aware=True)["sync-test"])
    run(mongo.ensure_account_indexes())  # те же индексы уникальности, что создаёт connect()
    auth._hits.clear()
    web._hits.clear()


def web_client() -> TestClient:
    app = FastAPI()
    app.include_router(web.router)
    return TestClient(app)


class Chat:
    """Один Telegram-пользователь: настоящее FSM-хранилище и поддельные сообщения."""

    def __init__(self, tg_id: int):
        self.tg_id = tg_id
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=tg_id, user_id=tg_id))
        self.sent: list[str] = []

    def message(self, text="") -> MagicMock:
        m = MagicMock(spec=Message)
        m.text = text
        m.from_user = MagicMock(id=self.tg_id)
        m.delete = AsyncMock()

        async def answer(body, **_kw):
            self.sent.append(body)
            return MagicMock(spec=Message, edit_text=AsyncMock())

        m.answer = answer
        m.answer_document = AsyncMock()
        return m

    def account(self):
        return run(accounts.get_account_by_telegram(self.tg_id))

    def say(self, handler, text="", **kw):
        run(handler(self.message(text), self.state, **kw))

    def register(self, login="ann_dev", password="correct-horse", email="ann@example.com", name=""):
        self.say(handlers.auth.cmd_register)
        self.say(auth.reg_login, login)
        self.say(auth.reg_password, password)
        self.say(auth.reg_email, email)
        if name:
            self.say(auth.reg_name, name)
        else:
            cb = MagicMock(spec=CallbackQuery)
            cb.from_user = MagicMock(id=self.tg_id)
            cb.message = self.message()
            cb.answer = AsyncMock()
            run(auth.reg_skip_name(cb, self.state))

    def login(self, login, email, password):
        self.say(auth.cmd_login)
        self.say(auth.login_ident, login)
        self.say(auth.login_email, email)
        self.say(auth.login_password, password)


# ---------- аккаунт из бота → вход на сайте ----------
def test_account_created_in_bot_works_on_site_with_same_data():
    bot = Chat(7)
    bot.register(name="Анна")
    acc = bot.account()
    assert acc["login"] == "ann_dev" and acc["name"] == "Анна" and acc["telegram_id"] == 7
    assert "correct-horse" not in str(acc) and acc["password_hash"].startswith("scrypt$")

    bot.say(handlers.resume_text, RESUME, account=acc)
    site = web_client()
    login = site.post("/api/auth/login", json={"login": "Ann_Dev", "email": "ann@example.com", "password": "correct-horse"})
    assert login.status_code == 200
    me = site.get("/api/me").json()
    assert me["account"]["telegram_linked"] is True
    assert me["resume"]["has"] is True                                  # резюме из бота видно на сайте
    assert site.post("/api/auth/login", json={"login": "ann_dev", "email": "wrong@example.com", "password": "correct-horse"}).status_code == 401

    # и обратно: фильтры с сайта видны боту
    site.put("/api/filters", json={"keywords": ["Python"], "location": "Ташкент", "employment_type": "полная"})
    assert run(repo.get_or_create_user(acc["user_key"]))["filters"]["keywords"] == ["Python"]


def test_existing_bot_data_is_kept_when_account_is_created():
    run(repo.get_or_create_user(7))
    run(repo.set_resume(7, RESUME))                 # резюме, добавленное в бот до появления аккаунтов
    bot = Chat(7)
    bot.register()
    assert bot.account()["user_key"] == 7
    site = web_client()
    site.post("/api/auth/login", json={"login": "ann_dev", "email": "ann@example.com", "password": "correct-horse"})
    assert site.get("/api/me").json()["resume"]["has"] is True


# ---------- аккаунт с сайта → вход в боте ----------
def test_account_created_on_site_can_be_used_in_bot():
    site = web_client()
    r = site.post("/api/auth/register", json={"login": "bob", "email": "bob@example.com", "password": "correct-horse"})
    assert r.status_code == 200 and r.json()["account"]["telegram_linked"] is False
    site.put("/api/resume", json={"text": RESUME})

    bot = Chat(8)
    assert bot.account() is None
    bot.login("bob", "bob@example.com", "correct-horse")
    acc = bot.account()
    assert acc and acc["login"] == "bob" and acc["user_key"] < 0
    assert any("Вы вошли" in t for t in bot.sent)

    bot.say(handlers.cmd_profile, account=acc)
    assert "bob" in bot.sent[-1] and "✅" in bot.sent[-1]    # резюме с сайта видно в боте
    assert site.get("/api/me").json()["account"]["telegram_linked"] is True


def test_wrong_password_does_not_link_and_attempts_are_limited():
    web_client().post("/api/auth/register", json={"login": "bob", "email": "bob@example.com", "password": "correct-horse"})
    bot = Chat(8)
    bot.login("bob", "bob@example.com", "wrong-password")
    assert bot.account() is None and "Неверный логин, пароль или email" in bot.sent[-1]
    bot.login("nobody", "nobody@example.com", "whatever-123")
    assert "Неверный логин, пароль или email" in bot.sent[-1]       # одинаковый ответ: логины не раскрываются
    for _ in range(10):
        bot.login("bob", "bob@example.com", "wrong-password")
    assert "Слишком много попыток" in bot.sent[-1]


def test_account_cannot_be_linked_to_two_telegrams():
    first = Chat(7)
    first.register()
    second = Chat(9)
    second.login("ann_dev", "ann@example.com", "correct-horse")
    assert second.account() is None and "другому Telegram" in second.sent[-1]

    first.say(handlers.cmd_logout, account=first.account())  # выйти в первом чате
    assert first.account() is None
    second.login("ann_dev", "ann@example.com", "correct-horse")
    assert second.account() and second.account()["login"] == "ann_dev"


def test_logout_keeps_account_and_data():
    bot = Chat(7)
    bot.register()
    acc = bot.account()
    bot.say(handlers.resume_text, RESUME, account=acc)
    bot.say(handlers.cmd_logout, account=acc)
    assert bot.account() is None
    bot.login("ann_dev", "ann@example.com", "correct-horse")
    assert run(repo.get_or_create_user(bot.account()["user_key"]))["resume_text"]


# ---------- проверки при регистрации ----------
def test_registration_rejects_taken_login_email_and_bad_input():
    Chat(7).register()
    other = Chat(8)
    other.say(handlers.auth.cmd_register)
    other.say(auth.reg_login, "ANN_DEV")
    assert "уже занят" in other.sent[-1]
    other.say(auth.reg_login, "a")
    assert "Логин не подходит" in other.sent[-1]
    other.say(auth.reg_login, "second")
    other.say(auth.reg_password, "short")
    assert "не короче" in other.sent[-1] or "от 8" in other.sent[-1]
    other.say(auth.reg_password, "correct-horse")
    other.say(auth.reg_email, "not-an-email")
    assert "Проверьте адрес" in other.sent[-1]
    other.say(auth.reg_email, "Ann@Example.com")
    assert "уже зарегистрирован" in other.sent[-1]
    assert other.account() is None


def test_password_message_is_deleted_from_chat():
    bot = Chat(7)
    bot.say(handlers.auth.cmd_register)
    bot.say(auth.reg_login, "ann_dev")
    pw = bot.message("correct-horse")
    run(auth.reg_password(pw, bot.state))
    pw.delete.assert_awaited_once()
    assert "correct-horse" not in str(run(bot.state.get_data()))   # в FSM только хэш


# ---------- бот недоступен без аккаунта ----------
def test_middleware_blocks_everything_but_auth_without_account():
    bot = Chat(7)
    mw = auth.AccountMiddleware()
    seen = []

    async def handler(event, data):
        seen.append(data.get("account"))
        return "ok"

    def event(text):
        m = bot.message(text)
        return m, {"event_from_user": m.from_user, "state": bot.state}

    for text in ("🔍 Поиск вакансий", "📄 Резюме", "⚙️ Фильтры", "💬 Чат с ИИ", "⬅️ Выйти из чата", "/search", "/chat", "/profile", "просто текст"):
        m, data = event(text)
        assert run(mw(handler, m, data)) is None, text
    assert seen == [] and "создайте аккаунт" in bot.sent[-1]

    for text in ("/start", "/help", "/cancel", "/register", "/login"):
        m, data = event(text)
        assert run(mw(handler, m, data)) == "ok", text

    run(bot.state.set_state(auth.AuthStates.reg_login))        # ввод логина в процессе регистрации
    m, data = event("ann_dev")
    assert run(mw(handler, m, data)) == "ok"
    m, data = event("🔍 Поиск вакансий")                         # но меню в середине регистрации не обходит вход
    assert run(mw(handler, m, data)) is None

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = MagicMock(id=7)
    cb.message = bot.message()
    cb.answer = AsyncMock()
    cb.data = "cl:123"
    assert run(mw(handler, cb, {"event_from_user": cb.from_user, "state": bot.state})) is None
    cb.data = "auth:register"
    assert run(mw(handler, cb, {"event_from_user": cb.from_user, "state": bot.state})) == "ok"


def test_middleware_passes_account_after_login():
    bot = Chat(7)
    bot.register()
    mw = auth.AccountMiddleware()
    got = {}

    async def handler(event, data):
        got.update(data)

    m = bot.message("🔍 Поиск вакансий")
    run(mw(handler, m, {"event_from_user": m.from_user, "state": bot.state}))
    assert got["account"]["login"] == "ann_dev"


def test_start_for_guest_shows_auth_buttons_and_for_user_shows_menu():
    bot = Chat(7)
    sent = []

    async def answer(body, **kw):
        sent.append((body, kw.get("reply_markup")))

    m = bot.message("/start")
    m.answer = answer
    run(handlers.cmd_start(m, bot.state, None))
    assert "создайте аккаунт" in sent[-1][0] and sent[-1][1].inline_keyboard[0][0].callback_data == "auth:register"

    bot.register()
    run(handlers.cmd_start(m, bot.state, bot.account()))
    assert "Привет" in sent[-1][0] and sent[-1][1].keyboard


# ---------- модуль аккаунтов напрямую ----------
def test_accounts_module_conflicts_and_user_key_fallback():
    a = run(accounts.create_account("A", "a@x.co", "correct-horse", "alice", telegram_id=5))
    assert a["user_key"] == 5
    with pytest.raises(accounts.AccountConflict) as e:
        run(accounts.create_account("B", "a@x.co", "correct-horse", "other"))
    assert e.value.field == "email"
    with pytest.raises(accounts.AccountConflict) as e:
        run(accounts.create_account("B", "b@x.co", "correct-horse", "alice"))
    assert e.value.field == "login"

    run(accounts.unlink_telegram(5))
    b = run(accounts.create_account("B", "b@x.co", "correct-horse", "bob", telegram_id=5))
    assert b["user_key"] < 0, "чужие данные прежнего аккаунта (user_key=5) не передаются новому"
    assert run(accounts.get_account_by_identifier("BOB"))["email"] == "b@x.co"
    assert run(accounts.get_account_by_identifier("b@x.co"))["login"] == "bob"
    assert run(accounts.get_account_by_identifier("")) is None


# ---------- старые данные присоединяются к аккаунту ----------
def site_account(login="bob", email="bob@example.com"):
    site = web_client()
    r = site.post("/api/auth/register", json={"login": login, "email": email, "password": "correct-horse"})
    assert r.status_code == 200
    return site


def seed_legacy_bot_user(tg_id, resume=RESUME, keywords=("Python",)):
    """Пользователь, который работал с ботом ещё до появления аккаунтов."""
    user = run(repo.get_or_create_user(tg_id))
    run(repo.set_resume(tg_id, resume))
    run(repo.update_filters(tg_id, keywords=list(keywords), location="Ташкент"))
    vacancy = run(repo.save_vacancy({"source_id": "hh_1", "title": "Python", "company": "Acme"}))
    run(repo.save_analysis(user["_id"], vacancy["_id"], {"resume_hash": repo.resume_hash(resume), "match_score": 80}))
    return user, vacancy


def test_old_bot_data_joins_site_account_on_login():
    site = site_account()
    legacy, vacancy = seed_legacy_bot_user(8)

    bot = Chat(8)
    bot.login("bob", "bob@example.com", "correct-horse")
    acc = bot.account()
    assert "резюме" in bot.sent[-1] and "фильтры" in bot.sent[-1] and "анализа" in bot.sent[-1]

    me = site.get("/api/me").json()                                   # то же самое видно на сайте
    assert me["resume"]["has"] is True and me["filters"]["keywords"] == ["Python"] and me["onboarded"] is True
    user = run(repo.get_or_create_user(acc["user_key"]))
    assert run(repo.get_analysis(user["_id"], vacancy["_id"], repo.resume_hash(RESUME)))["match_score"] == 80
    assert run(mongo.users.find_one({"_id": legacy["_id"]})) is None  # старая запись перенесена, дубля нет


def test_merge_never_overwrites_account_data():
    site = site_account()
    site.put("/api/resume", json={"text": "Другое резюме про бухгалтерию. " * 10})
    site.put("/api/filters", json={"keywords": ["Бухгалтер"], "location": "Ташкент", "employment_type": "полная"})
    seed_legacy_bot_user(8)

    bot = Chat(8)
    bot.login("bob", "bob@example.com", "correct-horse")
    assert "не перенесено" in bot.sent[-1]
    me = site.get("/api/me").json()
    assert me["filters"]["keywords"] == ["Бухгалтер"]
    assert "бухгалтерию" in site_resume_text(site)


def site_resume_text(site) -> str:
    acc = run(accounts.get_account_by_email("bob@example.com"))
    return run(repo.get_or_create_user(acc["user_key"]))["resume_text"]


def test_merge_does_not_take_data_that_belongs_to_another_account():
    first = Chat(7)
    first.register()                                          # аккаунт из бота: его данные лежат под ключом 7
    first.say(handlers.resume_text, RESUME, account=first.account())
    site = site_account()
    first.say(handlers.cmd_logout, account=first.account())

    first.login("bob", "bob@example.com", "correct-horse")   # тот же Telegram входит в другой аккаунт
    assert site.get("/api/me").json()["resume"]["has"] is False, "резюме первого аккаунта не должно утечь во второй"


def test_resume_added_in_bot_appears_on_site_and_back_without_duplicates():
    site = site_account()
    bot = Chat(8)
    bot.login("bob", "bob@example.com", "correct-horse")
    acc = bot.account()
    bot.say(handlers.resume_text, RESUME, account=acc)
    assert site.get("/api/me").json()["resume"]["has"] is True
    assert site.put("/api/resume", json={"text": "Второе резюме. " * 10}).status_code == 409   # второго резюме нет

    site.delete("/api/resume")
    bot.say(handlers.cmd_resume, account=acc)
    assert "Пришлите резюме" in bot.sent[-1]                  # на сайте удалили, бот сразу предлагает добавить новое


# ---------- старые аккаунты без логина ----------
def legacy_web_account(email="old@example.com"):
    run(mongo.web_accounts.insert_one({
        "email": email, "name": "Старый", "user_key": -123456,
        "password_hash": run(accounts.hash_password("correct-horse")),
    }))


def test_old_account_without_login_gets_login_on_first_sign_in():
    legacy_web_account()
    site = web_client()
    payload = {"login": "old_user", "email": "old@example.com", "password": "correct-horse"}
    assert site.post("/api/auth/login", json={**payload, "password": "nope-nope-1"}).status_code == 401
    assert site.post("/api/auth/login", json={**payload, "login": "x"}).status_code == 422     # логин не по правилам
    ok = site.post("/api/auth/login", json=payload)
    assert ok.status_code == 200 and ok.json()["account"]["login"] == "old_user"
    site.post("/api/auth/logout")
    assert site.post("/api/auth/login", json={**payload, "login": "another"}).status_code == 401   # теперь логин закреплён
    assert site.post("/api/auth/login", json=payload).status_code == 200

    bot = Chat(8)
    bot.login("old_user", "old@example.com", "correct-horse")                                  # и тот же вход работает в боте
    assert bot.account()["user_key"] == -123456


def test_old_account_cannot_claim_taken_login():
    run(accounts.create_account("A", "a@x.co", "correct-horse", "taken"))
    legacy_web_account()
    r = web_client().post("/api/auth/login", json={"login": "TAKEN", "email": "old@example.com", "password": "correct-horse"})
    assert r.status_code == 409 and "занят" in r.json()["detail"]
