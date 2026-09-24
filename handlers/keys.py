"""My Keys: list, info, renew, delete. (Fix: guide button, photos)"""
import time
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from database import get_user_keys, get_key_by_id, delete_key_by_id
from xui_client import delete_xui_user, get_xui_user, format_traffic
from keyboards import (
    my_keys_kb, my_keys_list_kb, key_detail_kb, confirm_delete_kb,
    payment_kb, back_to_menu, connection_guide_kb, tariff_selection_kb,
    autorenew_confirm_unbind_kb, manage_payment_methods_kb,
)
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
        devices = k.get("limit_ip", 2)
        device_label = {2: "2 уст.", 5: "5 уст.", 10: "10 уст."}.get(devices, f"{devices} уст.")
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
        devices = k.get("limit_ip", 2)
        device_label = {2: "2 уст.", 5: "5 уст.", 10: "10 уст."}.get(devices, f"{devices} уст.")
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
    devices = k.get("limit_ip", 2)
    device_label = f"{devices} устройств" if devices in (5, 6, 7, 8, 9, 10, 0) else (f"{devices} устройства" if devices in (2, 3, 4) else f"{devices} устройство")
    
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
    
    from database import get_auto_renew_subscription, set_auto_renew_status
    sub = await get_auto_renew_subscription(callback.from_user.id, key_id)
    has_autorenew = sub is not None
    autorenew_active = sub is not None and sub.get("status") == "active"

    if has_autorenew:
        if autorenew_active:
            pm_title = sub.get("payment_method_title") or "карта"
            ar_line = f"🔄 Автопродление: <b>✅ Включено</b> ({pm_title})\n"
        elif sub.get("status") == "failed":
            ar_line = "🔄 Автопродление: <b>⚠️ Ошибка списания</b>\n"
        else:
            ar_line = "🔄 Автопродление: <b>⏹ Отключено</b>\n"
    else:
        ar_line = ""

    text = (
        f"🔑 <b>{k.get('remark') or 'Ключ #' + str(k['id'])}</b>\n\n"
        f"Статус: {status}\n"
        f"Устройств: {device_label}\n"
        f"Действует до: {fmt_date(k['expiry'])}\n"
        f"Осталось: {fmt_days_left(k['expiry'])}\n"
        f"{ar_line}"
        f"{traffic_info}\n"
        f"🌐 <b>Ваша подписка:</b>\n"
        f"<code>{subscription_url}</code>"
    )

    await send_or_edit(bot, callback, text, key_detail_kb(key_id, has_autorenew=has_autorenew, autorenew_active=autorenew_active))


@router.callback_query(F.data.startswith("autorenew_unbind_prompt:"))
async def cb_autorenew_unbind_prompt(callback: CallbackQuery, bot: Bot):
    """Show unbind confirmation and warn that subscription will be terminated."""
    await safe_answer(callback)
    key_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    from database import get_auto_renew_subscription

    sub = await get_auto_renew_subscription(user_id, key_id if key_id > 0 else None)
    pm_title = (sub.get("payment_method_title") if sub else None) or "Банковская карта"

    text = (
        "⚠️ <b>Отмена автопродления и отвязка карты</b>\n\n"
        "Вы собираетесь отвязать банковскую карту от сервиса ByMeVPN.\n\n"
        f"💳 Способ оплаты: <b>{pm_title}</b>\n\n"
        "⚠️ <b>Внимание:</b> При отмене автопродления и отвязке карты ваша текущая подписка ByMeVPN "
        "будет <b>сразу отключена</b>, а доступ к VPN прекратится без возврата средств.\n\n"
        "Вы уверены, что хотите отвязать карту и прекратить действие подписки?"
    )
    await send_or_edit(bot, callback, text, autorenew_confirm_unbind_kb(key_id))


@router.callback_query(F.data.startswith("autorenew_unbind_confirm:"))
async def cb_autorenew_unbind_confirm(callback: CallbackQuery, bot: Bot):
    """Confirm card unbinding: cancel recurrent billing and immediately terminate VPN access."""
    await safe_answer(callback)
    key_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    from database import cancel_autorenew_and_terminate_subscription
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    await cancel_autorenew_and_terminate_subscription(user_id, key_id if key_id > 0 else None)

    text = (
        "✅ <b>Банковская карта успешно отвязана</b>\n\n"
        "Автоматические списания отменены, данные карты удалены из сервиса.\n"
        "Ваша подписка на ByMeVPN отключена.\n\n"
        "Если вы захотите вернуться, вы всегда можете оформить новую подписку в меню бота: /start"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")]
    ])
    await send_or_edit(bot, callback, text, kb)


@router.callback_query(F.data.startswith("autorenew_toggle:"))
async def cb_autorenew_toggle(callback: CallbackQuery, bot: Bot):
    """Backward compatibility for toggle callback."""
    await cb_autorenew_unbind_prompt(callback, bot)


@router.callback_query(F.data == "manage_payment_methods")
async def cb_manage_payment_methods(callback: CallbackQuery, bot: Bot):
    """Manage saved payment methods (YooKassa requirement)."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    from database import get_auto_renew_subscription

    sub = await get_auto_renew_subscription(user_id)
    if sub and sub.get("status") == "active":
        pm_title = sub.get("payment_method_title") or "Банковская карта"
        amount = sub.get("amount_rub", 89)
        devices = sub.get("devices", 2)
        key_id = sub.get("key_id") or 0
        text = (
            "💳 <b>Управление автоплатежами</b>\n\n"
            f"Привязанный способ оплаты: <b>{pm_title}</b>\n"
            f"Тариф: <b>{devices} устр. ({amount} ₽/мес)</b>\n"
            "Статус: <b>✅ Автопродление активно</b>\n\n"
            "Здесь вы можете самостоятельно отвязать карту от сервиса ByMeVPN без обращения в поддержку."
        )
        await send_or_edit(bot, callback, text, manage_payment_methods_kb(has_card=True, key_id=key_id))
    else:
        text = (
            "💳 <b>Управление автоплатежами</b>\n\n"
            "У вас нет привязанных банковских карт для автосписания.\n"
            "Автоматические списания не производятся."
        )
        await send_or_edit(bot, callback, text, manage_payment_methods_kb(has_card=False))


@router.message(F.text.in_({"/unsubscribe", "/cancel_subscription"}))
async def cmd_unsubscribe(message: Message, bot: Bot):
    """Direct command to unbind card / cancel recurrent subscription."""
    user_id = message.from_user.id
    from database import get_auto_renew_subscription

    sub = await get_auto_renew_subscription(user_id)
    if sub and sub.get("status") == "active":
        pm_title = sub.get("payment_method_title") or "Банковская карта"
        amount = sub.get("amount_rub", 89)
        devices = sub.get("devices", 2)
        key_id = sub.get("key_id") or 0
        text = (
            "💳 <b>Управление автоплатежами</b>\n\n"
            f"Привязанный способ оплаты: <b>{pm_title}</b>\n"
            f"Тариф: <b>{devices} устр. ({amount} ₽/мес)</b>\n"
            "Статус: <b>✅ Автопродление активно</b>\n\n"
            "Здесь вы можете самостоятельно отвязать карту от сервиса ByMeVPN без обращения в поддержку."
        )
        await message.answer(text, parse_mode="HTML", reply_markup=manage_payment_methods_kb(has_card=True, key_id=key_id))
    else:
        text = (
            "💳 <b>Управление автоплатежами</b>\n\n"
            "У вас нет привязанных банковских карт для автосписания.\n"
            "Автоматические списания не производятся."
        )
        await message.answer(text, parse_mode="HTML", reply_markup=manage_payment_methods_kb(has_card=False))


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

    key_devices = k.get("limit_ip", 2)
    await state.update_data(renew_key_id=key_id, devices=key_devices)
    await state.set_state(BuyFlow.choosing_type)

    await send_with_photo(
        bot, callback,
        f"🔄 <b>Продление ключа «{k.get('remark') or k['id']}»</b>\n\n"
        "Выберите тариф:",
        tariff_selection_kb(devices=key_devices),
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
# RF apps bypass guide
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "rf_apps_guide")
async def cb_rf_apps_guide(callback: CallbackQuery, bot: Bot):
    """Show instruction for using VPN with RF apps simultaneously."""
    await safe_answer(callback)
    
    text = (
        "🌐 <b>Как пользоваться VPN и РФ-приложениями одновременно</b>\n\n"
        "Если при включённом VPN вам нужно пользоваться российскими приложениями "
        "(например, <b>MAX</b>), а остальные приложения должны продолжать работать через VPN, "
        "настройте обход для нужных приложений.\n\n"
        "<b>Инструкция для Happ:</b>\n\n"
        "1. Откройте приложение <b>Happ</b> на устройстве.\n\n"
        "2. Нажмите <b>⚙️ Настройки</b> в правом верхнем углу главного экрана.\n\n"
        "3. Откройте раздел <b>«Настройки туннеля»</b>.\n\n"
        "4. Выберите <b>«Прокси для выбранных приложений»</b>.\n\n"
        "5. Установите режим <b>«Обход»</b>.\n\n"
        "6. В списке приложений отметьте те, которые должны работать <b>без VPN</b> "
        "— например, <b>MAX</b> и другие российские приложения.\n\n"
        "7. Вернитесь назад — настройки сохранятся автоматически.\n\n"
        "<b>Готово.</b> Теперь выбранные приложения будут работать напрямую, без VPN, "
        "а остальные приложения продолжат использовать VPN.\n\n"
        "Таким образом, <b>VPN можно не отключать полностью</b>: российские приложения "
        "работают напрямую, а иностранные — через VPN."
    )
    
    from keyboards import back_to_menu
    await send_or_edit(bot, callback, text, back_to_menu())


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
