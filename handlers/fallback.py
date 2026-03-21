"""
Fallback handler: clean chat and show main menu for unknown messages/commands.
Must be the last registered router.
"""
import logging

from aiogram import Bot, Router
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router()


@router.message()
async def fallback_message(message: Message, bot: Bot):
    from database import ensure_user, has_ever_had_key, has_used_trial
    from keyboards import main_menu_new_user, main_menu_existing
    from utils import LOGO_URL
    from handlers.start import _clean_chat

    user_id = message.from_user.id
    await ensure_user(user_id)

    # Clean chat before showing menu
    await _clean_chat(bot, message.chat.id, message.message_id, count=50)

    # Decide which menu to show
    ever = await has_ever_had_key(user_id)
    trial = await has_used_trial(user_id)
    kb = main_menu_existing() if (ever or trial) else main_menu_new_user()

    name = message.from_user.first_name or "друг"
    if ever or trial:
        text = (
            "Ваша подписка закончилась\n\n"
            "Вы можете продлить ВПН и дальше пользоваться сервисом без ограничений."
        )
    else:
        text = (
            f"Привет, {name}!\n\n"
            "ByMeVPN — твой надежный VPN-сервис\n\n"
            "Выбирай действие ниже:"
        )

    await bot.send_photo(
        chat_id=user_id,
        photo=LOGO_URL,
        caption=text,
        parse_mode="HTML",
        reply_markup=kb,
    )
