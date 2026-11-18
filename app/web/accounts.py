"""Общие аккаунты сайта и Telegram-бота: логин + email + пароль (scrypt из stdlib).

Аккаунт хранит user_key: именно под ним лежат резюме, фильтры и анализы в коллекциях users / analysis_logs.
Бот привязывается к аккаунту полем telegram_id, поэтому и сайт, и бот работают с одними и теми же данными.
Сессии сайта хранятся в MongoDB."""
import asyncio
import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from app.db import mongo

SESSION_DAYS = 30
MIN_PASSWORD = 8
_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")
_N, _R, _P = 2**14, 8, 1


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def normalize_login(login: str) -> str:
    return (login or "").strip().lower()


def valid_login(login: str) -> bool:
    return bool(_LOGIN_RE.match(login))


class AccountConflict(Exception):
    """Поле занято: field = "email" | "login" | "telegram"."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


class AuthError(Exception):
    """Вход не удался. message можно показывать пользователю, status подсказывает HTTP-код для сайта."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.message, self.status = message, status


BAD_CREDENTIALS = "Неверный логин, пароль или email."
_DUMMY_HASH = "scrypt$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 43 + "="


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) and len(email) <= 254


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)


async def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = await asyncio.to_thread(_scrypt, password, salt)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


async def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    except ValueError:
        return False
    digest = await asyncio.to_thread(_scrypt, password, salt)
    return hmac.compare_digest(digest, expected)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _random_user_key() -> int:
    return -(secrets.randbits(62) + 1)  # у Telegram id всегда положительные


async def create_account(
    name: str,
    email: str,
    password: str = "",
    login: str | None = None,
    *,
    password_hash: str | None = None,
    telegram_id: int | None = None,
) -> dict:
    """Создаёт аккаунт. Бросает AccountConflict, если email / логин / Telegram уже заняты.

    Для аккаунта, созданного в боте, user_key = telegram_id, если он свободен: так сохраняются резюме
    и фильтры, которые пользователь уже завёл в боте до появления аккаунтов. Иначе берётся случайный
    отрицательный ключ.
    """
    password_hash = password_hash or await hash_password(password)
    key = _random_user_key()
    if telegram_id is not None and not await mongo.web_accounts.find_one({"user_key": telegram_id}):
        key = telegram_id

    for _ in range(3):
        doc = {
            "email": email,
            "name": name,
            "password_hash": password_hash,
            "user_key": key,
            "created_at": _now(),
        }
        if login:
            doc["login"] = login
        if telegram_id is not None:
            doc["telegram_id"] = telegram_id
        try:
            res = await mongo.web_accounts.insert_one(doc)
        except DuplicateKeyError:
            # что именно занято, проверяем запросами: так не зависим от формата сообщения драйвера
            if await mongo.web_accounts.find_one({"email": email}):
                raise AccountConflict("email") from None
            if login and await mongo.web_accounts.find_one({"login": login}):
                raise AccountConflict("login") from None
            if telegram_id is not None and await mongo.web_accounts.find_one({"telegram_id": telegram_id}):
                raise AccountConflict("telegram") from None
            key = _random_user_key()  # занят user_key: пробуем другой
            continue
        return {**doc, "_id": res.inserted_id}
    raise RuntimeError("Не удалось выделить user_key для аккаунта")


async def get_account_by_email(email: str) -> dict | None:
    return await mongo.web_accounts.find_one({"email": email})


async def claim_login(account_id, login: str) -> bool:
    """Закрепляет логин за старым аккаунтом, у которого его ещё нет. False, если логин занят."""
    try:
        res = await mongo.web_accounts.update_one({"_id": account_id, "login": None}, {"$set": {"login": login}})
    except DuplicateKeyError:
        return False
    return res.modified_count == 1


async def authenticate(login: str, email: str, password: str) -> dict:
    """Вход по логину, паролю и email. Возвращает аккаунт или бросает AuthError.

    Все три значения должны относиться к одному аккаунту. Пароль проверяется и для несуществующего
    аккаунта, чтобы по времени ответа и тексту ошибки нельзя было узнать, кто зарегистрирован.
    Аккаунт, созданный до появления логинов, при первом успешном входе получает введённый логин.
    """
    login, email = normalize_login(login), normalize_email(email)
    account = await get_account_by_email(email) if email else None
    ok = await verify_password(password, account["password_hash"] if account else _DUMMY_HASH)
    if not account or not ok:
        raise AuthError(BAD_CREDENTIALS)

    own = account.get("login")
    if own:
        if own != login:
            raise AuthError(BAD_CREDENTIALS)
        return account

    # старый аккаунт без логина: пароль и email верны, значит можно закрепить за ним логин
    if not valid_login(login):
        raise AuthError(
            "У этого аккаунта ещё нет логина. Введите новый: 3–32 символа, латинские буквы, цифры и знаки _ . -",
            422,
        )
    if await mongo.web_accounts.find_one({"login": login}) or not await claim_login(account["_id"], login):
        raise AuthError("Этот логин уже занят. Введите другой.", 409)
    return {**account, "login": login}


async def get_account_by_identifier(identifier: str) -> dict | None:
    """Ищет по логину или по email (если в строке есть «@»)."""
    value = (identifier or "").strip().lower()
    if not value:
        return None
    field = "email" if "@" in value else "login"
    return await mongo.web_accounts.find_one({field: value})


async def get_account_by_telegram(telegram_id: int) -> dict | None:
    return await mongo.web_accounts.find_one({"telegram_id": telegram_id})


async def link_telegram(account_id, telegram_id: int) -> bool:
    """Привязывает Telegram к аккаунту. False, если у аккаунта уже другой Telegram или этот Telegram занят."""
    try:
        res = await mongo.web_accounts.update_one(
            {"_id": account_id, "telegram_id": None}, {"$set": {"telegram_id": telegram_id}}
        )
    except DuplicateKeyError:
        return False
    return res.modified_count == 1


async def unlink_telegram(telegram_id: int) -> None:
    await mongo.web_accounts.update_one({"telegram_id": telegram_id}, {"$unset": {"telegram_id": ""}})


async def create_session(account_id) -> str:
    token = secrets.token_urlsafe(32)
    await mongo.web_sessions.insert_one(
        {"token_hash": _token_hash(token), "account_id": account_id, "expires_at": _now() + timedelta(days=SESSION_DAYS)}
    )
    return token


async def account_for_token(token: str | None) -> dict | None:
    if not token or not (20 <= len(token) <= 100):
        return None
    sess = await mongo.web_sessions.find_one({"token_hash": _token_hash(token)})
    if not sess or sess["expires_at"] <= _now():
        return None
    return await mongo.web_accounts.find_one({"_id": sess["account_id"]})


async def delete_session(token: str | None) -> None:
    if token:
        await mongo.web_sessions.delete_one({"token_hash": _token_hash(token)})
