"""Fallback for unknown commands and text; never erase customer chat history.

Keep this router last and restrict it to idle FSM state. Do not log raw
customer text (it can contain login URLs, subscription links, or credentials).
"""
from aiogram import Bot, Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, InlineKeyboardMarkup

from async_utils import monitor_performance
from bot_style import themed_button as InlineKeyboardButton

router = Router()


async def _show_menu_hint(message: Message, *, unknown_command: bool = False) -> None:
    caption = "Команда не найдена. Откройте меню." if unknown_command else (
        "Не удалось распознать сообщение. Откройте меню."
    )
    await message.answer(
        caption,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть меню", callback_data="back_to_menu")
        ]]),
    )


@router.message(F.command, StateFilter(None))
@monitor_performance("fallback_command")
async def fallback_command(message: Message, bot: Bot, state: FSMContext) -> None:
    await _show_menu_hint(message, unknown_command=True)


@router.message(F.text & ~F.command, StateFilter(None))
@monitor_performance("fallback_text")
async def fallback_text(message: Message, bot: Bot, state: FSMContext) -> None:
    await _show_menu_hint(message)
