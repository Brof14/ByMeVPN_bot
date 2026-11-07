"""Short, platform-specific ByMeVPN setup guide using Happ's official downloads."""
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
)

from keyboards import connection_guide_kb, additional_platforms_kb, support_url, feedback_url
from utils import send_with_photo, send_or_edit, safe_answer
from bot_assets import SUPPORT, CONNECTION, ANDROID, IPHONE, WINDOWS, LINUX
from bot_style import themed_button as InlineKeyboardButton

logger = logging.getLogger(__name__)
router = Router()

BYMEVPN_ANDROID_PAGE = "https://bymevpn.duckdns.org/app/"
HAPP_SITE = "https://happ.info/"
HAPP_IOS_GLOBAL = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_ANDROID = "https://play.google.com/store/apps/details?id=com.happproxy"
HAPP_ANDROID_APK = "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk"
HAPP_WINDOWS = "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"
HAPP_MACOS_GLOBAL = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_MACOS_DMG = "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg"
HAPP_LINUX_DEB = "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.linux.x64.deb"
HAPP_LINUX_RPM = "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.linux.x64.rpm"
HAPP_LINUX_ARCH = "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.linux.x64.pkg.tar.zst"


@router.callback_query(F.data == "connection_guide")
async def cb_guide_menu(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    text = (
        "<b>Подключение</b>\n\n"
        "Выберите устройство."
    )
    await _track_guide_event(callback.from_user.id, "connection_guide_open")
    await send_or_edit(bot, callback, text, connection_guide_kb(), photo=CONNECTION)


@router.callback_query(F.data.startswith("connection_guide:"))
async def cb_guide_for_key(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    try:
        key_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    from database import get_key_by_id
    key = await get_key_by_id(key_id)
    if not key or key["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    text = (
        f"<b>Подключение · {escape(key.get('remark') or f'Ключ #{key_id}')}</b>\n\n"
        "Выберите устройство."
    )
    await send_or_edit(bot, callback, text, connection_guide_kb(key_id), photo=CONNECTION)


@router.callback_query(F.data == "trouble")
async def cb_trouble(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    await _track_guide_event(callback.from_user.id, "connection_help_open")
    text = (
        "<b>Чем помочь?</b>\n\n"
        "С чем возникла проблема?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="VPN не подключается", callback_data="support_topic:no_connection")],
        [InlineKeyboardButton(text="Нет интернета с VPN", callback_data="support_topic:no_internet")],
        [InlineKeyboardButton(text="Проблема с приложением", callback_data="support_topic:app")],
        [InlineKeyboardButton(text="Не добавляется подписка", callback_data="support_topic:import")],
        [InlineKeyboardButton(text="Оплата и подписка", callback_data="support_topic:billing")],
        [InlineKeyboardButton(text="Устройства", callback_data="support_topic:devices")],
        [InlineKeyboardButton(text="Написать в поддержку", url=support_url())],
        [InlineKeyboardButton(text="Сообщить об ошибке или идее", url=feedback_url())],
        [InlineKeyboardButton(text="← Назад", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, kb, photo=SUPPORT)


@router.callback_query(F.data.startswith("support_topic:"))
async def cb_support_topic(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    topic = callback.data.split(":", 1)[1]
    guidance = {
        "no_connection": (
            "<b>VPN не подключается</b>\n\n"
            "1. Проверьте интернет без VPN.\n2. Обновите подписку в приложении.\n3. Подключитесь повторно."
        ),
        "no_internet": (
            "<b>VPN включён, но интернета нет</b>\n\n"
            "Отключите другие VPN. Затем обновите подписку и подключитесь снова."
        ),
        "app": (
            "<b>Не работает приложение</b>\n\n"
            "Перезапустите приложение. Если ошибка повторится, напишите в поддержку."
        ),
        "import": (
            "<b>Не добавляется подписка</b>\n\n"
            "Скопируйте ссылку в разделе «Подписка» и импортируйте её в VPN-приложение."
        ),
        "billing": (
            "<b>Оплата и подписка</b>\n\n"
            "Если платёж прошёл, а доступ не появился, не оплачивайте повторно. "
            "Проверьте статус подписки или напишите в поддержку."
        ),
        "devices": (
            "<b>Устройства</b>\n\n"
            "Откройте «Подписка» → «Устройства». Удалите старое устройство, "
            "если лимит занят."
        ),
    }
    text = guidance.get(topic)
    if not text:
        await safe_answer(callback, "Подсказка не найдена.", alert=True)
        return
    rows = []
    if topic in {"billing", "devices", "import"}:
        rows.append([InlineKeyboardButton(text="Подписка", callback_data="my_keys")])
    if topic in {"no_connection", "no_internet", "app", "import"}:
        rows.append([InlineKeyboardButton(text="Инструкция по подключению", callback_data="connection_guide")])
    rows.extend([
        [InlineKeyboardButton(text="Написать в поддержку", url=support_url())],
        [InlineKeyboardButton(text="← Назад", callback_data="trouble")],
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await send_with_photo(bot, callback, text, kb, photo=SUPPORT)


@router.callback_query(F.data.startswith("guide_more"))
async def cb_guide_more(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    key_id = None
    if ":" in callback.data:
        try:
            key_id = int(callback.data.split(":", 1)[1])
        except ValueError:
            await safe_answer(callback, "Ключ не найден.", alert=True)
            return
        from database import get_key_by_id
        key = await get_key_by_id(key_id)
        if not key or key["user_id"] != callback.from_user.id:
            await safe_answer(callback, "Ключ не найден.", alert=True)
            return
    text = "<b>Другие устройства</b>\n\nВыберите платформу."
    await send_or_edit(bot, callback, text, additional_platforms_kb(key_id), photo=CONNECTION)


_GUIDES = {
    "ios": (
        "<b>iPhone и iPad</b>\n\n"
        "1. Установите Happ.\n"
        "2. Скопируйте ссылку в разделе «Подписка».\n"
        "3. Добавьте её в Happ и подключитесь."
    ),
    "android": (
        "<b>Android</b>\n\n"
        "1. Установите ByMeVPN по кнопке ниже.\n"
        "2. Войдите и подключитесь.\n\n"
        "Если приложение недоступно, установите Happ и добавьте в него ссылку из раздела «Подписка»."
    ),
    "android_tv": (
        "<b>Android TV</b>\n\n"
        "1. Установите Happ из Google Play по кнопке ниже.\n"
        "2. В разделе «Подписка» скопируйте ссылку подписки.\n"
        "3. Добавьте ссылку в Happ и подключитесь."
    ),
    "ios_mac": (
        "<b>iPhone, iPad и Mac</b>\n\n"
        "Выберите подходящее приложение по кнопке ниже. Затем скопируйте ссылку в разделе «Подписка», "
        "добавьте её в Happ и подключитесь."
    ),
    "windows": (
        "<b>Windows</b>\n\n"
        "1. Установите Happ.\n"
        "2. Скопируйте ссылку в разделе «Подписка».\n"
        "3. Импортируйте её и подключитесь."
    ),
    "macos": (
        "<b>macOS</b>\n\n"
        "1. Установите Happ из App Store или скачайте установщик по кнопкам ниже.\n"
        "2. В разделе «Подписка» скопируйте ссылку подписки.\n"
        "3. Добавьте ссылку в Happ и подключитесь."
    ),
    "linux": (
        "<b>Linux</b>\n\n"
        "1. Скачайте пакет для своего дистрибутива по кнопкам ниже и установите его.\n"
        "2. В разделе «Подписка» скопируйте ссылку подписки.\n"
        "3. Добавьте ссылку в Happ и подключитесь."
    ),
}


@router.callback_query(F.data == "guide_linux")
async def cb_guide_linux(callback: CallbackQuery, bot: Bot):
    await _send_platform_guide(callback, bot, "linux")


@router.callback_query(F.data.startswith("guide_linux_"))
async def cb_guide_linux_legacy(callback: CallbackQuery, bot: Bot):
    """Keep old saved Linux-distro buttons working after the guide simplification."""
    await _send_platform_guide(callback, bot, "linux")


@router.callback_query(F.data.in_({"guide_ios", "guide_android", "guide_windows", "guide_macos", "guide_android_tv", "guide_ios_mac"}))
async def cb_platform_guide(callback: CallbackQuery, bot: Bot):
    await _send_platform_guide(callback, bot, callback.data.removeprefix("guide_"))


@router.callback_query(F.data.regexp(r"^guide_(ios|android|windows|macos|android_tv|ios_mac|linux):[0-9]+$"))
async def cb_platform_guide_for_key(callback: CallbackQuery, bot: Bot):
    platform_and_key = callback.data.removeprefix("guide_")
    platform, raw_key_id = platform_and_key.rsplit(":", 1)
    try:
        key_id = int(raw_key_id)
    except ValueError:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    from database import get_key_by_id
    key = await get_key_by_id(key_id)
    if not key or key["user_id"] != callback.from_user.id:
        await safe_answer(callback, "Ключ не найден.", alert=True)
        return
    await _send_platform_guide(callback, bot, platform, key_id=key_id)


async def _send_platform_guide(callback: CallbackQuery, bot: Bot, platform: str, key_id: int | None = None):
    await safe_answer(callback)
    await _track_guide_event(callback.from_user.id, "connection_guide_platform", platform)
    text = _GUIDES.get(platform)
    if text is None:
        text = f"Инструкция не найдена. Откройте официальный сайт Happ: <a href=\"{HAPP_SITE}\">happ.info</a>."
    choose_callback = f"connection_guide:{key_id}" if key_id else "connection_guide"
    download_rows = {
        "android": [
            [InlineKeyboardButton(text="ByMeVPN для Android", url=BYMEVPN_ANDROID_PAGE)],
            [InlineKeyboardButton(text="Happ · резервный клиент", url=HAPP_ANDROID)],
        ],
        "android_tv": [[InlineKeyboardButton(text="Happ в Google Play", url=HAPP_ANDROID)]],
        "ios": [[
            InlineKeyboardButton(text="Официальная страница Happ", url=HAPP_SITE),
            InlineKeyboardButton(text="App Store · другие регионы", url=HAPP_IOS_GLOBAL),
        ]],
        "windows": [[InlineKeyboardButton(text="Скачать Happ для Windows", url=HAPP_WINDOWS)]],
        "macos": [
            [
                InlineKeyboardButton(text="Официальная страница Happ", url=HAPP_SITE),
                InlineKeyboardButton(text="App Store · другие регионы", url=HAPP_MACOS_GLOBAL),
            ],
            [InlineKeyboardButton(text="Скачать Happ для Mac", url=HAPP_MACOS_DMG)],
        ],
        "linux": [
            [
                InlineKeyboardButton(text="Ubuntu / Debian · .deb", url=HAPP_LINUX_DEB),
                InlineKeyboardButton(text="Fedora · .rpm", url=HAPP_LINUX_RPM),
            ],
            [InlineKeyboardButton(text="Arch Linux", url=HAPP_LINUX_ARCH)],
        ],
        # Compatibility for already-sent device buttons.
        "ios_mac": [
            [
                InlineKeyboardButton(text="iPhone · официальная страница", url=HAPP_SITE),
                InlineKeyboardButton(text="iPhone · другие регионы", url=HAPP_IOS_GLOBAL),
            ],
            [
                InlineKeyboardButton(text="Mac · официальная страница", url=HAPP_SITE),
                InlineKeyboardButton(text="Mac · другие регионы", url=HAPP_MACOS_GLOBAL),
            ],
            [InlineKeyboardButton(text="Установщик для Mac", url=HAPP_MACOS_DMG)],
        ],
    }.get(platform, [])
    guide_rows = list(download_rows)
    if key_id:
        from database import get_key_by_id
        key = await get_key_by_id(key_id)
        if not key or key["user_id"] != callback.from_user.id:
            await safe_answer(callback, "Ключ не найден.", alert=True)
            return
        if key.get("key"):
            # Install/open the client first. Manual import is a fallback,
            # not the primary action for a customer unfamiliar with VPN.
            guide_rows.append([InlineKeyboardButton(
                text="Скопировать ссылку",
                copy_text={"text": key["key"]},
            )])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *guide_rows,
        [InlineKeyboardButton(text="← Назад", callback_data=choose_callback)],
    ])
    platform_art = {
        "ios": IPHONE,
        "android": ANDROID,
        "android_tv": ANDROID,
        "ios_mac": IPHONE,
        "windows": WINDOWS,
        "macos": CONNECTION,
        "linux": LINUX,
    }.get(platform, CONNECTION)
    await send_or_edit(bot, callback, text, kb, photo=platform_art)


async def _track_guide_event(user_id: int, event_type: str, platform: str | None = None) -> None:
    from database import log_analytics_event
    await log_analytics_event(user_id=user_id, event_type=event_type, details=platform)
