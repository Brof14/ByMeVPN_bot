"""My Keys: list, info, renew, delete. (Fix: guide button, photos)"""
import time
import asyncio
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from database import (
    get_user_keys, get_key_by_id, delete_key_by_id, update_user_key_credentials,
)
from xui_client import (
    delete_xui_user,
    get_xui_devices, delete_xui_hwid_device, rotate_xui_client_credentials,
    restore_xui_client_credentials, CredentialRotationRollbackError,
)
from keyboards import (
    my_keys_kb, my_keys_list_kb, key_detail_kb, confirm_delete_kb,
    payment_kb, back_to_menu, connection_guide_kb, tariff_selection_kb,
    autorenew_cancel_confirm_kb, manage_payment_methods_kb, key_settings_kb,
    key_devices_kb, key_device_detail_kb, confirm_delete_device_kb,
    confirm_reissue_key_kb,
)
from utils import send_with_photo, send_or_edit, safe_answer
from constants import format_timestamp as fmt_date, format_days_left as fmt_days_left
from states import BuyFlow

logger = logging.getLogger(__name__)
router = Router()
_credential_rotation_lock = asyncio.Lock()


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

    if len(keys) == 1:
        text, keyboard = await _key_info_content(user_id, keys[0])
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        return

    await message.answer(
        "<b>Ваши VPN-подписки</b>\n\nВыберите подписку для подключения или управления.",
        parse_mode="HTML", reply_markup=my_keys_list_kb(keys),
    )


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

    if len(keys) == 1:
        text, keyboard = await _key_info_content(user_id, keys[0])
        await send_or_edit(bot, callback, text, keyboard)
        return

    text = "<b>Ваши VPN-подписки</b>\n\nВыберите подписку для подключения или управления."
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

    text, keyboard = await _key_info_content(callback.from_user.id, k)
    await send_or_edit(bot, callback, text, keyboard)


async def _key_info_content(user_id: int, k: dict) -> tuple[str, object]:
    key_id = int(k["id"])

    now = int(time.time())
    status = "Активен" if k["expiry"] > now else "Срок завершён"
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
            f"📱 Устройства: <b>{registered} из {device_limit}</b>\n"
            if device_limit else f"📱 Устройств в подписке: <b>{registered}</b>\n"
        )

    from database import get_auto_renew_subscription
    sub = await get_auto_renew_subscription(user_id, key_id)
    has_autorenew = sub is not None
    autorenew_active = sub is not None and sub.get("status") == "active"
    ar_line = ""
    if sub:
        if autorenew_active:
            ar_line = "Автопродление: включено\n"
        elif sub.get("status") == "failed":
            ar_line = "Автопродление: ошибка списания\n"
        else:
            ar_line = "Автопродление: отключено\n"

    text = (
        "<b>Мой VPN</b>\n\n"
        f"{status}\n"
        f"Действует до: <b>{fmt_date(k['expiry'])}</b>\n"
        f"{ar_line}{devices_info}\n"
        "<b>Ссылка для подключения</b>\n"
        f"<code>{escape(subscription_url)}</code>"
    )

    return text, key_detail_kb(key_id, has_autorenew=has_autorenew, autorenew_active=autorenew_active)


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
        "<b>📱 Устройства</b>",
        usage_line,
        "Трафик общий для всех устройств.",
    ]
    if devices:
        lines.extend(["", "Выберите устройство, чтобы посмотреть сведения или удалить его."])
        pages = max(1, (len(devices) + 9) // 10)
        if pages > 1:
            lines.append(f"Страница {min(page + 1, pages)} из {pages}")
        lines.append("Тариф с другим лимитом можно выбрать при продлении; сумму бот покажет до оплаты.")
    else:
        lines.extend(["", "Список заполнится после подключения через Happ. Другие приложения могут не передавать название устройства и систему."])
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
                           key_devices_kb(key_id))
        return
    await send_or_edit(bot, callback, text, keyboard)


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
            await send_or_edit(bot, callback, "Это устройство уже удалено.", key_devices_kb(key_id, info["devices"]))
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
            "оно может зарегистрироваться повторно. Чтобы прекратить доступ, перевыпустите ссылку."
        )
        await send_or_edit(bot, callback, text, key_device_detail_kb(key_id, device_id))
    except Exception:
        logger.exception("Could not load device %s for user %s", device_id, callback.from_user.id)
        await send_or_edit(bot, callback, "Не удалось загрузить устройство. Обновите список чуть позже.", key_devices_kb(key_id))


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
        "Оно освободит место, если включён лимит устройств. Если на нём останется ссылка, устройство может "
        "зарегистрироваться снова. Для прекращения доступа перевыпустите ссылку подписки."
    )
    await send_or_edit(bot, callback, text, confirm_delete_device_kb(key_id, device_id))


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
        await delete_xui_hwid_device(callback.from_user.id, device_id)
    except Exception:
        logger.exception("Could not remove device %s for user %s", device_id, callback.from_user.id)
        await safe_answer(callback, "Не удалось удалить устройство. Попробуйте позже.", alert=True)
        return
    await safe_answer(callback, "Устройство удалено")
    try:
        text, keyboard = await _key_devices_view(callback.from_user.id, key)
        await send_or_edit(bot, callback, text, keyboard)
    except Exception:
        logger.exception("Could not refresh device list after deleting for user %s", callback.from_user.id)
        await send_or_edit(bot, callback, "Устройство удалено.", key_devices_kb(key_id))


@router.callback_query(F.data.startswith("key_reissue_prompt:"))
async def cb_key_reissue_prompt(callback: CallbackQuery, bot: Bot):
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    key = await get_key_by_id(key_id)
    if not key or key["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    await safe_answer(callback)
    text = (
        "<b>Перевыпустить ссылку и ключ доступа?</b>\n\n"
        "Текущие подключения сразу отключатся. На каждом устройстве нужно будет добавить новую ссылку.\n"
        "Доступ обновится для всех ключей аккаунта. Срок, тариф и трафик сохранятся."
    )
    await send_or_edit(bot, callback, text, confirm_reissue_key_kb(key_id))


@router.callback_query(F.data.startswith("key_reissue_confirm:"))
async def cb_key_reissue_confirm(callback: CallbackQuery, bot: Bot):
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    key = await get_key_by_id(key_id)
    if not key or key["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    await safe_answer(callback, "Обновляю ссылку…")
    try:
        async with _credential_rotation_lock:
            rotated = await rotate_xui_client_credentials(callback.from_user.id)
            if not rotated:
                raise RuntimeError("VPN credentials were not rotated")
            saved = await update_user_key_credentials(
                callback.from_user.id, rotated["uuid"], rotated["subscription_url"],
            )
            if not saved:
                try:
                    restored = await restore_xui_client_credentials(
                        callback.from_user.id, rotated["previous_uuid"], rotated["previous_sub_id"],
                    )
                except Exception:
                    restored = False
                    logger.exception("Credential rollback failed for user %s", callback.from_user.id)
                if not restored:
                    logger.critical("VPN credentials rotated in panel but DB update and rollback failed for user %s", callback.from_user.id)
                await send_or_edit(bot, callback,
                                   "Не удалось завершить перевыпуск. " + (
                                       "Текущий доступ восстановлен." if restored else
                                       "Панель не подтвердила восстановление доступа. Напишите в поддержку, поможем восстановить подключение."
                                   ), back_to_menu())
                return
        await send_or_edit(bot, callback,
                           "Ссылка перевыпущена. Старые подключения отключены. Откройте ключ и добавьте новую ссылку на устройствах.",
                           key_devices_kb(key_id))
    except CredentialRotationRollbackError:
        logger.exception("Credential rotation rollback failed for user %s", callback.from_user.id)
        await send_or_edit(bot, callback,
                           "Не удалось подтвердить восстановление доступа. Напишите в поддержку — поможем вернуть подключение.",
                           back_to_menu())
    except Exception:
        logger.exception("Could not reissue VPN link for user %s", callback.from_user.id)
        await send_or_edit(bot, callback,
                           "Не удалось перевыпустить ссылку. Изменения не применены; попробуйте позже.",
                           confirm_reissue_key_kb(key_id))


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
    elif len(keys) == 1:
        text, keyboard = await _key_info_content(user_id, keys[0])
        await send_or_edit(bot, callback, f"Ключ «{remark}» удалён.\n\n{text}", keyboard)
    else:
        text = (
            f"Ключ «{remark}» удалён.\n\n"
            "Остальные подписки и подключения работают как прежде. Выберите нужную подписку ниже."
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
