"""My Keys: list, info, renew, delete. (Fix: guide button, photos)"""
import time
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery

from database import get_user_keys, get_key_by_id, delete_key_by_id
from xui import delete_client
from keyboards import my_keys_kb, confirm_delete_kb, payment_kb, plan_type_kb, back_to_menu
from utils import send_with_photo, send_or_edit, safe_answer, fmt_date, fmt_days_left
from states import BuyFlow

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# Show keys list (fix: uses photo)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "my_keys")
async def cb_my_keys(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    user_id = callback.from_user.id
    keys = await get_user_keys(user_id)

    if not keys:
        text = (
            "У вас пока нет ключей.\n\n"
            "Оформите подписку, чтобы получить доступ к VPN."
        )
        await send_with_photo(bot, callback, text, back_to_menu())
        return

    now = int(time.time())
    lines = ["🔑 <b>Ваши ключи:</b>\n"]
    for k in keys:
        status = "✅ активен" if k["expiry"] > now else "❌ истёк"
        lines.append(
            f"<b>{k.get('remark') or 'Ключ #' + str(k['id'])}</b>\n"
            f"  Статус: {status}\n"
            f"  До: {fmt_date(k['expiry'])} "
            f"(осталось: {fmt_days_left(k['expiry'])})"
        )

    text = "\n\n".join(lines)
    # If text too long for photo caption, send_with_photo falls back to text mode
    await send_with_photo(bot, callback, text, my_keys_kb(keys))


# ---------------------------------------------------------------------------
# Key info (tap on remark label — show the key string)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("key_info:"))
async def cb_key_info(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    key_id = int(callback.data.split(":")[1])
    k = await get_key_by_id(key_id)

    if not k or k["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return

    now = int(time.time())
    status = "✅ активен" if k["expiry"] > now else "❌ истёк"
    text = (
        f"🔑 <b>{k.get('remark') or 'Ключ #' + str(k['id'])}</b>\n\n"
        f"Статус: {status}\n"
        f"Действует до: {fmt_date(k['expiry'])}\n"
        f"Осталось: {fmt_days_left(k['expiry'])}\n\n"
        f"Ваш ключ:\n<code>{k['key']}</code>"
    )

    keys = await get_user_keys(callback.from_user.id)
    await send_or_edit(bot, callback, text, my_keys_kb(keys))


# ---------------------------------------------------------------------------
# Renew key → go to buy flow
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("key_renew:"))
async def cb_key_renew(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    key_id = int(callback.data.split(":")[1])
    k = await get_key_by_id(key_id)

    if not k or k["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return

    await state.update_data(renew_key_id=key_id)
    await state.set_state(BuyFlow.choosing_type)

    await send_with_photo(
        bot, callback,
        f"🔄 <b>Продление ключа «{k.get('remark') or k['id']}»</b>\n\n"
        "Выберите тариф:",
        plan_type_kb(),
    )


# ---------------------------------------------------------------------------
# Delete key — ask confirmation
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("key_delete:"))
async def cb_key_delete(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    key_id = int(callback.data.split(":")[1])
    k = await get_key_by_id(key_id)

    if not k or k["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return

    remark = k.get("remark") or f"Ключ #{key_id}"
    text = (
        f"🗑 <b>Удалить ключ «{remark}»?</b>\n\n"
        "Ключ будет удалён с сервера и из базы данных.\n"
        "⚠️ Это действие <b>необратимо</b>."
    )
    await send_or_edit(bot, callback, text, confirm_delete_kb(key_id))


# ---------------------------------------------------------------------------
# Delete key — confirmed
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("key_delete_confirm:"))
async def cb_key_delete_confirm(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    key_id = int(callback.data.split(":")[1])
    k = await get_key_by_id(key_id)

    if not k or k["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return

    remark = k.get("remark") or f"Ключ #{key_id}"

    # Delete from 3x-UI panel
    if k.get("uuid"):
        ok = await delete_client(k["uuid"])
        if not ok:
            logger.warning(
                "Could not delete UUID %s from panel (key_id=%d)", k["uuid"], key_id
            )

    # Delete from DB
    await delete_key_by_id(key_id)
    logger.info("Key %d deleted by user %d", key_id, callback.from_user.id)

    # Refresh list
    keys = await get_user_keys(callback.from_user.id)
    if not keys:
        await send_with_photo(
            bot, callback,
            f"✅ Ключ «{remark}» удалён.\n\nУ вас больше нет активных ключей.",
            back_to_menu(),
        )
    else:
        now = int(time.time())
        lines = [f"✅ Ключ «{remark}» удалён.\n\n🔑 <b>Оставшиеся ключи:</b>\n"]
        for k2 in keys:
            status = "✅ активен" if k2["expiry"] > now else "❌ истёк"
            lines.append(
                f"<b>{k2.get('remark') or 'Ключ #' + str(k2['id'])}</b> — "
                f"{status}, до {fmt_date(k2['expiry'])}"
            )
        await send_with_photo(bot, callback, "\n".join(lines), my_keys_kb(keys))


# ---------------------------------------------------------------------------
# Referral bonus activation
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("ref_bonus_activate:"))
async def cb_ref_bonus_activate(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    REF_BONUS_DAYS = 15  # days per paid referral
    await state.clear()
    await state.set_state(BuyFlow.waiting_name)
    await state.update_data(
        days=REF_BONUS_DAYS,
        prefix="ref_bonus",
        is_paid=False,
        amount=0,
        currency="RUB",
        method="ref_bonus",
        payload=f"ref_bonus_{callback.from_user.id}",
    )
    from keyboards import cancel_kb
    await send_with_photo(
        bot, callback,
        f"🎁 <b>Бонус +{REF_BONUS_DAYS} дней!</b>\n\n"
        "Введите название конфига для бонусного ключа:",
        cancel_kb(),
    )
