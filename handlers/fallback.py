"""
Fallback handler: handle unknown commands only.
Must be last registered router.
"""
import logging

from aiogram import Bot, Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from states import BuyFlow
from async_utils import monitor_performance

logger = logging.getLogger(__name__)
router = Router()


@router.message(F.command, ~StateFilter(BuyFlow.waiting_for_config_name, BuyFlow.waiting_name))
@monitor_performance("fallback_command")
async def fallback_command(message: Message, bot: Bot, state: FSMContext):
    """Handle unknown commands only."""
    logger.info(f"Fallback handling unknown command: {message.text}")
    
    from database import ensure_user, has_ever_had_key, has_trial_used
    from keyboards import main_menu_new_user, main_menu_existing
    from utils import LOGO_URL
    from handlers.start import _clean_chat, _send_main_menu

    user_id = message.from_user.id
    await ensure_user(user_id)

    # Clean previous messages but keep user's message (anchor message_id + 1) - reduced from 50 to 5
    await _clean_chat(bot, message.chat.id, message.message_id + 1, count=5)

    # Use optimized main menu sender
    name = message.from_user.first_name or "друг"
    await _send_main_menu(bot, message, user_id, name)


@router.message(F.text & ~F.command, ~StateFilter(BuyFlow.waiting_for_config_name, BuyFlow.waiting_name))
@monitor_performance("fallback_text")
async def fallback_text(message: Message, bot: Bot, state: FSMContext):
    """Handle unknown text messages and show main menu."""
    logger.info(f"Fallback handling unknown text: {message.text}")
    
    from database import ensure_user, has_ever_had_key, has_trial_used
    from keyboards import main_menu_new_user, main_menu_existing
    from utils import LOGO_URL
    from handlers.start import _clean_chat, _send_main_menu

    user_id = message.from_user.id
    await ensure_user(user_id)

    # Clean previous messages but keep user's message (anchor message_id + 1) - reduced from 50 to 5
    await _clean_chat(bot, message.chat.id, message.message_id + 1, count=5)

    # Use optimized main menu sender
    name = message.from_user.first_name or "друг"
    await _send_main_menu(bot, message, user_id, name)
