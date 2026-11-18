import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, Message

from app import container
from app import repositories as repo
from app.bot import handlers
from app.demo_vacancies import DEMO_VACANCIES
from app.services.ai_service import AIError
from app.services.analysis_service import AnalysisItem, ProfileError
from app.services.chat_service import ChatInputError, ChatResult
from app.services.scraper import HHError


ACC = {"user_key": 7, "login": "ann", "name": "Анна", "email": "a@b.co"}


def run(coro):
    return asyncio.run(coro)


def make_message(text="", user_id=7):
    m = MagicMock(spec=Message)
    m.text = text
    m.from_user = MagicMock(id=user_id)
    m.sent = []
    m.statuses = []

    async def answer(body, **kwargs):
        m.sent.append((body, kwargs))
        status = MagicMock(spec=Message)
        status.edit_text = AsyncMock()
        status.edits = status.edit_text
        m.statuses.append(status)
        return status

    m.answer = answer
    m.answer_document = AsyncMock()
    m.edit_text = AsyncMock()
    return m


def make_callback(data, user_id=7):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = MagicMock(id=user_id)
    cb.message = make_message(user_id=user_id)
    cb.answer = AsyncMock()
    return cb


def make_state():
    state = MagicMock()
    state.clear = AsyncMock()
    state.set_state = AsyncMock()
    return state


class Store:
    """Подмена репозитория в памяти."""

    def __init__(self, monkeypatch, resume=""):
        self.user = {"_id": 1, "resume_text": resume, "resume_name": "cv.pdf" if resume else "",
                     "filters": {"keywords": ["Python"], "location": "Ташкент", "employment_type": "полная"}}

        async def get_or_create_user(_uid):
            return self.user

        async def set_resume_if_empty(_uid, text, name=""):
            if self.user["resume_text"]:
                return False
            self.user["resume_text"], self.user["resume_name"] = text, name
            return True

        async def clear_resume(_uid):
            self.user["resume_text"], self.user["resume_name"] = "", ""

        monkeypatch.setattr(repo, "get_or_create_user", get_or_create_user)
        monkeypatch.setattr(repo, "set_resume_if_empty", set_resume_if_empty)
        monkeypatch.setattr(repo, "clear_resume", clear_resume)


# ---------- поиск: демо-вакансии ----------
def search_with(monkeypatch, gen):
    monkeypatch.setattr(container.analysis, "search", gen)


def demo_titles(m):
    return [body for body, _ in m.sent if "ДЕМО" in body]


def test_search_shows_demo_when_hh_blocks(monkeypatch):
    async def blocked(_uid, on_plan=None, on_stage=None):
        raise HHError("HH API отклонил запрос (403: forbidden).")
        yield  # pragma: no cover

    search_with(monkeypatch, blocked)
    m = make_message()
    run(handlers.cmd_search(m, make_state(), ACC))
    assert len(demo_titles(m)) == len(DEMO_VACANCIES)
    assert all(kwargs["reply_markup"] for body, kwargs in m.sent if "ДЕМО" in body)


def test_search_shows_demo_when_nothing_found(monkeypatch):
    async def empty(_uid, on_plan=None, on_stage=None):
        return
        yield  # pragma: no cover

    search_with(monkeypatch, empty)
    m = make_message()
    run(handlers.cmd_search(m, make_state(), ACC))
    assert len(demo_titles(m)) == len(DEMO_VACANCIES)


def test_search_shows_demo_when_ai_fails_for_all(monkeypatch):
    async def failing(_uid, on_plan=None, on_stage=None):
        yield AnalysisItem({"_id": 1, "title": "x"}, None, error="boom")

    search_with(monkeypatch, failing)
    m = make_message()
    run(handlers.cmd_search(m, make_state(), ACC))
    assert len(demo_titles(m)) == len(DEMO_VACANCIES)


def test_search_shows_demo_on_unexpected_error(monkeypatch):
    async def broken(_uid, on_plan=None, on_stage=None):
        raise RuntimeError("db down")
        yield  # pragma: no cover

    search_with(monkeypatch, broken)
    m = make_message()
    run(handlers.cmd_search(m, make_state(), ACC))
    assert len(demo_titles(m)) == len(DEMO_VACANCIES)


def test_search_without_resume_shows_warning_not_demo(monkeypatch):
    async def no_profile(_uid, on_plan=None, on_stage=None):
        raise ProfileError("Сначала добавьте резюме")
        yield  # pragma: no cover

    search_with(monkeypatch, no_profile)
    m = make_message()
    run(handlers.cmd_search(m, make_state(), ACC))
    assert demo_titles(m) == []


def test_demo_details_callback_sends_full_description():
    vid = next(iter(DEMO_VACANCIES))
    cb = make_callback(f"demo:{vid}")
    sent = []

    async def answer(body, **kwargs):
        sent.append(body)

    cb.message.answer = answer
    run(handlers.on_demo_details(cb))
    text = "\n".join(sent)
    assert "ДЕМО" in text and "Полное описание" in text and DEMO_VACANCIES[vid]["title"] in text

    bad = make_callback("demo:nope")
    run(handlers.on_demo_details(bad))
    bad.answer.assert_awaited_once()


# ---------- одно резюме ----------
def test_resume_command_blocks_second_resume(monkeypatch):
    Store(monkeypatch, resume="Опыт Python. " * 10)
    m, state = make_message(), make_state()
    run(handlers.cmd_resume(m, state, ACC))
    assert "уже сохранено" in m.sent[0][0] and m.sent[0][1]["reply_markup"] is not None
    state.set_state.assert_not_awaited()


def test_resume_command_asks_for_resume_when_empty(monkeypatch):
    Store(monkeypatch)
    m, state = make_message(), make_state()
    run(handlers.cmd_resume(m, state, ACC))
    state.set_state.assert_awaited_once()
    assert "Пришлите резюме" in m.sent[0][0]


def test_resume_text_saves_once_and_second_is_rejected(monkeypatch):
    store = Store(monkeypatch)
    first = make_message("Опыт Python и SQL. " * 10)
    run(handlers.resume_text(first, make_state(), ACC))
    assert "сохранено" in first.sent[0][0] and store.user["resume_text"]

    original = store.user["resume_text"]
    second = make_message("Совсем другое резюме. " * 10)
    run(handlers.resume_text(second, make_state(), ACC))
    assert "Сначала удалите" in second.sent[0][0]
    assert store.user["resume_text"] == original


def test_resume_view_sends_text_as_file(monkeypatch):
    Store(monkeypatch, resume="Текст резюме. " * 10)
    cb = make_callback("resume:view")
    run(handlers.on_resume_view(cb, ACC))
    cb.message.answer_document.assert_awaited_once()
    doc = cb.message.answer_document.await_args.args[0]
    assert doc.filename == "cv.txt"


def test_resume_delete_requires_confirmation(monkeypatch):
    store = Store(monkeypatch, resume="Текст резюме. " * 10)
    ask = make_callback("resume:del")
    run(handlers.on_resume_delete(ask))
    assert store.user["resume_text"], "до подтверждения резюме не удаляется"
    assert "Удалить резюме?" in ask.message.edit_text.await_args.args[0]

    cancel = make_callback("resume:del_no")
    run(handlers.on_resume_delete_cancel(cancel, ACC))
    assert store.user["resume_text"]

    yes = make_callback("resume:del_yes")
    run(handlers.on_resume_delete_confirm(yes, ACC))
    assert store.user["resume_text"] == ""

    m = make_message("Новое резюме. " * 10)
    run(handlers.resume_text(m, make_state(), ACC))
    assert "сохранено" in m.sent[0][0] and store.user["resume_text"]


# ---------- этапы поиска: сначала резюме ----------
def test_search_shows_stages_and_demo_when_nothing_fits_resume(monkeypatch):
    async def staged(_uid, on_plan=None, on_stage=None):
        await on_stage("Читаю ваше резюме…")
        await on_stage("Подбираю подходящие под резюме…")
        await on_plan(0, 0)
        yield AnalysisItem({"_id": 1, "title": "Повар"}, None, rejected=True)
        yield AnalysisItem({"_id": 2, "title": "Курьер"}, None, rejected=True)

    search_with(monkeypatch, staged)
    m = make_message()
    run(handlers.cmd_search(m, make_state(), ACC))
    assert "прочитает ваше резюме" in m.sent[0][0]                      # пользователь сразу видит, с чего начинается поиск
    edits = [c.args[0] for c in m.statuses[0].edit_text.await_args_list]
    assert any("Читаю ваше резюме" in e for e in edits) and any("Подбираю подходящие" in e for e in edits)
    assert edits.index(next(e for e in edits if "Читаю" in e)) < edits.index(next(e for e in edits if "Подбираю" in e))
    assert any("ни одна не подходит под ваше резюме" in e for e in edits)
    assert len(demo_titles(m)) == len(DEMO_VACANCIES)
    assert not any("Повар" in body for body, _ in m.sent)               # неподходящие вакансии не показываются


# ---------- чат с ИИ ----------
from app.bot import auth, keyboards as kb  # noqa: E402
from app.bot.states import ChatStates  # noqa: E402

CARD = {"id": "abc", "title": "Python Backend", "company": "Acme", "url": "https://hh.uz/1", "skills": ["Python", "SQL"],
        "salary": "1\u00a0000–2\u00a0000 UZS", "match_score": 85, "is_demo": False}


def fake_chat(monkeypatch, result=None, error=None):
    asked = []

    async def ask(uid, text):
        asked.append((uid, text))
        if error:
            raise error
        return result

    monkeypatch.setattr(container.chat, "ask", ask)
    return asked


def test_chat_requires_resume_then_opens(monkeypatch):
    Store(monkeypatch)                                              # резюме нет
    m, state = make_message(), make_state()
    run(handlers.cmd_chat(m, state, ACC))
    state.set_state.assert_not_awaited()
    assert "Сначала добавьте" in m.sent[0][0]

    Store(monkeypatch, resume="Python, SQL. " * 10)
    m, state = make_message(), make_state()
    run(handlers.cmd_chat(m, state, ACC))
    state.set_state.assert_awaited_once_with(ChatStates.active)
    body, kw = m.sent[0]
    assert "только о работе и карьере" in body and kw["reply_markup"].keyboard[0][0].text == kb.BTN_CHAT_EXIT


def test_chat_message_sends_reply_notice_and_vacancy_cards(monkeypatch):
    asked = fake_chat(monkeypatch, ChatResult("Подойдёт Python.", [CARD, {**CARD, "id": "demo-qa", "is_demo": True, "url": "", "salary": "", "match_score": None}], notice="Не удалось сверить."))
    m = make_message("Какие вакансии с высокой зарплатой?")
    run(handlers.chat_message(m, make_state(), ACC))
    assert asked == [(7, "Какие вакансии с высокой зарплатой?")]
    bodies = [b for b, _ in m.sent]
    assert bodies[0] == "Подойдёт Python." and "Не удалось сверить" in bodies[1]
    real, demo = m.sent[2], m.sent[3]
    assert "Python Backend" in real[0] and "1\u00a0000–2\u00a0000 UZS" in real[0] and "85%" in real[0] and "https://hh.uz/1" in real[0]
    assert real[1]["reply_markup"] is None
    assert "ДЕМО" in demo[0] and demo[1]["reply_markup"].inline_keyboard[0][0].callback_data == "demo:demo-qa"


def test_chat_off_topic_sends_only_the_refusal(monkeypatch):
    from app.services.ai_service import OFF_TOPIC_REPLY
    fake_chat(monkeypatch, ChatResult(OFF_TOPIC_REPLY, off_topic=True))
    m = make_message("Расскажи анекдот")
    run(handlers.chat_message(m, make_state(), ACC))
    assert len(m.sent) == 1 and "только на вопросы о работе и карьере" in m.sent[0][0]


def test_chat_errors_get_short_messages(monkeypatch):
    for error, expected in ((ChatInputError("Слишком длинное сообщение: не больше 500 символов."), "не больше 500"),
                            (ProfileError("Сначала добавьте резюме."), "добавьте резюме"),
                            (AIError("лимит"), "ИИ сейчас недоступен"), (RuntimeError("boom"), "Что-то пошло не так")):
        fake_chat(monkeypatch, error=error)
        m = make_message("вопрос")
        run(handlers.chat_message(m, make_state(), ACC))
        assert expected in m.sent[0][0] and "boom" not in m.sent[0][0] and "лимит" not in m.sent[0][0]


def test_chat_message_rate_limit(monkeypatch):
    auth._hits.clear()
    asked = fake_chat(monkeypatch, ChatResult("ок"))
    last = None
    for _ in range(22):
        last = make_message("вопрос")
        run(handlers.chat_message(last, make_state(), ACC))
    assert len(asked) == 20 and "Слишком много сообщений" in last.sent[0][0]
    auth._hits.clear()


def test_chat_exit_returns_main_menu():
    m, state = make_message(kb.BTN_CHAT_EXIT), make_state()
    run(handlers.chat_exit(m, state, ACC))
    state.clear.assert_awaited_once()
    assert m.sent[0][1]["reply_markup"].keyboard[0][0].text == kb.BTN_SEARCH
    assert kb.BTN_CHAT in [b.text for row in m.sent[0][1]["reply_markup"].keyboard for b in row]


def test_chat_buttons_are_menu_buttons_so_guests_are_blocked():
    assert kb.BTN_CHAT in kb.MENU_BUTTONS and kb.BTN_CHAT_EXIT in kb.MENU_BUTTONS
