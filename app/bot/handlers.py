import html
import io
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, LinkPreviewOptions, Message, ReplyKeyboardRemove
from bson import ObjectId
from bson.errors import InvalidId

from app import container
from app import repositories as repo
from app.demo_vacancies import DEMO_VACANCIES
from app.bot import auth
from app.bot import keyboards as kb
from app.bot.states import ChatStates, FilterStates, ResumeStates
from app.services.ai_service import AIError
from app.services.analysis_service import AnalysisItem, ProfileError, SearchBusyError
from app.services.chat_service import ChatInputError
from app.services.resume_parser import MAX_FILE_BYTES, MAX_RESUME_CHARS, ResumeParseError
from app.services.scraper import HHError
from app.utils import split_message
from app.web import accounts

log = logging.getLogger(__name__)
router = Router()
router.message.outer_middleware(auth.AccountMiddleware())
router.callback_query.outer_middleware(auth.AccountMiddleware())
NAME_PLACEHOLDER = "[Ваше имя]"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
esc = html.escape

WELCOME = (
    "👋 Привет! Я помогаю с поиском работы:\n"
    "• оцениваю, насколько резюме подходит вакансии (Match Score);\n"
    "• показываю, каких навыков не хватает;\n"
    "• пишу персональное сопроводительное письмо.\n\n"
    "Начните так:\n0️⃣ Создайте аккаунт (логин, пароль, email) или войдите: он общий с сайтом\n"
    "1️⃣ «📄 Резюме»: пришлите текст или файл (.pdf / .docx / .txt)\n"
    "2️⃣ «⚙️ Фильтры»: должность, город, тип занятости\n3️⃣ «🔍 Поиск вакансий»: ИИ сначала прочитает резюме, "
    "затем покажет только подходящие вакансии\n4️⃣ «💬 Чат с ИИ»: вопросы только о работе и карьере"
)


async def _safe_edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except TelegramBadRequest:  # «message is not modified» и т.п.
        pass


# ---------- меню и команды (без фильтра состояния, поэтому сбрасывают FSM) ----------
@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext, account: dict | None = None) -> None:
    await state.clear()
    if not account:
        await message.answer(f"{WELCOME}\n\n{auth.AUTH_PROMPT}", reply_markup=kb.auth_kb())
        return
    await repo.get_or_create_user(account["user_key"])
    await message.answer(WELCOME, reply_markup=kb.main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, account: dict | None = None) -> None:
    await state.clear()
    if account:
        await message.answer("Отменено.", reply_markup=kb.main_menu())
    else:
        await message.answer("Отменено.", reply_markup=kb.auth_kb())


@router.message(Command("profile"))
@router.message(F.text == kb.BTN_PROFILE)
async def cmd_profile(message: Message, state: FSMContext, account: dict) -> None:
    await state.clear()
    user = await repo.get_or_create_user(account["user_key"])
    f = user["filters"]
    resume = user.get("resume_text") or ""
    await message.answer(
        "👤 <b>Ваш профиль</b>\n"
        f"Аккаунт: {esc(account.get('login') or account['email'])} (общий с сайтом)\n"
        f"Резюме: {'✅ ' + _resume_label(user) if resume else '❌ не добавлено'}\n"
        f"Должности: {esc(', '.join(f['keywords'])) or '—'}\n"
        f"Локация: {esc(f['location']) or '—'}\n"
        f"Тип занятости: {esc(f['employment_type']) or '—'}"
    )


def _resume_label(user: dict) -> str:
    name = (user.get("resume_name") or "").strip()
    chars = len(user.get("resume_text") or "")
    return f"{esc(name)}, {chars} символов" if name else f"{chars} символов"


RESUME_EXISTS = (
    "📄 <b>Резюме уже сохранено</b>\n{label}\n\n"
    "Можно хранить только одно резюме. Чтобы добавить другое, сначала удалите это."
)
RESUME_EXISTS_SHORT = "⚠️ Резюме уже сохранено. Сначала удалите его через «📄 Резюме», потом можно добавить новое."


@router.message(Command("logout"))
@router.message(F.text == kb.BTN_LOGOUT)
async def cmd_logout(message: Message, state: FSMContext, account: dict) -> None:
    """Отвязывает этот Telegram от аккаунта. Сам аккаунт и его данные остаются, войти можно снова."""
    await state.clear()
    await accounts.unlink_telegram(message.from_user.id)
    await message.answer(
        f"🚪 Вы вышли из аккаунта <b>{esc(account.get('login') or account['email'])}</b>.\n"
        "Данные сохранены в аккаунте: войдите снова командой /login или на сайте.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(auth.AUTH_PROMPT, reply_markup=kb.auth_kb())


@router.message(Command("resume"))
@router.message(F.text == kb.BTN_RESUME)
async def cmd_resume(message: Message, state: FSMContext, account: dict) -> None:
    user = await repo.get_or_create_user(account["user_key"])
    if (user.get("resume_text") or "").strip():
        await state.clear()  # второе резюме добавить нельзя: показываем текущее с кнопками
        await message.answer(RESUME_EXISTS.format(label=_resume_label(user)), reply_markup=kb.resume_kb())
        return
    await state.set_state(ResumeStates.waiting)
    await message.answer("Пришлите резюме текстом или файлом (.pdf / .docx / .txt, до 5 МБ).\nОтмена: /cancel")


@router.message(Command("filters"))
@router.message(F.text == kb.BTN_FILTERS)
async def cmd_filters(message: Message, state: FSMContext, account: dict) -> None:
    await repo.get_or_create_user(account["user_key"])
    await state.set_state(FilterStates.keywords)
    await message.answer(
        "Шаг 1/3. Введите желаемые должности через запятую.\n"
        "Например: <i>Разнорабочий, Стажер Python</i>\nОтмена: /cancel"
    )


# ---------- резюме ----------
@router.message(ResumeStates.waiting, F.document)
async def resume_file(message: Message, state: FSMContext, bot: Bot, account: dict) -> None:
    doc = message.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await message.answer("Файл слишком большой (максимум 5 МБ).")
        return
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    try:
        text = await container.resume_parser.parse(buf.getvalue(), doc.file_name or "")
    except ResumeParseError as e:
        await message.answer(f"⚠️ {esc(str(e))}")
        return
    name = " ".join((doc.file_name or "").split())[:120]
    await _save_resume_once(message, state, text, name, account)


@router.message(ResumeStates.waiting, F.text)
async def resume_text(message: Message, state: FSMContext, account: dict) -> None:
    try:
        text = container.resume_parser.normalize(message.text)
    except ResumeParseError:
        await message.answer("Слишком коротко. Опишите опыт, навыки и образование подробнее.")
        return
    await _save_resume_once(message, state, text[:MAX_RESUME_CHARS], "", account)


async def _save_resume_once(message: Message, state: FSMContext, text: str, name: str, account: dict) -> None:
    """Сохраняет резюме, только если его ещё нет (атомарно): второе добавить нельзя."""
    saved = await repo.set_resume_if_empty(account["user_key"], text, name)
    await state.clear()
    if not saved:
        await message.answer(RESUME_EXISTS_SHORT, reply_markup=kb.resume_kb())
        return
    await message.answer(f"✅ Резюме сохранено ({len(text)} символов). Теперь можно искать вакансии.")


@router.message(ResumeStates.waiting)
async def resume_other(message: Message) -> None:
    await message.answer("Пришлите текст или файл .pdf / .docx / .txt.")


# ---------- фильтры (FSM) ----------
@router.message(FilterStates.keywords, F.text)
async def filters_keywords(message: Message, state: FSMContext, account: dict) -> None:
    keywords = [k.strip() for k in message.text.split(",") if k.strip()][:10]
    if not keywords:
        await message.answer("Укажите хотя бы одну должность.")
        return
    await repo.update_filters(account["user_key"], keywords=keywords)
    await state.set_state(FilterStates.location)
    await message.answer(
        "Шаг 2/3. Выберите город или введите свой текстом:", reply_markup=kb.location_kb()
    )


async def _ask_employment(message: Message, state: FSMContext) -> None:
    await state.set_state(FilterStates.employment)
    await message.answer("Шаг 3/3. Выберите тип занятости:", reply_markup=kb.employment_kb())


@router.callback_query(FilterStates.location, F.data.startswith("loc:"))
async def filters_location_btn(cb: CallbackQuery, state: FSMContext, account: dict) -> None:
    value = kb.LOCATIONS.get(cb.data.split(":", 1)[1])
    if not value or not isinstance(cb.message, Message):
        await cb.answer()
        return
    await repo.update_filters(account["user_key"], location=value)
    await cb.answer()
    await _safe_edit(cb.message, f"📍 Локация: {esc(value)}")
    await _ask_employment(cb.message, state)


@router.message(FilterStates.location, F.text)
async def filters_location_text(message: Message, state: FSMContext, account: dict) -> None:
    await repo.update_filters(account["user_key"], location=message.text.strip()[:100])
    await _ask_employment(message, state)


@router.callback_query(FilterStates.employment, F.data.startswith("emp:"))
async def filters_employment(cb: CallbackQuery, state: FSMContext, account: dict) -> None:
    value = kb.EMPLOYMENT.get(cb.data.split(":", 1)[1])
    if not value or not isinstance(cb.message, Message):
        await cb.answer()
        return
    await repo.update_filters(account["user_key"], employment_type=value)
    await state.clear()
    await cb.answer("Сохранено")
    await _safe_edit(cb.message, f"✅ Фильтры сохранены. Занятость: {value}.\nТеперь жмите «🔍 Поиск вакансий».")


# ---------- поиск и карточки ----------
def _score_bar(score: int) -> str:
    filled = round(score / 10)
    return "▓" * filled + "░" * (10 - filled)


def _score_icon(score: int) -> str:
    return "🟢" if score >= 70 else "🟡" if score >= 40 else "🔴"


def render_card(item: AnalysisItem) -> str:
    v, a = item.vacancy, item.analysis
    score = a["match_score"]
    missing = a.get("missing_skills") or []
    lines = [
        f"<b>{esc(v['title'])}</b>",
        f"🏢 {esc(v.get('company') or '—')}",
        "",
        f"{_score_icon(score)} Match Score: <b>{score}%</b>  {_score_bar(score)}",
        "⚠️ <b>Не хватает:</b> " + esc(", ".join(missing)) if missing else "✅ Ключевые требования закрыты",
    ]
    if v.get("url"):
        lines += ["", f'🔗 <a href="{esc(v["url"], quote=True)}">Открыть вакансию</a>']
    return "\n".join(lines)


@router.message(Command("search"))
@router.message(F.text == kb.BTN_SEARCH)
async def cmd_search(message: Message, state: FSMContext, account: dict) -> None:
    await state.clear()
    status = await message.answer("🤖 Начинаю: сначала ИИ прочитает ваше резюме…")

    async def on_plan(n_new: int, n_cached: int) -> None:
        if n_new:
            await _safe_edit(status, f"🤖 Анализирую вакансий: {n_new}. Карточки приходят по мере готовности…")
        elif n_cached:
            await _safe_edit(status, "Новых вакансий нет. Показываю ранее проанализированные (без затрат на ИИ).")

    async def on_stage(text: str) -> None:
        await _safe_edit(status, f"🤖 {esc(text)}")

    shown = failed = rejected = 0
    try:
        async for item in container.analysis.search(account["user_key"], on_plan=on_plan, on_stage=on_stage):
            if item.rejected:  # ИИ счёл вакансию неподходящей под резюме: не показываем
                rejected += 1
                continue
            if item.analysis is None:
                failed += 1
                continue
            await message.answer(
                render_card(item),
                reply_markup=kb.letter_kb(str(item.vacancy["_id"])),
                link_preview_options=NO_PREVIEW,
            )
            shown += 1
    except ProfileError as e:
        await _safe_edit(status, f"⚠️ {esc(str(e))}")
        return
    except SearchBusyError:
        await _safe_edit(status, "⏳ Предыдущий поиск ещё выполняется, подождите немного.")
        return
    except HHError as e:
        log.warning("Ошибка hh.ru: %s", e)
        await _show_demo_vacancies(message, status, f"😕 {esc(str(e))}")
        return
    except Exception:  # noqa: BLE001
        log.exception("Ошибка поиска")
        await _show_demo_vacancies(message, status, "😕 Что-то пошло не так.")
        return

    if shown:
        tail = f" (не удалось проанализировать: {failed})" if failed else ""
        await _safe_edit(status, f"✅ Готово: показано вакансий: {shown}{tail}.")
    elif failed:
        await _show_demo_vacancies(message, status, "😕 ИИ сейчас недоступен.")
    elif rejected:
        await _show_demo_vacancies(
            message, status, f"Нашлось вакансий: {rejected}, но ни одна не подходит под ваше резюме. Измените должности в «⚙️ Фильтры»."
        )
    else:
        await _show_demo_vacancies(message, status, "Ничего не нашёл по вашим фильтрам.")


# ---------- резюме: просмотр и удаление (с подтверждением) ----------
@router.callback_query(F.data == "resume:view")
async def on_resume_view(cb: CallbackQuery, account: dict) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    user = await repo.get_or_create_user(account["user_key"])
    text = (user.get("resume_text") or "").strip()
    if not text:
        await cb.answer("Резюме не добавлено.", show_alert=True)
        return
    await cb.answer()
    stem = (user.get("resume_name") or "").rsplit(".", 1)[0].strip() or "resume"
    await cb.message.answer_document(
        BufferedInputFile(text.encode("utf-8"), filename=f"{stem}.txt"),
        caption=f"📄 Текст вашего резюме ({len(text)} символов)",
    )


@router.callback_query(F.data == "resume:del")
async def on_resume_delete(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    await cb.answer()
    try:
        await cb.message.edit_text(
            "🗑 <b>Удалить резюме?</b>\nПосле удаления можно будет добавить одно новое.",
            reply_markup=kb.resume_delete_confirm_kb(),
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "resume:del_no")
async def on_resume_delete_cancel(cb: CallbackQuery, account: dict) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    user = await repo.get_or_create_user(account["user_key"])
    await cb.answer("Удаление отменено")
    try:
        if (user.get("resume_text") or "").strip():
            await cb.message.edit_text(RESUME_EXISTS.format(label=_resume_label(user)), reply_markup=kb.resume_kb())
        else:
            await cb.message.edit_text("Резюме не добавлено. Нажмите «📄 Резюме», чтобы добавить.")
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "resume:del_yes")
async def on_resume_delete_confirm(cb: CallbackQuery, account: dict) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    await repo.clear_resume(account["user_key"])
    await cb.answer("Резюме удалено")
    try:
        await cb.message.edit_text("🗑 Резюме удалено. Нажмите «📄 Резюме», чтобы добавить новое.")
    except TelegramBadRequest:
        pass


# ---------- чат с ИИ (только о работе) ----------
CHAT_INTRO = (
    "💬 <b>Чат с ИИ о работе</b>\n"
    "Спрашивайте только о работе и карьере: вакансии, зарплаты, профессии, навыки, резюме, собеседования. "
    "ИИ учитывает ваше резюме.\n\n"
    "Например:\n• Какие вакансии с высокой зарплатой мне подходят?\n• Что добавить в резюме для Junior Python?\n\n"
    "Выход: «⬅️ Выйти из чата» или /cancel"
)


@router.message(Command("chat"))
@router.message(F.text == kb.BTN_CHAT)
async def cmd_chat(message: Message, state: FSMContext, account: dict) -> None:
    user = await repo.get_or_create_user(account["user_key"])
    if not (user.get("resume_text") or "").strip():
        await state.clear()
        await message.answer("ИИ отвечает с учётом вашего резюме. Сначала добавьте его: «📄 Резюме».")
        return
    await state.set_state(ChatStates.active)
    await message.answer(CHAT_INTRO, reply_markup=kb.chat_kb())


@router.message(F.text == kb.BTN_CHAT_EXIT)
async def chat_exit(message: Message, state: FSMContext, account: dict | None = None) -> None:
    await state.clear()
    await message.answer("Вы вышли из чата.", reply_markup=kb.main_menu())


def render_chat_card(card: dict) -> str:
    lines = []
    if card.get("is_demo"):
        lines.append("🧪 <b>ДЕМО — не реальная вакансия</b>")
    lines += [f"<b>{esc(card['title'])}</b>", f"🏢 {esc(card.get('company') or '—')}"]
    if card.get("salary"):
        lines.append(f"💰 {esc(card['salary'])}")
    if card.get("match_score") is not None:
        lines.append(f"{_score_icon(card['match_score'])} Совпадение с резюме: <b>{card['match_score']}%</b>")
    if card.get("skills"):
        lines.append("🛠 " + esc(", ".join(card["skills"])))
    if card.get("url"):
        lines += ["", f'🔗 <a href="{esc(card["url"], quote=True)}">Открыть вакансию</a>']
    return "\n".join(lines)


@router.message(ChatStates.active, F.text)
async def chat_message(message: Message, state: FSMContext, account: dict) -> None:
    if auth._limited("chat", account["user_key"], 20, 600):
        await message.answer("Слишком много сообщений. Подождите несколько минут.")
        return
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:  # noqa: BLE001 — индикатор набора не критичен
        pass
    try:
        result = await container.chat.ask(account["user_key"], message.text)
    except (ChatInputError, ProfileError) as e:
        await message.answer(f"⚠️ {esc(str(e))}")
        return
    except AIError as e:
        log.warning("Чат: ИИ недоступен: %s", e)
        await message.answer("😕 ИИ сейчас недоступен. Попробуйте чуть позже.")
        return
    except Exception:  # noqa: BLE001
        log.exception("Ошибка чата")
        await message.answer("😕 Что-то пошло не так. Попробуйте позже.")
        return

    for part in split_message(esc(result.reply), 3500):
        await message.answer(part)
    if result.notice:
        await message.answer(f"ℹ️ {esc(result.notice)}")
    for card in result.vacancies:
        await message.answer(
            render_chat_card(card),
            reply_markup=kb.demo_kb(card["id"]) if card.get("is_demo") else None,
            link_preview_options=NO_PREVIEW,
        )


@router.message(ChatStates.active)
async def chat_other(message: Message) -> None:
    await message.answer("В чате можно писать только текстом.")


# ---------- демо-вакансии (запасной вариант, если поиск ничего не дал) ----------
def render_demo_card(vacancy: dict) -> str:
    lines = [
        "🧪 <b>ДЕМО — не реальная вакансия</b>",
        f"<b>{esc(vacancy['title'])}</b>",
        f"🏢 {esc(vacancy.get('company') or '—')}",
    ]
    if vacancy.get("summary"):
        lines += ["", esc(vacancy["summary"])]
    if vacancy.get("skills"):
        lines += ["", "🛠 " + esc(", ".join(vacancy["skills"]))]
    lines += ["", "Учебный пример: откликнуться на него нельзя."]
    return "\n".join(lines)


def render_demo_details(vacancy: dict) -> str:
    meta = [
        ("Местоположение", vacancy.get("location")),
        ("Опыт работы", vacancy.get("experience")),
        ("Занятость", vacancy.get("employment")),
        ("График", vacancy.get("schedule")),
        ("Специализация", ", ".join(vacancy.get("professional_roles") or [])),
    ]
    lines = [
        "🧪 <b>ДЕМО — не реальная вакансия</b>",
        f"<b>{esc(vacancy['title'])}</b>",
        f"🏢 {esc(vacancy.get('company') or '—')}",
        "",
    ]
    lines += [f"{label}: {esc(value)}" for label, value in meta if value]
    if vacancy.get("skills"):
        lines += ["", "<b>Ключевые навыки:</b> " + esc(", ".join(vacancy["skills"]))]
    lines += ["", "<b>Полное описание</b>", esc(vacancy.get("description") or "")]
    return "\n".join(lines)


async def _show_demo_vacancies(message: Message, status: Message, reason: str) -> None:
    await _safe_edit(
        status,
        f"{reason}\nПоказываю демонстрационные вакансии: это оригинальные учебные примеры, "
        "а не реальные объявления.",
    )
    for vacancy in DEMO_VACANCIES.values():
        await message.answer(
            render_demo_card(vacancy), reply_markup=kb.demo_kb(vacancy["id"]), link_preview_options=NO_PREVIEW
        )


@router.callback_query(F.data.startswith("demo:"))
async def on_demo_details(cb: CallbackQuery) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    vacancy = DEMO_VACANCIES.get(cb.data.split(":", 1)[1])
    if not vacancy:
        await cb.answer("Демо-вакансия не найдена.", show_alert=True)
        return
    await cb.answer()
    for part in split_message(render_demo_details(vacancy), 3500):
        await cb.message.answer(part, link_preview_options=NO_PREVIEW)


# ---------- письмо по кнопке ----------
@router.callback_query(F.data.startswith("cl:"))
async def on_letter(cb: CallbackQuery, account: dict) -> None:
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    try:
        vacancy_id = ObjectId(cb.data.split(":", 1)[1])
    except InvalidId:
        await cb.answer()
        return

    found = await container.analysis.get_cover_letter(account["user_key"], vacancy_id)
    if not found:
        await cb.answer("Резюме изменилось или анализ устарел. Запустите поиск заново.", show_alert=True)
        return
    await cb.answer()
    vacancy, analysis = found
    await cb.message.answer(f"✉️ <b>Сопроводительное письмо</b>\n{esc(vacancy['title'])}, {esc(vacancy.get('company') or '')}")
    letter = analysis["generated_cover_letter"].replace(NAME_PLACEHOLDER, account.get("name") or NAME_PLACEHOLDER)
    for part in split_message(letter, 3000):
        await cb.message.answer(f"<pre>{esc(part)}</pre>")  # моноширинный блок: копируется одним нажатием


# хэндлеры регистрации и входа подключаются в конце: они реагируют только на состояния AuthStates и auth:*
router.include_router(auth.router)
