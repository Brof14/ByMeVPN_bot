from aiogram.fsm.state import State, StatesGroup


class UserStates(StatesGroup):
    choosing_type = State()
    choosing_period = State()
    choosing_auto_renewal = State()


class AdminStates(StatesGroup):
    broadcast = State()
    search_user = State()