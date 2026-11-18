from aiogram.fsm.state import State, StatesGroup


class ResumeStates(StatesGroup):
    waiting = State()


class FilterStates(StatesGroup):
    keywords = State()
    location = State()
    employment = State()


class AuthStates(StatesGroup):
    reg_login = State()
    reg_password = State()
    reg_email = State()
    reg_name = State()
    login_ident = State()
    login_email = State()
    login_password = State()


class ChatStates(StatesGroup):
    active = State()
