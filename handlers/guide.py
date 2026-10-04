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
        "и следуйте простой инструкции.\n\n"
        "Если что-то не получится — внизу есть «Не получается?»."
    )
    await send_with_photo(bot, callback, text, connection_guide_kb())


# Как понять, что VPN работает + куда идти, если нет (дописывается к каждой инструкции)
_GUIDE_CHECK = (
    "\n\n✅ <b>Как понять, что всё работает:</b> нажмите кнопку подключения — "
    "появится статус «подключено». Откройте любой сайт: если загружается, готово.\n\n"
    "Не получилось? Нажмите «Не получается?» ниже."
)


@router.callback_query(F.data == "trouble")
async def cb_trouble(callback: CallbackQuery, bot: Bot):
    """Self-service troubleshooting: concrete ordered steps, honest, no promises."""
    await safe_answer(callback)
    text = (
        "⚠️ <b>Не получается подключить?</b>\n\n"
        "Пройдите по порядку — чаще всего помогает первый же шаг:\n\n"
        "1. Выключите другие VPN и прокси (включая системные) и переподключитесь.\n\n"
        "2. Обновите подписку: «Мои ключи» → скопируйте ссылку заново → в приложении «Вставить из буфера».\n\n"
        "3. Проверьте, что в приложении появилась именно подписка ByMeVPN.\n\n"
        "4. VPN подключён, но сайты не открываются? Переподключитесь или выберите другой сервер в приложении.\n\n"
        "5. Не помогло — напишите в поддержку, разберёмся."
    )
    from aiogram.types import InlineKeyboardBuilder, InlineKeyboardButton
    from keyboards import _SUPPORT_URL
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📋 Открыть инструкцию", callback_data="connection_guide"))
    kb.row(InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Написать в поддержку", url=_SUPPORT_URL))
    kb.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu"))
    await send_with_photo(bot, callback, text, kb.as_markup())


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
    await send_with_photo(bot, callback, text + _GUIDE_CHECK, linux_guide_back_kb())


@router.callback_query(F.data.in_({"guide_ios", "guide_android", "guide_windows", "guide_macos"}))
async def cb_platform_guide(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    platform = callback.data.split("_", 1)[1]
    text = _GUIDES.get(
        platform,
        "Инструкция для этой платформы будет добавлена в ближайшее время.",
    )
    await send_with_photo(bot, callback, text + _GUIDE_CHECK, guide_back_kb())
