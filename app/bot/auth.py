"""Аккаунты в Telegram-боте: регистрация (логин, пароль, email), вход и доступ только после входа.

Аккаунт общий с сайтом (коллекция web_accounts): бот привязывается к нему по telegram_id,
а резюме и фильтры лежат под user_key аккаунта, поэтому сайт и бот видят одни и те же данные.
"""
import html
import logging
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app import repositories as repo
from app.bot import keyboards as kb
from app.bot.states import AuthStates
from app.web import accounts

log = logging.getLogger(__name__)
router = Router()

AUTH_PROMPT = (
    "🔐 Чтобы пользоваться ботом, создайте аккаунт или войдите в уже существующий.\n"
    "Аккаунт общий для бота и сайта: резюме и фильтры будут одинаковыми и там, и там."
)
LOGIN_RULES = "3–32 символа: латинские буквы, цифры и знаки _ . - (начинается с буквы или цифры)."
CONFLICT_TEXT = {
    "email": "Этот email уже зарегистрирован. Нажмите «Войти».",
    "login": "Этот логин уже занят.",
    "telegram": "Этот Telegram уже привязан к аккаунту.",
}
_AUTH_COMMANDS = ("/start", "/help", "/cancel", "/register", "/login")

# ограничения попыток (в памяти процесса): защита пароля от подбора через бота
_hits: dict[str, deque] = defaultdict(deque)


def _limited(bucket: str, key: int, limit: int, window: int) -> bool:
    now = time.monotonic()
    q = _hits[f"{bucket}:{key}"]
    while q and q[0] <= now - window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


async def _forget(message: Message) -> None:
    """Удаляет из чата сообщение с паролем: оно не должно оставаться в истории."""
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


# ---------- доступ только после входа ----------
class AccountMiddleware(BaseMiddleware):
    """Подкладывает account в обработчики. Без аккаунта пускает только на регистрацию и вход."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        account = await accounts.get_account_by_telegram(user.id)
        if account:
            data["account"] = account
            return await handler(event, data)
        if await self._is_auth_event(event, data):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("Сначала войдите в аккаунт.", show_alert=True)
            if isinstance(event.message, Message):
                await event.message.answer(AUTH_PROMPT, reply_markup=kb.auth_kb())
        elif isinstance(event, Message):
            await event.answer(AUTH_PROMPT, reply_markup=kb.auth_kb())
        return None

    @staticmethod
    async def _is_auth_event(event: TelegramObject, data: dict[str, Any]) -> bool:
        if isinstance(event, CallbackQuery):
            return (event.data or "").startswith("auth:")
        if not isinstance(event, Message):
            return False
        text = (event.text or "").strip()
        if text.startswith(_AUTH_COMMANDS):
            return True
        if text in kb.MENU_BUTTONS or text.startswith("/"):
            return False
        state: FSMContext | None = data.get("state")
        current = await state.get_state() if state else None
        return bool(current and current.startswith("AuthStates:"))


# ---------- регистрация: логин → пароль → email → имя ----------
def _already(account: dict | None) -> str | None:
    if not account:
        return None
    return (
        f"Вы уже вошли как <b>{_esc(account.get('login') or account['email'])}</b>. "
        "Чтобы войти в другой аккаунт, сначала нажмите «🚪 Выйти»."
    )


@router.message(Command("register"))
async def cmd_register(message: Message, state: FSMContext, account: dict | None = None) -> None:
    if text := _already(account):
        await message.answer(text)
        return
    await _start_register(message, state)


@router.callback_query(F.data == "auth:register")
async def cb_register(cb: CallbackQuery, state: FSMContext, account: dict | None = None) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        if text := _already(account):
            await cb.message.answer(text)
            return
        await _start_register(cb.message, state)


async def _start_register(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AuthStates.reg_login)
    await message.answer(f"Шаг 1/4. Придумайте логин.\n{LOGIN_RULES}\nОтмена: /cancel")


@router.message(AuthStates.reg_login, F.text)
async def reg_login(message: Message, state: FSMContext) -> None:
    login = accounts.normalize_login(message.text)
    if not accounts.valid_login(login):
        await message.answer(f"Логин не подходит. {LOGIN_RULES}")
        return
    if await accounts.get_account_by_identifier(login):
        await message.answer("Этот логин уже занят. Придумайте другой.")
        return
    await state.update_data(login=login)
    await state.set_state(AuthStates.reg_password)
    await message.answer(
        "Шаг 2/4. Придумайте пароль (не короче "
        f"{accounts.MIN_PASSWORD} символов). Я сразу удалю это сообщение из чата."
    )


@router.message(AuthStates.reg_password, F.text)
async def reg_password(message: Message, state: FSMContext) -> None:
    password = message.text
    await _forget(message)
    if not accounts.MIN_PASSWORD <= len(password) <= 200:
        await message.answer(f"Пароль должен быть от {accounts.MIN_PASSWORD} до 200 символов. Попробуйте ещё раз.")
        return
    # в памяти FSM хранится только хэш, пароль в открытом виде не сохраняется
    await state.update_data(password_hash=await accounts.hash_password(password))
    await state.set_state(AuthStates.reg_email)
    await message.answer("Пароль принят, сообщение удалено.\nШаг 3/4. Введите email.")


@router.message(AuthStates.reg_email, F.text)
async def reg_email(message: Message, state: FSMContext) -> None:
    email = accounts.normalize_email(message.text)
    if not accounts.valid_email(email):
        await message.answer("Проверьте адрес электронной почты и отправьте ещё раз.")
        return
    if await accounts.get_account_by_identifier(email):
        await message.answer("Этот email уже зарегистрирован. Введите другой или нажмите /login, чтобы войти.")
        return
    await state.update_data(email=email)
    await state.set_state(AuthStates.reg_name)
    await message.answer(
        "Шаг 4/4. Как вас зовут? Имя подставится в подпись сопроводительного письма. "
        "Можно пропустить: тогда будет использован логин.",
        reply_markup=kb.skip_name_kb(),
    )


@router.message(AuthStates.reg_name, F.text)
async def reg_name(message: Message, state: FSMContext) -> None:
    await _finish_register(message, state, message.from_user.id, message.text)


@router.callback_query(AuthStates.reg_name, F.data == "auth:skip_name")
async def reg_skip_name(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _finish_register(cb.message, state, cb.from_user.id, "")


async def _finish_register(message: Message, state: FSMContext, telegram_id: int, name: str) -> None:
    if _limited("register", telegram_id, 5, 3600):
        await state.clear()
        await message.answer("Слишком много попыток регистрации. Попробуйте через час.")
        return
    data = await state.get_data()
    login = data.get("login")
    if not (login and data.get("email") and data.get("password_hash")):
        await state.clear()
        await message.answer("Данные регистрации потерялись. Начните заново: /register", reply_markup=kb.auth_kb())
        return
    name = " ".join((name or "").split())[:80] or login
    try:
        account = await accounts.create_account(
            name, data["email"], login=login, password_hash=data["password_hash"], telegram_id=telegram_id
        )
    except accounts.AccountConflict as e:
        await state.clear()
        await message.answer(
            f"⚠️ {CONFLICT_TEXT.get(e.field, 'Не удалось создать аккаунт.')}\nНачните заново.", reply_markup=kb.auth_kb()
        )
        return
    await state.clear()
    await repo.get_or_create_user(account["user_key"])
    await message.answer(
        f"✅ Аккаунт <b>{_esc(login)}</b> создан. Вы вошли в бот.\n"
        "На сайте можно войти тем же логином (или email) и паролем: данные общие.\n\n"
        "Дальше: «📄 Резюме», «⚙️ Фильтры», «🔍 Поиск вакансий».",
        reply_markup=kb.main_menu(),
    )


def _esc(value: str) -> str:
    return html.escape(value)


# ---------- вход: логин или email → пароль ----------
@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext, account: dict | None = None) -> None:
    if text := _already(account):
        await message.answer(text)
        return
    await _start_login(message, state)


@router.callback_query(F.data == "auth:login")
async def cb_login(cb: CallbackQuery, state: FSMContext, account: dict | None = None) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        if text := _already(account):
            await cb.message.answer(text)
            return
        await _start_login(cb.message, state)


async def _start_login(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AuthStates.login_ident)
    await message.answer(
        "Вход в аккаунт (созданный в боте или на сайте). Нужны логин, email и пароль.\n"
        "Шаг 1/3. Введите логин.\nОтмена: /cancel"
    )


@router.message(AuthStates.login_ident, F.text)
async def login_ident(message: Message, state: FSMContext) -> None:
    login = message.text.strip()[:64]
    if not login:
        await message.answer("Введите логин.")
        return
    await state.update_data(login=login)
    await state.set_state(AuthStates.login_email)
    await message.answer("Шаг 2/3. Введите email аккаунта.")


@router.message(AuthStates.login_email, F.text)
async def login_email(message: Message, state: FSMContext) -> None:
    email = message.text.strip()[:254]
    if not email:
        await message.answer("Введите email.")
        return
    await state.update_data(email=email)
    await state.set_state(AuthStates.login_password)
    await message.answer("Шаг 3/3. Введите пароль. Я сразу удалю это сообщение из чата.")


MERGE_TITLES = {"resume": "резюме", "filters": "фильтры", "analyses": "результаты анализа"}


def _merge_note(report: dict) -> str:
    note = ""
    if report.get("moved"):
        note += "\nИз бота добавлены в аккаунт: " + ", ".join(MERGE_TITLES[m] for m in report["moved"]) + "."
    if report.get("resume_conflict"):
        note += "\nВ аккаунте уже было своё резюме, поэтому резюме, добавленное в бот раньше, не перенесено."
    return note


@router.message(AuthStates.login_password, F.text)
async def login_password(message: Message, state: FSMContext) -> None:
    password = message.text[:200]
    await _forget(message)
    telegram_id = message.from_user.id
    if _limited("login", telegram_id, 8, 900):
        await state.clear()
        await message.answer("Слишком много попыток входа. Попробуйте через 15 минут.")
        return
    data = await state.get_data()
    await state.clear()

    try:
        account = await accounts.authenticate(data.get("login", ""), data.get("email", ""), password)
    except accounts.AuthError as e:
        await message.answer(f"⚠️ {_esc(e.message)}", reply_markup=kb.auth_kb())
        return

    linked = account.get("telegram_id")
    if linked and linked != telegram_id:
        await message.answer(
            "Этот аккаунт уже подключён к другому Telegram. Выйдите из него там (кнопка «🚪 Выйти»), "
            "затем войдите здесь.",
            reply_markup=kb.auth_kb(),
        )
        return
    if not linked and not await accounts.link_telegram(account["_id"], telegram_id):
        await message.answer("Не удалось подключить аккаунт. Попробуйте ещё раз.", reply_markup=kb.auth_kb())
        return

    await repo.get_or_create_user(account["user_key"])
    try:  # данные, заведённые в боте до появления аккаунтов, присоединяются к аккаунту
        note = _merge_note(await repo.merge_legacy_user(telegram_id, account["user_key"]))
    except Exception:  # noqa: BLE001 — вход не должен ломаться из-за переноса
        log.exception("Не удалось перенести данные бота в аккаунт")
        note = ""
    await message.answer(
        f"✅ Вы вошли как <b>{_esc(account.get('login') or account['email'])}</b>. "
        f"Резюме и фильтры подтянулись из аккаунта.{note}",
        reply_markup=kb.main_menu(),
    )
