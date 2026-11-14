"""/legal command — юридические документы."""
import logging

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from keyboards import legal_kb
from utils import send_with_photo, safe_answer
from bot_assets import LEGAL

logger = logging.getLogger(__name__)
router = Router()

_LEGAL_TEXT = (
    "<b>Документы ByMeVPN</b>\n\n"
    "Выберите документ, чтобы ознакомиться с условиями сервиса."
)

@router.message(F.text.startswith("/legal"))
async def cmd_legal(message: Message, bot: Bot):
    logger.info(f"Legal command received from user {message.from_user.id}")
    try:
        await message.delete()
    except Exception:
        pass
    await send_with_photo(bot, message, _LEGAL_TEXT, legal_kb(), photo=LEGAL)


@router.callback_query(F.data == "legal")
async def cb_legal(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    await send_with_photo(bot, callback, _LEGAL_TEXT, legal_kb(), photo=LEGAL)
