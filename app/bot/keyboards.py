from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

BTN_SEARCH = "🔍 Поиск вакансий"
BTN_RESUME = "📄 Резюме"
BTN_FILTERS = "⚙️ Фильтры"
BTN_PROFILE = "👤 Профиль"
BTN_LOGOUT = "🚪 Выйти"
BTN_CHAT = "💬 Чат с ИИ"
BTN_CHAT_EXIT = "⬅️ Выйти из чата"
MENU_BUTTONS = {BTN_SEARCH, BTN_RESUME, BTN_FILTERS, BTN_PROFILE, BTN_LOGOUT, BTN_CHAT, BTN_CHAT_EXIT}

EMPLOYMENT = {"full": "полная", "part": "частичная", "intern": "стажировка"}
LOCATIONS = {"tashkent": "Ташкент", "uz": "Узбекистан"}


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SEARCH)],
            [KeyboardButton(text=BTN_RESUME), KeyboardButton(text=BTN_FILTERS)],
            [KeyboardButton(text=BTN_CHAT), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_LOGOUT)],
        ],
        resize_keyboard=True,
    )


def chat_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CHAT_EXIT)]],
        resize_keyboard=True,
        input_field_placeholder="Вопрос о работе или карьере",
    )


def location_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📍 Ташкент", callback_data="loc:tashkent"),
                InlineKeyboardButton(text="🇺🇿 Весь Узбекистан", callback_data="loc:uz"),
            ]
        ]
    )


def employment_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label.capitalize(), callback_data=f"emp:{code}")] for code, label in EMPLOYMENT.items()]
    )


def letter_kb(vacancy_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📄 Сгенерировать сопроводительное письмо", callback_data=f"cl:{vacancy_id}")]]
    )


def resume_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👁 Посмотреть файл", callback_data="resume:view"),
                InlineKeyboardButton(text="🗑 Удалить файл", callback_data="resume:del"),
            ]
        ]
    )


def resume_delete_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data="resume:del_yes"),
                InlineKeyboardButton(text="↩️ Отмена", callback_data="resume:del_no"),
            ]
        ]
    )


def demo_kb(vacancy_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="ℹ️ Полное описание", callback_data=f"demo:{vacancy_id}")]]
    )


def auth_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Создать аккаунт", callback_data="auth:register"),
                InlineKeyboardButton(text="🔑 Войти", callback_data="auth:login"),
            ]
        ]
    )


def skip_name_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="auth:skip_name")]]
    )
