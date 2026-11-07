"""Controls for optional promotional bot messages."""

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
)

from database import set_promotional_opt_out
from utils import safe_answer
from bot_style import themed_button as InlineKeyboardButton

router = Router()


@router.callback_query(F.data == "marketing_optout")
async def cb_marketing_optout(callback: CallbackQuery):
    await safe_answer(callback)
    await set_promotional_opt_out(callback.from_user.id, True)
    await callback.message.edit_text(
        "Рекламные уведомления отключены. Сообщения о подписке, оплате и безопасности останутся включены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Включить промоуведомления", callback_data="marketing_optin")],
            [InlineKeyboardButton(text="← В меню", callback_data="back_to_menu")],
        ]),
    )


@router.callback_query(F.data == "marketing_optin")
async def cb_marketing_optin(callback: CallbackQuery):
    await safe_answer(callback)
    await set_promotional_opt_out(callback.from_user.id, False)
    await callback.message.edit_text(
        "Рекламные уведомления включены. Служебные уведомления остаются активны.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отключить промоуведомления", callback_data="marketing_optout")],
            [InlineKeyboardButton(text="← В меню", callback_data="back_to_menu")],
        ]),
    )
