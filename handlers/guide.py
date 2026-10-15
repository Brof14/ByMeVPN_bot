"""Short, platform-specific ByMeVPN setup guide using Happ's official downloads."""
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from keyboards import connection_guide_kb, guide_back_kb
from utils import send_with_photo, safe_answer

logger = logging.getLogger(__name__)
router = Router()

HAPP_SITE = "https://www.happ.su/main/ru"
HAPP_IOS_RU = "https://apps.apple.com/ru/app/happ-lite/id6799917773"
HAPP_IOS_GLOBAL = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_ANDROID = "https://play.google.com/store/apps/details?id=com.happproxy"
HAPP_ANDROID_APK = "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk"
HAPP_WINDOWS = "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"
HAPP_MACOS_RU = "https://apps.apple.com/ru/app/happ-proxy-utility/id6783623643"
HAPP_MACOS_GLOBAL = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_MACOS_DMG = "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg"
HAPP_LINUX = "https://github.com/Happ-proxy/happ-desktop/releases/latest"


@router.callback_query(F.data == "connection_guide")
async def cb_guide_menu(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    text = (
        "<b>Подключение ByMeVPN</b>\n\n"
        "1. Выберите устройство.\n"
        "2. Установите Happ по официальной ссылке.\n"
        "3. В боте откройте «Мои ключи» → ваш ключ → «Скопировать ссылку».\n"
        "4. Вернитесь в Happ, нажмите «Вставить из буфера», затем «Подключить».\n\n"
        "Если Happ не находится в российском App Store, ниже есть официальное приложение для российского каталога. "
        "Не скачивайте VPN-клиенты с неизвестных сайтов."
    )
    await send_with_photo(bot, callback, text, connection_guide_kb())


@router.callback_query(F.data == "trouble")
async def cb_trouble(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    text = (
        "⚠️ <b>Не получается подключиться?</b>\n\n"
        "1. Отключите другие VPN и прокси.\n"
        "2. В Happ откройте подписку ByMeVPN и нажмите «Обновить».\n"
        "3. Если подписки нет, скопируйте ссылку заново: «Мои ключи» → ключ → «Скопировать ссылку».\n"
        "4. Включите VPN ещё раз и откройте сайт.\n\n"
        "Если не помогло, отправьте в поддержку модель устройства, название приложения и скриншот ошибки. "
        "Саму ссылку-подписку не пересылайте — это ваш доступ."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys")],
        [InlineKeyboardButton(text="📋 Инструкция", callback_data="connection_guide")],
        [InlineKeyboardButton(text="Написать в поддержку", url="https://t.me/ByMeVPN_support_bot")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")],
    ])
    await send_with_photo(bot, callback, text, kb)


_GUIDES = {
    "ios": (
        "🍏 <b>iPhone и iPad</b>\n\n"
        f"1. Установите Happ из российского App Store: <a href=\"{HAPP_IOS_RU}\">Happ для России</a>.\n"
        f"Если у вашего Apple ID другой регион, откройте <a href=\"{HAPP_IOS_GLOBAL}\">глобальную версию</a>.\n\n"
        "2. В боте: «Мои ключи» → ключ → «Скопировать ссылку».\n"
        "3. Откройте Happ и разрешите добавление VPN-конфигурации, когда iPhone спросит.\n"
        "4. Нажмите «Вставить из буфера», затем «Подключить»."
    ),
    "android": (
        "🤖 <b>Android</b>\n\n"
        f"1. Установите <a href=\"{HAPP_ANDROID}\">Happ из Google Play</a>. "
        f"Если Play недоступен, скачайте APK только с <a href=\"{HAPP_ANDROID_APK}\">официальной страницы Happ на GitHub</a>.\n\n"
        "2. Скопируйте ссылку: бот → «Мои ключи» → ключ → «Скопировать ссылку».\n"
        "3. Откройте Happ и нажмите «Вставить из буфера».\n"
        "4. Нажмите кнопку подключения и подтвердите запрос Android."
    ),
    "windows": (
        "💻 <b>Windows</b>\n\n"
        f"1. <a href=\"{HAPP_WINDOWS}\">Скачайте Happ для Windows x64</a> с официального GitHub Happ.\n"
        "2. Установите и откройте приложение.\n"
        "3. Скопируйте ссылку в боте: «Мои ключи» → ключ → «Скопировать ссылку».\n"
        "4. В Happ нажмите «Вставить из буфера», затем кнопку подключения."
    ),
    "macos": (
        "🍎 <b>macOS</b>\n\n"
        f"1. Для российского Apple ID установите <a href=\"{HAPP_MACOS_RU}\">Happ из российского App Store</a>. "
        f"Для другого региона — <a href=\"{HAPP_MACOS_GLOBAL}\">глобальная версия</a>. "
        f"Можно также скачать <a href=\"{HAPP_MACOS_DMG}\">официальный универсальный установщик</a>.\n\n"
        "2. Скопируйте ссылку в боте: «Мои ключи» → ключ → «Скопировать ссылку».\n"
        "3. Откройте Happ, вставьте ссылку и нажмите кнопку подключения."
    ),
    "linux": (
        "🐧 <b>Linux</b>\n\n"
        f"1. Откройте <a href=\"{HAPP_LINUX}\">официальные релизы Happ</a> и скачайте пакет для своей системы (.deb, .rpm или Arch).\n"
        "2. Установите пакет средствами своей системы и откройте Happ.\n"
        "3. Скопируйте ссылку в боте: «Мои ключи» → ключ → «Скопировать ссылку».\n"
        "4. В Happ вставьте ссылку и нажмите кнопку подключения."
    ),
}


@router.callback_query(F.data == "guide_linux")
async def cb_guide_linux(callback: CallbackQuery, bot: Bot):
    await _send_platform_guide(callback, bot, "linux")


@router.callback_query(F.data.startswith("guide_linux_"))
async def cb_guide_linux_legacy(callback: CallbackQuery, bot: Bot):
    """Keep old saved Linux-distro buttons working after the guide simplification."""
    await _send_platform_guide(callback, bot, "linux")


@router.callback_query(F.data.in_({"guide_ios", "guide_android", "guide_windows", "guide_macos"}))
async def cb_platform_guide(callback: CallbackQuery, bot: Bot):
    await _send_platform_guide(callback, bot, callback.data.removeprefix("guide_"))


async def _send_platform_guide(callback: CallbackQuery, bot: Bot, platform: str):
    await safe_answer(callback)
    text = _GUIDES.get(platform)
    if text is None:
        text = f"Инструкция не найдена. Откройте официальный сайт Happ: <a href=\"{HAPP_SITE}\">happ.su</a>."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Выбрать устройство", callback_data="connection_guide")],
        [InlineKeyboardButton(text="⚠️ Не получается?", callback_data="trouble")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu"),
         InlineKeyboardButton(text="Поддержка", url="https://t.me/ByMeVPN_support_bot")],
    ])
    await send_with_photo(bot, callback, text, kb)
