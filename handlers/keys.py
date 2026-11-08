"""My VPN: subscription, device, renewal, and key management screens."""
import time
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from database import (
    get_user_keys, get_key_by_id, delete_key_by_id,
)
from xui_client import (
    delete_xui_user, get_xui_devices, delete_xui_hwid_device,
)
from keyboards import (
    my_keys_kb, my_keys_list_kb, key_detail_kb, confirm_delete_kb,
    payment_kb, back_to_menu, connection_guide_kb, tariff_selection_kb,
    autorenew_cancel_confirm_kb, manage_payment_methods_kb, key_settings_kb,
    key_devices_kb, key_device_detail_kb, confirm_delete_device_kb, key_history_kb,
)
from utils import send_with_photo, send_or_edit, safe_answer
from constants import format_timestamp as fmt_date, format_days_left as fmt_days_left
from states import BuyFlow
from bot_assets import KEY, DEVICES, RENEWAL, CONNECTION, AUTO_RENEWAL
from bot_style import themed_button as InlineKeyboardButton

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
        await send_with_photo(bot, message,
            "<b>У вас пока нет VPN-ключей</b>\n\n"
            "Оформите подписку или получите дополнительные дни за приглашение друга и публикацию в канале.",
            back_to_menu(),
            photo=KEY,
        )
        return

    now = int(time.time())
    active_keys = sorted((k for k in keys if int(k.get("expiry") or 0) > now),
                         key=lambda k: (k.get("expiry", 0), k.get("id", 0)), reverse=True)
    if len(active_keys) == 1:
        text, keyboard = await _key_info_content(user_id, active_keys[0])
        await send_with_photo(bot, message, text, keyboard, photo=KEY)
        return
    if not active_keys and len(keys) == 1:
        text, keyboard = await _key_info_content(user_id, keys[0])
        await send_with_photo(bot, message, text, keyboard, photo=KEY)
        return
    if not active_keys:
        await send_with_photo(bot, message,
            "<b>История подписок</b>\n\nАктивной подписки сейчас нет. Выберите завершённую, чтобы открыть её данные.",
            key_history_kb(keys),
            photo=KEY,
        )
        return
    await send_with_photo(bot, message,
        "<b>Подписки</b>\n\nВыберите подписку.",
        my_keys_list_kb(active_keys, has_history=len(active_keys) < len(keys)),
        photo=KEY,
    )


# ---------------------------------------------------------------------------
# Show the current subscription and key list.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "my_keys")
async def cb_my_keys(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    user_id = callback.from_user.id
    keys = await get_user_keys(user_id)

    if not keys:
        text = (
            "Пока нет активной подписки.\n\n"
            "Выберите тариф или получите дополнительные дни в разделе «Бесплатные дни»."
        )
        await send_with_photo(bot, callback, text, back_to_menu(), photo=KEY)
        return

    now = int(time.time())
    active_keys = sorted((k for k in keys if int(k.get("expiry") or 0) > now),
                         key=lambda k: (k.get("expiry", 0), k.get("id", 0)), reverse=True)
    if len(active_keys) == 1:
        text, keyboard = await _key_info_content(user_id, active_keys[0])
        await send_or_edit(bot, callback, text, keyboard, photo=KEY)
        return
    if not active_keys and len(keys) == 1:
        text, keyboard = await _key_info_content(user_id, keys[0])
        await send_or_edit(bot, callback, text, keyboard, photo=KEY)
        return
    if not active_keys:
        text = "<b>История подписок</b>\n\nАктивной подписки сейчас нет. Выберите завершённую, чтобы открыть её данные."
        await send_with_photo(bot, callback, text, key_history_kb(keys), photo=KEY)
        return
    text = "<b>Ваш активный VPN</b>\n\nВыберите подписку для управления. Завершённые подписки находятся в истории."
    await send_with_photo(bot, callback, text, my_keys_list_kb(active_keys, has_history=len(active_keys) < len(keys)), photo=KEY)


@router.callback_query(F.data == "key_history")
async def cb_key_history(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    keys = await get_user_keys(callback.from_user.id)
    expired = [key for key in keys if int(key.get("expiry") or 0) <= int(time.time())]
    text = (
        "<b>История подписок</b>\n\nВыберите завершённую подписку, чтобы посмотреть её данные."
        if expired else "<b>История подписок</b>\n\nЗавершённых подписок пока нет."
    )
    await send_with_photo(bot, callback, text, key_history_kb(expired), photo=KEY)


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

    text, keyboard = await _key_info_content(callback.from_user.id, k)
    await send_or_edit(bot, callback, text, keyboard, photo=KEY)


async def _key_info_content(user_id: int, k: dict) -> tuple[str, object]:
    key_id = int(k["id"])

    now = int(time.time())
    status = "Доступ активен" if k["expiry"] > now else "Доступ завершён"
    subscription_url = k.get("key", "")

    devices_info = ""
    try:
        device_state = await get_xui_devices(user_id)
    except Exception:
        device_state = None
        logger.warning("Failed to get device count for user %s", user_id, exc_info=True)
    if isinstance(device_state, dict):
        registered = len(device_state.get("devices") or [])
        device_limit = int(device_state.get("limit_hwid") or 0)
        devices_info = (
            f"Устройства: <b>{registered} из {device_limit}</b>\n"
            if device_limit else f"Устройств: <b>{registered}</b>\n"
        )

    all_keys = await get_user_keys(user_id)
    has_history = any(int(item.get("expiry") or 0) <= now and item.get("id") != k.get("id") for item in all_keys)
    text = (
        f"<b>Подписка</b>\n{status}\n\n"
        f"Действует до: <b>{fmt_date(k['expiry'])}</b>\n"
        f"{devices_info}"
    )

    return text, key_detail_kb(
        key_id, subscription_url=subscription_url, has_history=has_history,
    )


def _time_ago(timestamp: int) -> str:
    seconds = max(0, int(time.time()) - int(timestamp))
    if seconds < 60:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    days = seconds // 86400
    last_two = days % 100
    last = days % 10
    word = "день" if last == 1 and last_two != 11 else (
        "дня" if last in (2, 3, 4) and last_two not in (12, 13, 14) else "дней"
    )
    return f"{days} {word} назад"


async def _key_devices_view(user_id: int, key: dict, page: int = 0) -> tuple[str, object]:
    info = await get_xui_devices(user_id)
    devices = info["devices"]
    for device in devices:
        device["last_seen_label"] = _time_ago(device["last_seen"]) if device["last_seen"] else "давно"
    if info["limit_hwid"]:
        usage_line = f"Занято мест: <b>{len(devices)} из {info['limit_hwid']}</b>"
    else:
        usage_line = f"Сохранено устройств: <b>{len(devices)}</b>"
    lines = [
        "<b>Устройства</b>",
        usage_line,
        "Лимит общий для всех устройств.",
    ]
    if devices:
        lines.extend(["", "Выберите устройство, чтобы посмотреть детали или удалить его."])
        pages = max(1, (len(devices) + 9) // 10)
        if pages > 1:
            lines.append(f"Страница {min(page + 1, pages)} из {pages}")
        lines.append("")
    else:
        lines.extend(["", "Устройства появятся здесь после первого подключения."])
    return "\n".join(lines), key_devices_kb(key["id"], devices, page)


@router.callback_query(F.data.startswith("key_devices:"))
async def cb_key_devices(callback: CallbackQuery, bot: Bot):
    try:
        parts = callback.data.split(":")
        key_id = int(parts[1])
        page = max(0, int(parts[2])) if len(parts) > 2 else 0
    except (IndexError, ValueError):
        await safe_answer(callback, "Не удалось открыть устройства.", alert=True)
        return
    key = await get_key_by_id(key_id)
    if not key or key["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    await safe_answer(callback)
    try:
        text, keyboard = await _key_devices_view(callback.from_user.id, key, page)
    except Exception:
        logger.exception("Could not load connection details for user %s", callback.from_user.id)
        await send_or_edit(bot, callback,
                           "<b>Не удалось загрузить устройства</b>\n\nПопробуйте обновить список чуть позже.",
                           key_devices_kb(key_id), photo=DEVICES)
        return
    await send_or_edit(bot, callback, text, keyboard, photo=DEVICES)


@router.callback_query(F.data.startswith("key_device:"))
async def cb_key_device(callback: CallbackQuery, bot: Bot):
    try:
        _, raw_key_id, raw_device_id = callback.data.split(":", 2)
        key_id, device_id = int(raw_key_id), int(raw_device_id)
    except (IndexError, ValueError):
        await safe_answer(callback, "Устройство не найдено.", alert=True)
        return
    key = await get_key_by_id(key_id)
    if not key or key["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    await safe_answer(callback)
    try:
        info = await get_xui_devices(callback.from_user.id)
        device = next((item for item in info["devices"] if item["id"] == device_id), None)
        if not device:
            await send_or_edit(bot, callback, "Это устройство уже удалено.", key_devices_kb(key_id, info["devices"]), photo=DEVICES)
            return
        name = escape(device["model"] or device["user_agent"] or "Устройство")
        system = " ".join(part for part in (device["os"], device["os_version"]) if part)
        last_seen = _time_ago(device["last_seen"]) if device["last_seen"] else "неизвестно"
        text = (
            "<b>📱 Устройство</b>\n"
            f"🔗 {name}\n"
            f"⚙️ Система: {escape(system or 'не определена')}\n"
            f"🕒 Последняя активность: {last_seen}\n\n"
            "Удаление освободит место, если для подписки задан лимит. Если устройство снова обновит подписку, "
            "оно может зарегистрироваться повторно. Не узнаёте устройство? Напишите в поддержку — поможем проверить подключение."
        )
        await send_or_edit(bot, callback, text, key_device_detail_kb(key_id, device_id), photo=DEVICES)
    except Exception:
        logger.exception("Could not load device %s for user %s", device_id, callback.from_user.id)
        await send_or_edit(bot, callback, "Не удалось загрузить устройство. Обновите список чуть позже.", key_devices_kb(key_id), photo=DEVICES)


@router.callback_query(F.data.startswith("key_device_delete_prompt:"))
async def cb_key_device_delete_prompt(callback: CallbackQuery, bot: Bot):
    try:
        _, raw_key_id, raw_device_id = callback.data.split(":", 2)
        key_id, device_id = int(raw_key_id), int(raw_device_id)
    except (IndexError, ValueError):
        await safe_answer(callback, "Не удалось выполнить действие.", alert=True)
        return
    key = await get_key_by_id(key_id)
    if not key or key["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    await safe_answer(callback)
    text = (
        "<b>Удалить это устройство?</b>\n\n"
        "Оно освободит место, если включён лимит устройств. Если устройство снова обновит подписку, "
        "оно может зарегистрироваться повторно. Не узнаёте устройство? Напишите в поддержку — поможем проверить подключение."
    )
    await send_or_edit(bot, callback, text, confirm_delete_device_kb(key_id, device_id), photo=DEVICES)


@router.callback_query(F.data.startswith("key_device_delete:"))
async def cb_key_device_delete(callback: CallbackQuery, bot: Bot):
    try:
        _, raw_key_id, raw_device_id = callback.data.split(":", 2)
        key_id, device_id = int(raw_key_id), int(raw_device_id)
    except (IndexError, ValueError):
        await safe_answer(callback, "Не удалось выполнить действие.", alert=True)
        return
    key = await get_key_by_id(key_id)
    if not key or key["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    try:
        removed = await delete_xui_hwid_device(callback.from_user.id, device_id)
    except Exception as exc:
        logger.warning("Device deletion failed (%s)", type(exc).__name__)
        await safe_answer(callback, "Не удалось подтвердить удаление устройства. Попробуйте ещё раз.", alert=True)
        return
    if not removed:
        await safe_answer(
            callback,
            "Панель не подтвердила удаление. Обновите список устройств и попробуйте ещё раз.",
            alert=True,
        )
        return
    await safe_answer(callback, "Устройство удалено")
    try:
        text, keyboard = await _key_devices_view(callback.from_user.id, key)
        await send_or_edit(bot, callback, text, keyboard, photo=DEVICES)
    except Exception as exc:
        logger.warning("Device list refresh failed after deletion (%s)", type(exc).__name__)
        await send_or_edit(bot, callback, "Устройство удалено.", key_devices_kb(key_id), photo=DEVICES)


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
        "<b>Управление</b>\n\n"
        "Оплата, перевыпуск и удаление доступа."
    )
    await send_or_edit(bot, callback, text, key_settings_kb(key_id), photo=AUTO_RENEWAL)


def _pm_tariff_line(sub: dict | None) -> str:
    """Real tariff line from the auto-renew subscription row (no hardcoded data)."""
    if not sub:
        return ""
    months = (sub.get("days") or (sub.get("months") or 1) * 30) // 30
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
    if active and sub.get("ambiguous_key"):
        status = "⚠️ Требуется уточнить подписку"
        next_charge = (
            "Сохранённое автопродление не привязано к ключу: их несколько или нет ни одного. "
            "Автоматического списания не будет до уточнения. Продлите нужную подписку вручную или обратитесь в поддержку."
        )
    elif active:
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

    sub = await get_auto_renew_subscription(user_id, key_id if key_id > 0 else None, include_legacy=True)
    if sub and sub.get("key_id") is None:
        sub["ambiguous_key"] = len(await get_user_keys(user_id)) != 1
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
        photo=AUTO_RENEWAL,
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
        await send_or_edit(bot, callback, text, kb, photo=AUTO_RENEWAL)
        return

    warning = "Списания по выбранному автопродлению прекратятся."
    if expiry_str:
        warning += f" Доступ сохранится до <b>{expiry_str}</b>."

    text = (
        "<b>Отключить автопродление?</b>\n\n"
        f"💳 Способ оплаты: <b>{escape(card_title)}</b>\n\n"
        f"{warning}\n\n"
        "Доступ сохранится до конца оплаченного срока. Автопродление можно подключить снова при следующей оплате через ЮKassa."
    )
    await send_or_edit(bot, callback, text, autorenew_cancel_confirm_kb(key_id), photo=AUTO_RENEWAL)


async def _run_billing_change_under_lease(user_id: int, operation):
    """Serialize card cancellation/resumption with recurring invoice creation."""
    import asyncio
    import secrets
    from database import try_acquire_delivery_lease, release_delivery_lease

    token = secrets.token_urlsafe(24)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    while True:
        if await try_acquire_delivery_lease(user_id, token):
            break
        if loop.time() >= deadline:
            raise TimeoutError("another account operation is in progress")
        await asyncio.sleep(0.25)
    try:
        return await operation()
    finally:
        await release_delivery_lease(user_id, token)


@router.callback_query(F.data.startswith("autorenew_unbind_confirm:"))
async def cb_autorenew_unbind_confirm(callback: CallbackQuery, bot: Bot):
    """Disable auto-renew. Paid access is preserved until the period ends."""
    await safe_answer(callback)
    parts = callback.data.split(":")
    key_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    user_id = callback.from_user.id

    from database import unlink_auto_renew_payment_method
    from aiogram.types import InlineKeyboardMarkup

    try:
        res = await _run_billing_change_under_lease(
            user_id,
            lambda: unlink_auto_renew_payment_method(user_id, key_id if key_id > 0 else None),
        )
    except Exception:
        logger.exception("Cannot safely unlink recurring payment method for user %d", user_id)
        await send_or_edit(
            bot, callback,
            "Не удалось подтвердить отключение автопродления. "
            "Проверьте статус подписки и повторите попытку.",
            key_settings_kb(key_id) if key_id > 0 else back_to_menu(),
            photo=AUTO_RENEWAL,
        )
        return

    expiry_str = ""
    if res.get("expires_at"):
        expiry_str = fmt_date(res["expires_at"])

    keep_line = (
        f"Доступ сохранится до <b>{expiry_str}</b> — всё оплаченное работает."
        if expiry_str
        else "Активных оплаченных периодов не осталось."
    )

    if not res.get("unlinked"):
        text = "Для этой подписки сохранённый способ оплаты не найден. Проверьте настройки других подписок."
    else:
        text = (
            "<b>Автопродление отключено</b>\n\n"
            "По отключённому автопродлению новых списаний не будет. Проверьте настройки других подписок.\n"
            f"{keep_line}\n\n"
            "Автопродление можно подключить снова при следующей оплате через ЮKassa."
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← К ключу", callback_data=f"key_info:{key_id}")],
    ])
    await send_or_edit(bot, callback, text, kb, photo=AUTO_RENEWAL)


@router.callback_query(F.data.startswith("autorenew_enable:"))
async def cb_autorenew_enable(callback: CallbackQuery, bot: Bot):
    """Re-enable auto-renew using the previously saved payment method."""
    await safe_answer(callback)
    parts = callback.data.split(":")
    key_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    user_id = callback.from_user.id

    from database import resume_auto_renew
    from aiogram.types import InlineKeyboardMarkup

    try:
        reenabled = await _run_billing_change_under_lease(
            user_id,
            lambda: resume_auto_renew(user_id, key_id if key_id > 0 else None),
        )
    except Exception:
        logger.exception("Cannot safely resume recurring payment method for user %d", user_id)
        await send_or_edit(
            bot, callback,
            "Не удалось подтвердить включение автопродления. "
            "Возможна другая операция с подпиской. Повторите позже.",
            key_settings_kb(key_id) if key_id > 0 else back_to_menu(),
            photo=AUTO_RENEWAL,
        )
        return
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
    await send_or_edit(bot, callback, text, kb, photo=AUTO_RENEWAL)


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
        photo=AUTO_RENEWAL,
    )


@router.message(F.text.in_({"/cards", "/unsubscribe", "/cancel_subscription", "/payment_methods"}))
async def cmd_unsubscribe(message: Message, bot: Bot):
    """Direct command to manage auto-renew / saved card."""
    user_id = message.from_user.id

    sub, card_title, expiry_str = await _load_autorenew_context(user_id, 0)
    key_id = (sub.get("key_id") if sub else 0) or 0

    text = _get_payment_methods_text(card_title=card_title, is_checked=True, sub=sub, expiry_str=expiry_str)
    await send_with_photo(
        bot, message, text,
        manage_payment_methods_kb(
            card_title=card_title, is_checked=True, key_id=key_id,
            has_card=bool(card_title), autorenew_active=bool(sub and sub.get("status") == "active"),
        ),
        photo=AUTO_RENEWAL,
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
        "<b>Подключение</b>\n\nВыберите устройство."
    )
    await send_or_edit(bot, callback, text, connection_guide_kb(key_id, subscription_url), photo=CONNECTION)


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
        "<b>Продление подписки</b>\n\nВыберите срок и количество устройств.",
        tariff_selection_kb(devices=key_devices),
        photo=RENEWAL,
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
    active = int(k.get("expiry") or 0) > int(time.time())
    until = f" до <b>{fmt_date(k['expiry'])}</b>" if active else ""
    text = (
        f"<b>Удалить доступ «{remark}»?</b>\n\n"
        + (f"Подписка ещё активна{until}. " if active else "")
        + "Удаление сразу отключит VPN по этому ключу и не сохранит оставшийся срок.\n\n"
        "Если хотите только отменить будущие списания, отключите автопродление. "
        "Существующий доступ при этом сохранится."
    )
    await send_or_edit(bot, callback, text, confirm_delete_kb(key_id), photo=KEY)


# ---------------------------------------------------------------------------
# Delete key — confirmed
# ---------------------------------------------------------------------------

async def _report_unconfirmed_deletion(
    callback: CallbackQuery, user_id: int, key_id: int,
    stage: str, customer_text: str,
) -> None:
    """Persist a reconciliation incident without hiding the customer error."""
    from database import log_key_error
    logger.error("Unconfirmed deletion for user=%d key=%d stage=%s", user_id, key_id, stage)
    try:
        await log_key_error(
            user_id, "key_delete_reconciliation", "Manual verification required",
            {"key_id": key_id, "stage": stage},
        )
    except Exception:
        logger.exception("Could not record key %d reconciliation incident", key_id)
    from aiogram.types import InlineKeyboardMarkup
    from keyboards import support_url
    await callback.message.answer(
        customer_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Написать в поддержку", url=support_url())],
            [InlineKeyboardButton(text="Подписка", callback_data="my_keys")],
        ]),
    )


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

    async def _delete_under_account_lease():
        # Re-check ownership inside the same lease used by renewals and key
        # provisioning. Do not use a key loaded before the lease was taken.
        current = await get_key_by_id(key_id)
        if not current or current["user_id"] != user_id:
            await callback.message.answer("Ключ уже удалён или недоступен.")
            return

        # A partially completed three-day trial may have a persisted key while
        # trial_used is not yet committed. Never let deleting that key restore
        # eligibility for another free trial. This also covers old callback
        # buttons that remain in existing Telegram messages.
        if int(current.get("days") or 0) == 3:
            from database import has_trial_used, set_trial_used
            if not await has_trial_used(user_id):
                await set_trial_used(user_id)

        from database import has_recent_unsettled_checkout
        if await has_recent_unsettled_checkout(user_id):
            await callback.message.answer(
                "Удаление подписки пока недоступно: недавний платёж "
                "ещё может завершиться. Проверьте его статус и повторите позже."
            )
            return

        # The bot currently uses one 3x-ui client per Telegram user. Removing
        # one saved key must not delete that shared server client while other key
        # records remain.
        existing_keys = await get_user_keys(user_id)
        other_keys = [item for item in existing_keys if item.get("id") != key_id]

        # Stop billing first. A remote timeout may happen after a partial deletion;
        # restarting charges without verifying remaining access is unsafe.
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
                await _report_unconfirmed_deletion(
                    callback, user_id, key_id, "panel_unverified",
                    "Удаление на всех VPN-серверах пока не подтверждено. "
                    "Автопродление не будет включено автоматически. "
                    "Напишите в поддержку для проверки. Повторно оплачивать не нужно.",
                )
                return

        try:
            deleted = await delete_key_by_id(key_id)
        except Exception:
            # The remote user may already be gone. Never reactivate billing or
            # let a database outage hide this partially completed deletion.
            logger.exception("Key %d removal failed after remote reconciliation", key_id)
            deleted = False
        if not deleted:
            if other_keys and renew_was_active:
                # No remote client was touched; restoring billing is safe only here.
                await resume_auto_renew(user_id, key_id)
            if not other_keys:
                await _report_unconfirmed_deletion(
                    callback, user_id, key_id, "database_after_panel",
                    "VPN-доступ на серверах удалён, но запись в боте осталась. "
                    "Автопродление не возобновлено. Не оплачивайте повторно. "
                    "Напишите в поддержку, чтобы завершить операцию.",
                )
            else:
                logger.error("Could not delete key row %d for user %d", key_id, user_id)
                await callback.message.answer(
                    "Не удалось обновить подписку. Попробуйте ещё раз или напишите в поддержку."
                )
            return

        logger.info("Key %d deleted by user %d (other_keys=%d)", key_id, user_id, len(other_keys))

        keys = await get_user_keys(user_id)
        if not keys:
            await send_with_photo(
                bot, callback,
                f"Ключ «{remark}» удалён. Подключение по нему отключено.",
                back_to_menu(),
                photo=KEY,
            )
        elif len(keys) == 1:
            text, keyboard = await _key_info_content(user_id, keys[0])
            await send_or_edit(bot, callback, f"Ключ «{remark}» удалён.\n\n{text}", keyboard, photo=KEY)
        else:
            text = (
                f"Ключ «{remark}» удалён.\n\n"
                "Остальные подписки и подключения работают как прежде. Выберите нужную подписку ниже."
            )
            now = int(time.time())
            active = [key for key in keys if int(key.get("expiry") or 0) > now]
            expired = [key for key in keys if int(key.get("expiry") or 0) <= now]
            if not active and expired:
                await send_with_photo(bot, callback, "<b>История подписок</b>", key_history_kb(expired), photo=KEY)
            else:
                await send_with_photo(bot, callback, text, my_keys_list_kb(active, has_history=bool(expired)), photo=KEY)

    try:
        await _run_billing_change_under_lease(user_id, _delete_under_account_lease)
    except Exception:
        logger.exception("Could not safely serialize key deletion for user %d", user_id)
        await callback.message.answer(
            "Подписка занята другой операцией или временно недоступна. "
            "Удаление не подтверждено — проверьте состояние подписки и повторите позже."
        )

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
