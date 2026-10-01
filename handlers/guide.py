"""Connection guide for all platforms — with logo photo and updated links."""
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from keyboards import (
    connection_guide_kb, guide_back_kb,
    linux_distro_kb, linux_guide_back_kb,
)
from utils import send_with_photo, safe_answer

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data == "connection_guide")
async def cb_guide_menu(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    text = (
        "<b>Выберите вашу платформу</b>\n\n"
        "Нажмите на вашу операционную систему "
        "и следуйте простой инструкции."
    )
    await send_with_photo(bot, callback, text, connection_guide_kb())


_GUIDES: dict[str, str] = {
    "ios": (
        "🍏 <b>iOS (iPhone / iPad) — Инструкция подключения</b>\n\n"
        "1. Установите INCY из App Store:\n"
        "<a href='https://apps.apple.com/ru/app/incy/id6756943388'>INCY</a>\n\n"
        "2. Вернитесь в ByMeVPN и откройте «Мои ключи»\n\n"
        "3. Откройте свой ключ и скопируйте ссылку\n\n"
        "4. Откройте INCY\n\n"
        "5. Нажмите кнопку «Импортировать подписку» или «Вставить из буфера»\n\n"
        "6. Выберите ByMeVPN\n\n"
        "7. Подключитесь"
    ),
    "android": (
        "🤖 <b>Android — Инструкция подключения</b>\n\n"
        "1. Установите HAPP Proxy из Google Play:\n"
        "<a href='https://play.google.com/store/apps/details?id=com.happproxy'>HAPP Proxy</a>\n\n"
        "2. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку\n\n"
        "3. Откройте HAPP Proxy\n\n"
        "4. Нажмите кнопку «Вставить из буфера»\n\n"
        "5. Нажмите кнопку подключения в центре экрана"
    ),
    "windows": (
        "💻 <b>Windows — Инструкция подключения</b>\n\n"
        "1. Скачайте HAPP для Windows:\n"
        "<a href='https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe'>Скачать HAPP</a>\n\n"
        "2. Запустите установку и дождитесь завершения\n\n"
        "3. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку\n\n"
        "4. В HAPP нажмите «Вставить из буфера»\n\n"
        "5. Нажмите кнопку Connect для подключения"
    ),
    "macos": (
        "🍎 <b>macOS — Инструкция подключения</b>\n\n"
        "1. Скачайте HAPP из Mac App Store:\n"
        "<a href='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973'>HAPP Proxy Utility Plus</a>\n\n"
        "2. Запустите приложение\n\n"
        "3. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку\n\n"
        "4. В HAPP нажмите «Вставить из буфера»\n\n"
        "5. Нажмите кнопку Connect для подключения"
    ),
}

_LINUX_GUIDES: dict[str, str] = {
    "arch": (
        "🐧 <b>Arch / EndeavourOS / Manjaro</b>\n\n"
        "1. Скачайте v2rayN для Linux:\n"
        "<a href='https://github.com/2dust/v2rayN/releases'>GitHub → Releases</a> → Linux → <code>v2rayN-linux-64.zip</code>\n\n"
        "2. Распакуйте архив и запустите v2rayN\n\n"
        "3. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку\n\n"
        "4. Добавьте ссылку в v2rayN\n\n"
        "5. Включите System Proxy и Global\n\n"
        "6. Подключитесь"
    ),
    "ubuntu": (
        "🐧 <b>Ubuntu / Debian / Linux Mint</b>\n\n"
        "1. Скачайте v2rayN для Linux (.deb):\n"
        "<a href='https://github.com/2dust/v2rayN/releases'>GitHub → Releases</a> → <code>v2rayN-linux-64.deb</code>\n\n"
        "2. Откройте терминал в папке с файлом и выполните:\n"
        "<code>sudo apt install -y ./v2rayN-linux-64.deb</code>\n\n"
        "3. Откройте v2rayN\n\n"
        "4. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку\n\n"
        "5. Добавьте ссылку в v2rayN\n\n"
        "6. Включите System Proxy и Global\n\n"
        "7. Подключитесь"
    ),
    "fedora": (
        "🐧 <b>Fedora / RHEL</b>\n\n"
        "1. Скачайте v2rayN для Linux (.rpm):\n"
        "<a href='https://github.com/2dust/v2rayN/releases'>GitHub → Releases</a> → <code>v2rayN-linux-rhel-64.rpm</code>\n\n"
        "2. Откройте терминал в папке с файлом и выполните:\n"
        "<code>sudo dnf install -y ./v2rayN-linux-rhel-64.rpm</code>\n\n"
        "3. Откройте v2rayN\n\n"
        "4. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку\n\n"
        "5. Добавьте ссылку в v2rayN\n\n"
        "6. Включите System Proxy и Global\n\n"
        "7. Подключитесь"
    ),
    "other": (
        "🐧 <b>Другой Linux</b>\n\n"
        "1. Скачайте v2rayN для Linux:\n"
        "<a href='https://github.com/2dust/v2rayN/releases'>GitHub → Releases</a> → <code>v2rayN-linux-64.zip</code>\n\n"
        "2. Распакуйте архив и запустите v2rayN\n\n"
        "3. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку\n\n"
        "4. Добавьте ссылку в v2rayN\n\n"
        "5. Включите System Proxy и Global\n\n"
        "6. Подключитесь"
    ),
}


@router.callback_query(F.data == "guide_linux")
async def cb_guide_linux(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    text = (
        "🐧 <b>Linux</b>\n\n"
        "Выберите вашу систему:"
    )
    await send_with_photo(bot, callback, text, linux_distro_kb())


@router.callback_query(F.data.startswith("guide_linux_"))
async def cb_guide_linux_distro(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    distro = callback.data.split("guide_linux_", 1)[1]
    text = _LINUX_GUIDES.get(
        distro,
        "Инструкция для данного дистрибутива в разработке.",
    )
    await send_with_photo(bot, callback, text, linux_guide_back_kb())


@router.callback_query(F.data.in_({"guide_ios", "guide_android", "guide_windows", "guide_macos"}))
async def cb_platform_guide(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    platform = callback.data.split("_", 1)[1]
    text = _GUIDES.get(
        platform,
        "Инструкция для этой платформы будет добавлена в ближайшее время.",
    )
    await send_with_photo(bot, callback, text, guide_back_kb())
