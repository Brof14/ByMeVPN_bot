from aiogram.fsm.state import State, StatesGroup


class BuyFlow(StatesGroup):
    choosing_type = State()
    choosing_period = State()
    waiting_name = State()


class AdminFlow(StatesGroup):
    broadcast = State()
    search_user = State()
    edit_key_days = State()      # waiting for new days value
    send_personal_msg = State()  # waiting for message text to send to user
    refund_amount = State()      # waiting for refund amount
    refund_reason = State()      # waiting for refund reason
