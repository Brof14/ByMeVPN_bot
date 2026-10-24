"""My Keys: list, info, renew, delete. (Fix: guide button, photos)"""
import time
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from database import get_user_keys, get_key_by_id, delete_key_by_id
from xui_client import delete_xui_user, get_xui_user, format_traffic
from keyboards import (
    my_keys_kb, my_keys_list_kb, key_detail_kb, confirm_delete_kb,
    payment_kb, back_to_menu, connection_guide_kb, tariff_selection_kb,
    autorenew_cancel_confirm_kb, manage_payment_methods_kb, key_settings_kb,
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
            "<b>У вас пока нет VPN-ключей</b>\n\n"
            "Выберите тариф или получите бесплатные дни за приглашения друзей.",
            parse_mode="HTML",
            reply_markup=back_to_menu()
        )
        return

    text = (
        "<b>Мои ключи</b>\n\n"
        "Активные и завершённые подписки отображаются ниже.\n"
        "Выберите ключ для подключения или управления подпиской."
    )
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
            "Выберите тариф или получите бесплатные дни за приглашения друзей."
        )
        await send_with_photo(bot, callback, text, back_to_menu())
        return

    text = (
        "<b>Мои ключи</b>\n\n"
        "Активные и завершённые подписки отображаются ниже.\n"
        "Выберите ключ для подключения или управления подпиской."
    )
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
    status = "✅ Активен" if k["expiry"] > now else "Срок завершён"
    devices = k.get("limit_ip", 2)
    device_label = f"{devices} устройств" if devices in (5, 6, 7, 8, 9, 10, 0) else (f"{devices} устройства" if devices in (2, 3, 4) else f"{devices} устройство")
    
    # Получаем ссылку на подписку из БД
    subscription_url = k.get('key', '')
    
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
    
    from database import get_auto_renew_subscription
    sub = await get_auto_renew_subscription(callback.from_user.id, key_id)
    has_autorenew = sub is not None
    autorenew_active = sub is not None and sub.get("status") == "active"

    if has_autorenew:
        if autorenew_active:
            pm_title = sub.get("payment_method_title") or _payment_method_label(sub.get("payment_method_type"))
            ar_line = f"🔄 Автопродление: <b>✅ Включено</b> ({pm_title})\n"
        elif sub.get("status") == "failed":
            ar_line = "🔄 Автопродление: <b>⚠️ Ошибка списания</b>\n"
        else:
            ar_line = "🔄 Автопродление: <b>⏹ Отключено</b>\n"
    else:
        ar_line = ""

    text = (
        f"<b>{escape(k.get('remark') or 'Мой VPN')}</b>\n"
        f"{status} · {device_label}\n"
        f"📅 До {fmt_date(k['expiry'])} · {fmt_days_left(k['expiry'])}\n"
        f"{ar_line}"
        f"{traffic_info}"
        f"\n<b>Ссылка для подключения</b>\n"
        f"<code>{escape(subscription_url)}</code>"
    )

    await send_or_edit(bot, callback, text, key_detail_kb(key_id, has_autorenew=has_autorenew, autorenew_active=autorenew_active))


@router.callback_query(F.data.startswith("key_settings:"))
async def cb_key_settings(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await safe_answer(callback, "Не удалось открыть настройки ключа.", alert=True)
        return

    key = await get_key_by_id(key_id)
    if not key or key["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return

    text = (
        f"<b>Управление подпиской «{escape(key.get('remark') or f'Ключ #{key_id}')}»</b>\n\n"
        "Здесь можно управлять автопродлением и ключом."
    )
    await send_or_edit(bot, callback, text, key_settings_kb(key_id))


def _pm_tariff_line(sub: dict | None) -> str:
    """Real tariff line from the auto-renew subscription row (no hardcoded data)."""
    if not sub:
        return ""
    months = sub.get("months") or 1
    devices = sub.get("devices") or 2
    amount = sub.get("amount_rub") or 0
    return f"└ Тариф: {devices} устр., {months} мес. ({amount} ₽ за период)\n"


def _payment_method_label(method_type: str | None) -> str:
    """Human-readable label for a saved payment method, even without a title."""
    return {
        "bank_card": "Банковская карта",
        "sbp": "Счёт СБП",
        "yoo_money": "ЮMoney",
    }.get((method_type or "").lower(), "Сохранённый способ оплаты")


def _get_payment_methods_text(
    card_title: str = "",
    is_checked: bool = True,
    sub: dict | None = None,
    expiry_str: str = "",
) -> str:
    """Short, calm auto-renew summary based on the saved payment record."""
    if not card_title:
        return (
            "<b>Автопродление</b>\n\n"
            "Автопродление пока не подключено. Оплатите продление через ЮKassa, чтобы включить его.\n\n"
            "Telegram Stars и CryptoBot продлевают подписку вручную."
        )

    active = bool(sub and sub.get("status") == "active")
    amount = sub.get("amount_rub", 0) if sub else 0
    days = sub.get("days", 30) if sub else 30
    if active:
        next_charge = f"Следующее продление: <b>{amount} ₽</b> за {days} дней."
        if expiry_str:
            next_charge += f"\nДата: <b>{expiry_str}</b>."
        status = "✅ Автопродление включено"
    else:
        next_charge = "Новых списаний не будет."
        status = "Автопродление выключено"
        if expiry_str:
            next_charge += f" Доступ действует до <b>{expiry_str}</b>."

    return (
        "<b>Автопродление</b>\n\n"
        f"Способ оплаты: <b>{escape(card_title)}</b>\n"
        f"{status}\n"
        f"{next_charge}\n\n"
        "Отключить автопродление можно в любой момент. Оплаченный срок сохранится."
    )


async def _load_autorenew_context(user_id: int, key_id: int) -> tuple[dict | None, str, str]:
    """Load (sub, card_title, expiry_str) for payment-method screens."""
    from database import get_auto_renew_subscription, get_user_keys

    sub = await get_auto_renew_subscription(user_id, key_id if key_id > 0 else None)
    card_title = ""
    if sub and sub.get("payment_method_id"):
        card_title = (sub.get("payment_method_title") or "").strip()
        if card_title.lower() == "отвязана":
            card_title = ""
        if not card_title:
            card_title = _payment_method_label(sub.get("payment_method_type"))
    expiry_str = ""
    if key_id > 0:
        key = await get_key_by_id(key_id)
        expiry_ts = key.get("expiry", 0) if key and key.get("user_id") == user_id else 0
    else:
        keys = await get_user_keys(user_id)
        expiry_ts = max((key.get("expiry", 0) for key in keys), default=0)
    if expiry_ts:
        expiry_str = fmt_date(expiry_ts)
    return sub, card_title, expiry_str


@router.callback_query(F.data.startswith("card_toggle_check:"))
async def cb_card_toggle_check(callback: CallbackQuery, bot: Bot):
    """Toggle checkbox selection for saved card."""
    await safe_answer(callback)
    parts = callback.data.split(":")
    key_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    was_checked = int(parts[2]) == 1 if len(parts) > 2 and parts[2].isdigit() else True
    new_checked = not was_checked

    user_id = callback.from_user.id
    sub, card_title, expiry_str = await _load_autorenew_context(user_id, key_id)

    text = _get_payment_methods_text(card_title=card_title, is_checked=new_checked, sub=sub, expiry_str=expiry_str)
    await send_or_edit(
        bot, callback, text,
        manage_payment_methods_kb(
            card_title=card_title, is_checked=new_checked, key_id=key_id,
            has_card=bool(sub and sub.get("payment_method_id")), autorenew_active=bool(sub and sub.get("status") == "active"),
        ),
    )


@router.callback_query(F.data.startswith("autorenew_unbind_prompt:"))
async def cb_autorenew_unbind_prompt(callback: CallbackQuery, bot: Bot):
    """Show confirmation for disabling auto-renew (access is NOT revoked)."""
    await safe_answer(callback)
    parts = callback.data.split(":")
    key_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    user_id = callback.from_user.id

    sub, card_title, expiry_str = await _load_autorenew_context(user_id, key_id)
    if not card_title:
        text = _get_payment_methods_text(sub=sub, expiry_str=expiry_str)
        kb = manage_payment_methods_kb(key_id=key_id, has_card=False)
        await send_or_edit(bot, callback, text, kb)
        return

    warning = "Будущие списания прекратятся."
    if expiry_str:
        warning += f" Доступ сохранится до <b>{expiry_str}</b>."

    text = (
        "<b>Отключить автопродление?</b>\n\n"
        f"💳 Способ оплаты: <b>{escape(card_title)}</b>\n\n"
        f"{warning}\n\n"
        "Доступ сохранится до конца оплаченного срока. Автопродление можно подключить снова при следующей оплате через ЮKassa."
    )
    await send_or_edit(bot, callback, text, autorenew_cancel_confirm_kb(key_id))


@router.callback_query(F.data.startswith("autorenew_unbind_confirm:"))
async def cb_autorenew_unbind_confirm(callback: CallbackQuery, bot: Bot):
    """Disable auto-renew. Paid access is preserved until the period ends."""
    await safe_answer(callback)
    parts = callback.data.split(":")
    key_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    user_id = callback.from_user.id

    from database import unlink_auto_renew_payment_method
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    res = await unlink_auto_renew_payment_method(user_id, key_id if key_id > 0 else None)

    expiry_str = ""
    if res.get("expires_at"):
        expiry_str = fmt_date(res["expires_at"])

    keep_line = (
        f"Доступ сохранится до <b>{expiry_str}</b> — всё оплаченное работает."
        if expiry_str
        else "Активных оплаченных периодов не осталось."
    )

    if not res.get("unlinked"):
        text = "Сохранённый способ оплаты не найден. Новых списаний не будет."
    else:
        text = (
            "<b>Автопродление отключено</b>\n\n"
            "Новых списаний не будет.\n"
            f"{keep_line}\n\n"
            "Автопродление можно подключить снова при следующей оплате через ЮKassa."
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К ключу", callback_data=f"key_info:{key_id}")],
    ])
    await send_or_edit(bot, callback, text, kb)


@router.callback_query(F.data.startswith("autorenew_enable:"))
async def cb_autorenew_enable(callback: CallbackQuery, bot: Bot):
    """Re-enable auto-renew using the previously saved payment method."""
    await safe_answer(callback)
    parts = callback.data.split(":")
    key_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    user_id = callback.from_user.id

    from database import resume_auto_renew
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    reenabled = await resume_auto_renew(user_id, key_id if key_id > 0 else None)
    if reenabled:
        text = (
            "🔔 <b>Автопродление включено</b>\n\n"
            "Подписка продлится автоматически в конце оплаченного срока.\n"
            "Отключить автопродление можно в любой момент."
        )
    else:
        text = (
            "⚠️ <b>Не удалось включить автопродление</b>\n\n"
            "Способ оплаты недоступен. Выберите тариф: автопродление включится, если ЮKassa сохранит способ оплаты."
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К ключу", callback_data=f"key_info:{key_id}")],
    ])
    await send_or_edit(bot, callback, text, kb)


@router.callback_query(F.data.startswith("autorenew_toggle:"))
async def cb_autorenew_toggle(callback: CallbackQuery, bot: Bot):
    """Backward compatibility for toggle callback."""
    await cb_autorenew_unbind_prompt(callback, bot)


@router.callback_query(F.data.startswith("manage_payment_methods"))
async def cb_manage_payment_methods(callback: CallbackQuery, bot: Bot):
    """Manage saved payment methods (only real data, no fake cards)."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    key_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    sub, card_title, expiry_str = await _load_autorenew_context(user_id, key_id)

    text = _get_payment_methods_text(card_title=card_title, is_checked=True, sub=sub, expiry_str=expiry_str)
    await send_or_edit(
        bot, callback, text,
        manage_payment_methods_kb(
            card_title=card_title, is_checked=True, key_id=key_id,
            has_card=bool(sub and sub.get("payment_method_id")), autorenew_active=bool(sub and sub.get("status") == "active"),
        ),
    )


@router.message(F.text.in_({"/cards", "/unsubscribe", "/cancel_subscription", "/payment_methods"}))
async def cmd_unsubscribe(message: Message, bot: Bot):
    """Direct command to manage auto-renew / saved card."""
    user_id = message.from_user.id

    sub, card_title, expiry_str = await _load_autorenew_context(user_id, 0)
    key_id = (sub.get("key_id") if sub else 0) or 0

    text = _get_payment_methods_text(card_title=card_title, is_checked=True, sub=sub, expiry_str=expiry_str)
    await message.answer(
        text, parse_mode="HTML",
        reply_markup=manage_payment_methods_kb(
            card_title=card_title, is_checked=True, key_id=key_id,
            has_card=bool(card_title), autorenew_active=bool(sub and sub.get("status") == "active"),
        ),
    )


# ---------------------------------------------------------------------------
# Key instructions (tap on remark label — show the key string)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("key_instructions:"))
async def cb_key_instructions(callback: CallbackQuery, bot: Bot):
    """Show connection instructions with device selection."""
    await safe_answer(callback)
    user_id = callback.from_user.id
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return

    key = await get_key_by_id(key_id)
    if not key or key["user_id"] != user_id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return

    subscription_url = key.get("key", "")

    # Show device selection guide
    from keyboards import connection_guide_kb

    text = (
        f"<b>Подключение VPN</b> · {escape(key.get('remark') or f'Ключ #{key_id}')}\n\n"
        f"Скопируйте ссылку и выберите устройство:\n<code>{escape(subscription_url)}</code>"
    )
    await send_or_edit(bot, callback, text, connection_guide_kb(key_id))


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
        f"<b>Продление подписки «{escape(k.get('remark') or str(k['id']))}»</b>\n\n"
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

    remark = escape(k.get("remark") or f"Ключ #{key_id}")
    text = (
        f"Удалить ключ <b>«{remark}»</b>?\n\n"
        "После удаления подключение по этой ссылке перестанет работать."
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

    remark = escape(k.get("remark") or f"Ключ #{key_id}")
    user_id = callback.from_user.id

    # The bot currently uses one 3x-ui client per Telegram user. Removing
    # one saved key must not delete that shared server client while other key
    # records remain.
    existing_keys = await get_user_keys(user_id)
    other_keys = [item for item in existing_keys if item.get("id") != key_id]

    # Stop billing before removing its key. If remote deletion fails, restore
    # the previous auto-renew state so the user's subscription is unchanged.
    from database import get_auto_renew_subscription, cancel_auto_renew, resume_auto_renew
    auto_renew = await get_auto_renew_subscription(user_id, key_id)
    renew_was_active = bool(auto_renew and auto_renew.get("status") == "active")
    if renew_was_active:
        cancelled = await cancel_auto_renew(user_id, key_id)
        if not cancelled.get("cancelled"):
            await callback.message.answer(
                "⚠️ Не удалось отключить автопродление. Ключ не удалён; попробуйте ещё раз или напишите в поддержку."
            )
            return

    if not other_keys:
        ok = await delete_xui_user(user_id)
        if not ok:
            if renew_was_active:
                await resume_auto_renew(user_id, key_id)
            logger.error("Could not delete user %d from 3x-ui (key_id=%d); DB row retained", user_id, key_id)
            await callback.message.answer(
                "⚠️ Не удалось подтвердить удаление на VPN-сервере. Запись сохранена; "
                "попробуйте позже или обратитесь в поддержку."
            )
            return

    deleted = await delete_key_by_id(key_id)
    if not deleted:
        if renew_was_active:
            await resume_auto_renew(user_id, key_id)
        logger.error("Could not delete key row %d for user %d", key_id, user_id)
        await callback.message.answer("⚠️ Не удалось удалить запись. Попробуйте позже.")
        return

    logger.info("Key %d deleted by user %d (other_keys=%d)", key_id, user_id, len(other_keys))

    keys = await get_user_keys(user_id)
    if not keys:
        await send_with_photo(
            bot, callback,
            f"Ключ «{remark}» удалён. Подключение по нему отключено.",
            back_to_menu(),
        )
    else:
        text = (
            f"Ключ «{remark}» удалён.\n\n"
            "Остальные подписки и подключение по ним работают как прежде.\n"
            "Выберите ключ, чтобы открыть его настройки."
        )
        await send_with_photo(bot, callback, text, my_keys_list_kb(keys))

# ---------------------------------------------------------------------------
# Referral bonus activation
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("ref_bonus_activate:"))
async def cb_ref_bonus_activate(callback: CallbackQuery, bot: Bot, state: FSMContext):
    referrer_id = callback.from_user.id
    from constants import REFERRAL_FIRST_PAYMENT_BONUS_DAYS

    # Parse the referred_id this bonus is tied to
    try:
        referred_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await safe_answer(callback, "Неверный формат бонуса.", alert=True)
        return

    from database import (
        get_referrer, has_referral_payment_reward, try_claim_ref_bonus,
        release_ref_bonus_claim,
    )
    if await get_referrer(referred_id) != referrer_id or not await has_referral_payment_reward(referrer_id, referred_id):
        await safe_answer(callback, "Бонус не найден.", alert=True)
        return

    # Reserve the claim so a duplicate tap cannot extend twice. If delivery
    # fails, release it so the customer can retry after the issue is fixed.
    claimed = await try_claim_ref_bonus(referrer_id, referred_id)
    if not claimed:
        await safe_answer(callback, "Этот бонус уже был активирован.", alert=True)
        return
    await state.clear()
    try:
        from subscription import deliver_key
        from constants import DEFAULT_DEVICE_LIMIT
        user_keys = await get_user_keys(referrer_id)
        active_key = max(user_keys, key=lambda item: item.get("expiry", 0)) if user_keys else None
        devices = active_key.get("limit_ip", DEFAULT_DEVICE_LIMIT) if active_key else DEFAULT_DEVICE_LIMIT

        success = await deliver_key(
            bot=bot,
            user_id=referrer_id,
            chat_id=referrer_id,
            config_name=f"ByMeVPN_{referrer_id}",
            days=REFERRAL_FIRST_PAYMENT_BONUS_DAYS,
            limit_ip=devices,
            is_paid=False,
            amount=0,
            method="ref_bonus",
            payload=f"ref_bonus_{referrer_id}_{referred_id}",
            extend_existing=True,
        )
    except Exception:
        logger.exception("Failed to deliver referral bonus for referrer %d", referrer_id)
        success = False

    if not success:
        await release_ref_bonus_claim(referrer_id, referred_id)
        await safe_answer(callback, "Не получилось начислить бонус. Попробуйте позже или напишите в поддержку.", alert=True)
        return
    await safe_answer(callback, f"Готово: начислено +{REFERRAL_FIRST_PAYMENT_BONUS_DAYS} дней.", alert=True)
