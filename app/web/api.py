"""Веб-API: те же сервисы, что и у Telegram-бота, но для браузера.

Аккаунты: логин + email + пароль, серверная сессия в httpOnly-cookie. Аккаунт общий с Telegram-ботом:
бот привязывается к нему по telegram_id, а резюме, фильтры и анализы лежат под user_key аккаунта,
поэтому сайт и бот видят одни и те же данные. Вся существующая логика (репозитории, анализ, письма)
работает без изменений.
"""
import asyncio
import json
import logging
import time
from collections import defaultdict, deque

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import container
from app import repositories as repo
from app.config import settings
from app.demo_vacancies import DEMO_VACANCIES  # noqa: F401 — реэкспорт для обратной совместимости
from app.services.ai_service import AIError
from app.services.analysis_service import AnalysisItem, ProfileError, SearchBusyError
from app.services.chat_service import ChatInputError
from app.services.resume_parser import MAX_FILE_BYTES, MAX_RESUME_CHARS, ResumeParseError
from app.services.scraper import HH_AREAS, HH_EMPLOYMENT, HHError
from app.web import accounts

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

COOKIE = "sid"
RESUME_PREVIEW_CHARS = 600
NAME_PLACEHOLDER = "[Ваше имя]"  # так ИИ подписывает письма

def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def _set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE, token, max_age=accounts.SESSION_DAYS * 86400, httponly=True,
        samesite="lax", secure=_is_https(request), path="/",
    )


async def _account(request: Request) -> dict:
    acc = await accounts.account_for_token(request.cookies.get(COOKIE))
    if not acc:
        raise HTTPException(401, "Войдите в аккаунт.")
    return acc


# ---------- ограничения (в памяти процесса) ----------
_hits: dict[str, deque[float]] = defaultdict(deque)
_featured_cache: tuple[float, dict] = (0, {})


def _limit(bucket: str, key: str, limit: int, window: float, message: str) -> None:
    if limit <= 0:
        return
    now = time.monotonic()
    q = _hits[f"{bucket}:{key}"]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, message)
    q.append(now)


def _check_search_limit(ip: str, account_id: str) -> None:
    msg = "Слишком много поисков за последний час. Попробуйте позже."
    _limit("search-ip", ip, settings.web_searches_per_hour * 3, 3600, msg)
    _limit("search-acc", account_id, settings.web_searches_per_hour, 3600, msg)


def _check_auth_limit(ip: str) -> None:
    _limit("auth", ip, 12, 900, "Слишком много попыток входа. Подождите 15 минут.")


# ---------- сериализация ----------
def _profile(user: dict) -> dict:
    resume = user.get("resume_text") or ""
    f = user.get("filters") or {}
    return {
        "resume": {
            "has": bool(resume),
            "chars": len(resume),
            "name": (user.get("resume_name") or "") if resume else "",
            "preview": resume[:RESUME_PREVIEW_CHARS],
        },
        "filters": {
            "keywords": f.get("keywords") or [],
            "location": f.get("location") or "",
            "employment_type": f.get("employment_type") or "",
        },
        "options": {
            "locations": list(HH_AREAS),
            "employment": list(HH_EMPLOYMENT),
        },
    }


def _card(item: AnalysisItem) -> dict:
    v, a = item.vacancy, item.analysis
    return {
        "id": str(v["_id"]),
        "title": v["title"],
        "company": v.get("company") or "",
        "url": v.get("url") or "",
        "skills": v.get("skills") or [],
        "match_score": a["match_score"],
        "missing_skills": a.get("missing_skills") or [],
        "cached": item.cached,
    }


def _featured_card(vacancy: dict) -> dict:
    return {
        "id": str(vacancy["_id"]),
        "title": vacancy.get("title") or "",
        "company": vacancy.get("company") or "",
        "url": vacancy.get("url") or "",
        "skills": vacancy.get("skills") or [],
        "summary": vacancy.get("description") or "",
        "is_demo": False,
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------- аккаунт ----------
def _me(acc: dict, user: dict) -> dict:
    p = _profile(user)
    p["account"] = {
        "name": acc["name"],
        "login": acc.get("login") or "",
        "email": acc["email"],
        "telegram_linked": bool(acc.get("telegram_id")),
    }
    p["onboarded"] = p["resume"]["has"] and bool(p["filters"]["keywords"])
    return p


async def _me_for(acc: dict) -> dict:
    return _me(acc, await repo.get_or_create_user(acc["user_key"]))


class RegisterIn(BaseModel):
    login: str = Field(max_length=64)
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    name: str = Field(default="", max_length=80)


class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


LOGIN_RULES = "Логин: 3–32 символа, латинские буквы, цифры и знаки _ . -, начинается с буквы или цифры."


@router.post("/auth/register")
async def register(body: RegisterIn, request: Request, response: Response):
    _check_auth_limit(_client_ip(request))
    login, email = accounts.normalize_login(body.login), accounts.normalize_email(body.email)
    if not accounts.valid_login(login):
        raise HTTPException(422, LOGIN_RULES)
    if not accounts.valid_email(email):
        raise HTTPException(422, "Проверьте адрес электронной почты.")
    if len(body.password) < accounts.MIN_PASSWORD:
        raise HTTPException(422, f"Пароль должен быть не короче {accounts.MIN_PASSWORD} символов.")
    name = body.name.strip() or login  # имя попадает в подпись письма; по умолчанию это логин
    try:
        acc = await accounts.create_account(name, email, body.password, login)
    except accounts.AccountConflict as e:
        if e.field == "login":
            raise HTTPException(409, "Этот логин уже занят. Выберите другой.")
        raise HTTPException(409, "Этот email уже зарегистрирован. Войдите в аккаунт.")
    _set_cookie(request, response, await accounts.create_session(acc["_id"]))
    return await _me_for(acc)


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response):
    _check_auth_limit(_client_ip(request))
    try:
        acc = await accounts.authenticate(body.login, body.email, body.password)
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)
    _set_cookie(request, response, await accounts.create_session(acc["_id"]))
    return await _me_for(acc)


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    await accounts.delete_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    return await _me_for(await _account(request))


class ResumeIn(BaseModel):
    text: str = Field(max_length=200_000)


@router.put("/resume")
async def put_resume(body: ResumeIn, request: Request, response: Response):
    acc = await _account(request)
    uid = acc["user_key"]
    user = await repo.get_or_create_user(uid)
    if (user.get("resume_text") or "").strip():
        raise HTTPException(409, "Сначала удалите сохранённое резюме.")
    try:
        text = container.resume_parser.normalize(body.text)
    except ResumeParseError:
        raise HTTPException(422, "Слишком коротко. Опишите опыт, навыки и образование подробнее.")
    if not await repo.set_resume_if_empty(uid, text[:MAX_RESUME_CHARS], ""):
        raise HTTPException(409, "Сначала удалите сохранённое резюме.")
    return await _me_for(acc)


def _clean_filename(filename: str) -> str:
    return " ".join((filename or "").replace("\\", "/").split("/")[-1].split())[:120]


@router.get("/resume")
async def get_resume(request: Request):
    """Полный текст сохранённого резюме (для кнопки «Посмотреть файл»)."""
    acc = await _account(request)
    user = await repo.get_or_create_user(acc["user_key"])
    text = user.get("resume_text") or ""
    if not text.strip():
        raise HTTPException(404, "Резюме не добавлено.")
    return {"name": user.get("resume_name") or "", "chars": len(text), "text": text}


@router.delete("/resume")
async def delete_resume(request: Request):
    acc = await _account(request)
    uid = acc["user_key"]
    await repo.get_or_create_user(uid)
    await repo.clear_resume(uid)
    return await _me_for(acc)


@router.post("/resume/file")
async def post_resume_file(request: Request, response: Response, filename: str = ""):
    """Тело запроса — сырые байты файла (без multipart, чтобы не тянуть лишние зависимости)."""
    acc = await _account(request)
    uid = acc["user_key"]
    user = await repo.get_or_create_user(uid)
    if (user.get("resume_text") or "").strip():
        raise HTTPException(409, "Сначала удалите сохранённое резюме.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
        raise HTTPException(413, "Файл слишком большой (максимум 5 МБ).")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, "Файл слишком большой (максимум 5 МБ).")
    try:
        text = await container.resume_parser.parse(bytes(data), filename)
    except ResumeParseError as e:
        raise HTTPException(422, str(e))
    if not await repo.set_resume_if_empty(uid, text, _clean_filename(filename)):
        raise HTTPException(409, "Сначала удалите сохранённое резюме.")
    return await _me_for(acc)


class FiltersIn(BaseModel):
    keywords: list[str] = Field(max_length=10)
    location: str = Field(default="Ташкент", max_length=100)
    employment_type: str = Field(default="полная", max_length=30)


@router.put("/filters")
async def put_filters(body: FiltersIn, request: Request, response: Response):
    acc = await _account(request)
    uid = acc["user_key"]
    keywords = [k.strip()[:80] for k in body.keywords if k.strip()]
    if not keywords:
        raise HTTPException(422, "Укажите хотя бы одну должность.")
    await repo.get_or_create_user(uid)
    await repo.update_filters(
        uid,
        keywords=keywords[:10],
        location=body.location.strip() or "Ташкент",
        employment_type=body.employment_type.strip().lower(),
    )
    return await _me_for(acc)


# ---------- актуальные вакансии ----------
def _demo_items() -> list[dict]:
    return [
        {key: vacancy[key] for key in ("id", "title", "company", "skills", "summary", "is_demo")}
        for vacancy in DEMO_VACANCIES.values()
    ]


def _demo_result(reason: str) -> dict:
    return {
        "items": _demo_items(),
        "source": "demo",
        "notice": (
            f"{reason} Ниже — оригинальные демонстрационные примеры, "
            "это не реальные объявления и на них нельзя откликнуться."
        ),
    }


@router.get("/vacancies/demo")
async def demo_vacancies(request: Request):
    """Демо-карточки без обращения к hh.ru: запасной вариант, когда поиск не дал результата."""
    _limit(
        "demo-ip", _client_ip(request), 60, 60,
        "Слишком много запросов. Попробуйте через минуту.",
    )
    return _demo_result("Реальные вакансии сейчас недоступны.")


@router.get("/vacancies/featured")
async def featured_vacancies(request: Request):
    global _featured_cache
    _limit(
        "featured-ip", _client_ip(request), 20, 60,
        "Слишком много запросов. Попробуйте через минуту.",
    )
    expires_at, cached = _featured_cache
    if time.monotonic() < expires_at:
        return cached

    try:
        vacancies = await container.scraper.search(
            {
                "keywords": ["Python", "JavaScript", "QA", "DevOps", "аналитик данных"],
                "location": "Узбекистан",
                "employment_type": "полная",
            },
            limit=8,
        )
    except HHError as e:
        log.warning("Не удалось загрузить актуальные вакансии hh.ru: %s", e)
        result = _demo_result(f"hh.ru не отдал актуальные вакансии ({e}).")
        _featured_cache = (time.monotonic() + 900, result)
        return result
    except Exception:  # noqa: BLE001 — БД/сеть/формат ответа: лучше показать демо, чем пустой экран
        log.exception("Ошибка при загрузке актуальных вакансий")
        return _demo_result("Не удалось загрузить актуальные вакансии.")

    if not vacancies:
        result = _demo_result("hh.ru не вернул подходящих вакансий.")
        _featured_cache = (time.monotonic() + 300, result)
        return result

    result = {"items": [_featured_card(vacancy) for vacancy in vacancies], "source": "hh"}
    _featured_cache = (time.monotonic() + 900, result)
    return result


@router.get("/vacancy/{vacancy_id}")
async def vacancy_details(vacancy_id: str):
    demo = DEMO_VACANCIES.get(vacancy_id)
    if demo:
        return dict(demo)
    try:
        oid = ObjectId(vacancy_id)
    except InvalidId:
        raise HTTPException(404, "Вакансия не найдена.")
    vacancy = await repo.get_vacancy(oid)
    if not vacancy:
        raise HTTPException(404, "Вакансия не найдена.")
    try:
        details = await container.scraper.get_vacancy_details(vacancy)
    except HHError as e:
        log.warning("Не удалось загрузить описание вакансии %s: %s", vacancy_id, e)
        # hh.ru недоступен или блокирует запрос: показываем всё, что уже сохранено у нас
        return _stored_details(
            vacancy_id, vacancy,
            "hh.ru сейчас не отдаёт полное описание, поэтому показаны данные, "
            "сохранённые при поиске. Полный текст — по ссылке на hh.ru.",
        )
    if not details:
        raise HTTPException(404, "Вакансия больше не опубликована на hh.ru.")
    details["id"] = vacancy_id
    return details


def _stored_details(vacancy_id: str, vacancy: dict, notice: str) -> dict:
    parts = []
    if vacancy.get("responsibilities"):
        parts.append("Обязанности:\n" + vacancy["responsibilities"])
    if vacancy.get("requirements"):
        parts.append("Требования:\n" + vacancy["requirements"])
    description = "\n\n".join(parts) or vacancy.get("description") or ""
    return {
        "id": vacancy_id,
        "title": vacancy.get("title") or "",
        "company": vacancy.get("company") or "",
        "description": description,
        "skills": vacancy.get("skills") or [],
        "location": "",
        "salary": vacancy.get("salary"),
        "experience": "",
        "employment": "",
        "schedule": "",
        "published_at": "",
        "url": vacancy.get("url") or "",
        "professional_roles": [],
        "is_demo": False,
        "notice": notice,
    }


# ---------- поиск (Server-Sent Events) ----------
@router.get("/search")
async def search(request: Request, response: Response):
    acc = await _account(request)
    uid = acc["user_key"]
    ip = _client_ip(request)
    # быстрые проверки до начала потока, чтобы вернуть нормальный HTTP-статус
    user = await repo.get_or_create_user(uid)
    if not (user.get("resume_text") or "").strip():
        raise HTTPException(422, "Сначала добавьте резюме.")
    if not (user.get("filters") or {}).get("keywords"):
        raise HTTPException(422, "Сначала задайте должности в фильтрах.")
    _check_search_limit(ip, str(acc["_id"]))

    queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

    async def on_plan(n_new: int, n_cached: int) -> None:
        await queue.put(("plan", {"analyzing": n_new, "cached": n_cached}))

    async def on_stage(text: str) -> None:
        await queue.put(("stage", {"text": text}))

    async def produce() -> None:
        shown = failed = rejected = 0
        try:
            async for item in container.analysis.search(uid, on_plan=on_plan, on_stage=on_stage):
                if item.rejected:  # ИИ счёл вакансию неподходящей под резюме: не показываем
                    rejected += 1
                    continue
                if item.analysis is None:
                    failed += 1
                    await queue.put(("failed", {"id": str(item.vacancy["_id"])}))
                    continue
                shown += 1
                await queue.put(("card", _card(item)))
            await queue.put(("done", {"shown": shown, "failed": failed, "rejected": rejected}))
        except ProfileError as e:
            await queue.put(("error", {"message": str(e)}))
        except SearchBusyError:
            await queue.put(("error", {"message": "Предыдущий поиск ещё выполняется, подождите немного."}))
        except HHError as e:
            log.warning("Ошибка hh.ru: %s", e)
            await queue.put(("error", {"message": str(e), "code": "hh_unavailable"}))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка веб-поиска")
            await queue.put(("error", {"message": "Что-то пошло не так. Попробуйте позже."}))
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(produce())
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # держим соединение живым за прокси
                    continue
                if msg is None:
                    break
                yield _sse(*msg)
        finally:
            task.cancel()  # пользователь закрыл вкладку, поиск останавливается

    headers = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


# ---------- чат с ИИ (только о работе) ----------
class ChatIn(BaseModel):
    message: str = Field(max_length=2000)  # точное ограничение и понятная ошибка в ChatService


def _check_chat_limit(ip: str, account_id: str) -> None:
    msg = "Слишком много сообщений. Подождите несколько минут."
    _limit("chat-ip", ip, 60, 600, msg)
    _limit("chat-acc", account_id, 20, 600, msg)


@router.get("/chat")
async def chat_history(request: Request):
    acc = await _account(request)
    return {"messages": await container.chat.history(acc["user_key"])}


@router.post("/chat")
async def chat(body: ChatIn, request: Request):
    acc = await _account(request)
    _check_chat_limit(_client_ip(request), str(acc["_id"]))
    try:
        result = await container.chat.ask(acc["user_key"], body.message)
    except (ChatInputError, ProfileError) as e:
        raise HTTPException(422, str(e))
    except AIError as e:
        log.warning("Чат: ИИ недоступен: %s", e)
        raise HTTPException(503, "ИИ сейчас недоступен. Попробуйте чуть позже.")
    return {
        "reply": result.reply,
        "vacancies": result.vacancies,
        "off_topic": result.off_topic,
        "notice": result.notice,
    }


@router.delete("/chat")
async def chat_clear(request: Request):
    acc = await _account(request)
    await container.chat.clear(acc["user_key"])
    return {"messages": []}


# ---------- письмо ----------
@router.get("/letter/{vacancy_id}")
async def letter(vacancy_id: str, request: Request, response: Response):
    acc = await _account(request)
    uid = acc["user_key"]
    try:
        oid = ObjectId(vacancy_id)
    except InvalidId:
        raise HTTPException(404, "Вакансия не найдена.")
    found = await container.analysis.get_cover_letter(uid, oid)
    if not found:
        raise HTTPException(409, "Резюме изменилось или анализ устарел. Запустите поиск заново.")
    vacancy, analysis = found
    return {
        "title": vacancy["title"],
        "company": vacancy.get("company") or "",
        "text": analysis["generated_cover_letter"].replace(NAME_PLACEHOLDER, acc["name"]),
    }
