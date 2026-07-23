"""My Keys: list, info, renew, delete. (Fix: guide button, photos)"""
import time
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from database import get_user_keys, get_key_by_id, delete_key_by_id
from xui_client import delete_xui_user, get_xui_user, format_traffic
from keyboards import my_keys_kb, my_keys_list_kb, key_detail_kb, confirm_delete_kb, payment_kb, back_to_menu, connection_guide_kb, tariff_selection_kb
from utils import send_with_photo, send_or_edit, safe_answer
from constants import format_timestamp as fmt_date, format_days_left as fmt_days_left
from states import BuyFlow

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# /keys command - direct access to my keys
# ---------------------------------------------------------------------------

@router.message(F.text.startswith("/keys"))
async def cmd_keys(message: Message, bot: Bot):
    """Direct command to show user's keys."""
    user_id = message.from_user.id
    keys = await get_user_keys(user_id)

    if not keys:
        await message.answer(
            "🔑 <b>У вас пока нет ключей.</b>\n\n"
            "Оформите подписку, чтобы получить доступ к VPN.",
            parse_mode="HTML",
            reply_markup=back_to_menu()
        )
        return

    now = int(time.time())
    lines = ["🔑 <b>Ваши ключи:</b>\n"]
    for k in keys:
        status = "✅ активен" if k["expiry"] > now else "❌ истёк"
        devices = k.get("limit_ip", 1)
        device_label = {1: "1 уст.", 2: "2 уст.", 5: "5 уст."}.get(devices, f"{devices} уст.")
        lines.append(
            f"<b>{k.get('remark') or 'Ключ #' + str(k['id'])}</b>\n"
            f"  Статус: {status} · {device_label}\n"
            f"  До: {fmt_date(k['expiry'])} "
            f"(осталось: {fmt_days_left(k['expiry'])})"
        )

    text = "\n\n".join(lines)
    await message.answer(text, parse_mode="HTML", reply_markup=my_keys_list_kb(keys))


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
        devices = k.get("limit_ip", 1)
        device_label = {1: "1 уст.", 2: "2 уст.", 5: "5 уст."}.get(devices, f"{devices} уст.")
        lines.append(
            f"<b>{k.get('remark') or 'Ключ #' + str(k['id'])}</b>\n"
            f"  Статус: {status} · {device_label}\n"
            f"  До: {fmt_date(k['expiry'])} "
            f"(осталось: {fmt_days_left(k['expiry'])})"
        )

    text = "\n\n".join(lines)
    # If text too long for photo caption, send_with_photo falls back to text mode
    await send_with_photo(bot, callback, text, my_keys_list_kb(keys))


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
    devices = k.get("limit_ip", 1)
    device_label = f"{devices} устройств" if devices > 1 else f"{devices} устройство"
    
    # Получаем ссылку на подписку из БД
    subscription_url = k.get('key', '')
    
    # Получаем VLESS ключ из поля uuid
    vless_key = k.get('uuid', '')
    
    # Получаем информацию о трафике из 3x-ui
    traffic_info = ""
    try:
        user_id = callback.from_user.id
        xui_user = await get_xui_user(user_id)
        if xui_user:
            used_traffic = xui_user.get("used_traffic", 0)
            traffic_info = f"📊 Использовано трафика: {format_traffic(used_traffic)}\n"
    except Exception as e:
        logger.error("Failed to get traffic info: %s", e)
    
    text = (
        f"🔑 <b>{k.get('remark') or 'Ключ #' + str(k['id'])}</b>\n\n"
        f"Статус: {status}\n"
        f"Устройств: {device_label}\n"
        f"Действует до: {fmt_date(k['expiry'])}\n"
        f"Осталось: {fmt_days_left(k['expiry'])}\n"
        f"{traffic_info}\n"
        f"� <b>Ваша подписка:</b>\n"
        f"<code>{subscription_url}</code>"
    )

    keys = await get_user_keys(callback.from_user.id)
    await send_or_edit(bot, callback, text, key_detail_kb(key_id))


# ---------------------------------------------------------------------------
# Key instructions (tap on remark label — show the key string)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("key_instructions:"))
async def cb_key_instructions(callback: CallbackQuery, bot: Bot):
    """Show connection instructions with device selection."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    key_id = int(callback.data.split(":")[1])
    
    # Get all user keys and find the specific one
    keys = await get_user_keys(user_id)
    key = None
    for k in keys:
        if k['id'] == key_id:
            key = k
            break
    
    if not key:
        await callback.message.answer("Ключ не найден.", reply_markup=back_to_menu())
        return
    
    # Get subscription URL from existing key
    subscription_url = key.get('key', '')

    # Show device selection guide
    from keyboards import connection_guide_kb

    text = (
        f"📋 <b>Инструкция подключения</b>\n\n"
        f"🔑 <b>Ключ #{key_id}</b>\n"
        f"🔗 <code>{subscription_url}</code>\n\n"
        f"<b>Выберите ваше устройство:</b>"
    )
    
    await callback.message.edit_text(text, reply_markup=connection_guide_kb(), parse_mode="HTML")


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
        tariff_selection_kb(),
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
    user_id = callback.from_user.id

    # Delete from 3x-ui panel
    ok = await delete_xui_user(user_id)
    if not ok:
        logger.warning(
            "Could not delete user %d from 3x-ui (key_id=%d)", user_id, key_id
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
        await send_with_photo(bot, callback, "\n".join(lines), my_keys_list_kb(keys))


# ---------------------------------------------------------------------------
# Referral bonus activation
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("ref_bonus_activate:"))
async def cb_ref_bonus_activate(callback: CallbackQuery, bot: Bot, state: FSMContext):
    await safe_answer(callback)
    REF_BONUS_DAYS = 15  # days per paid referral
    referrer_id = callback.from_user.id

    # Parse the referred_id this bonus is tied to
    try:
        referred_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await safe_answer(callback, "Неверный формат бонуса.", alert=True)
        return

    # Atomic claim: only the referrer who earned this specific bonus can redeem it once
    from database import try_claim_ref_bonus
    claimed = await try_claim_ref_bonus(referrer_id, referred_id)
    if not claimed:
        await safe_answer(callback, "Этот бонус уже был активирован.", alert=True)
        return

    await state.clear()
    await state.set_state(BuyFlow.waiting_name)
    await state.update_data(
        days=REF_BONUS_DAYS,
        prefix="ref_bonus",
        is_paid=False,
        amount=0,
        currency="RUB",
        method="ref_bonus",
        payload=f"ref_bonus_{referrer_id}_{referred_id}",
        limit_ip=1,  # Referral bonus always 1 device
    )
    from keyboards import cancel_kb
    await send_with_photo(
        bot, callback,
        f"🎁 <b>Бонус +{REF_BONUS_DAYS} дней!</b>\n\n"
        "Введите название конфига для бонусного ключа:",
        cancel_kb(),
    )
