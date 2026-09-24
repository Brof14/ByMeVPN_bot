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
        "<b>Вариант 1: HAPP Proxy (Рекомендуемый)</b>\n"
        "1. Установите: <a href='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973'>HAPP Proxy Utility Plus</a> "
        "(или <a href='https://apps.apple.com/us/app/happ-proxy-utility/id6504287215'>US версия</a>)\n"
        "2. Скопируйте ссылку на подписку из раздела «Мои ключи»\n"
        "3. В HAPP нажмите кнопку <b>«Вставить из буфера»</b> в левом нижнем углу\n"
        "4. Включите переключатель по центру для подключения.\n\n"
        "<b>Вариант 2: V2Box</b>\n"
        "1. Установите <a href='https://apps.apple.com/us/app/v2box-v2ray-client/id6446814690'>V2Box из App Store</a>.\n"
        "2. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку.\n"
        "3. Откройте V2Box.\n"
        "4. Нажмите <b>«+»</b> / импорт из буфера обмена.\n"
        "5. Вставьте ссылку.\n"
        "6. Выберите сервер и нажмите <b>Connect</b>."
    ),
    "android": (
        "🤖 <b>Android — Инструкция подключения</b>\n\n"
        "1. Установите <a href='https://play.google.com/store/apps/details?id=com.happproxy'>HAPP Proxy из Google Play</a> "
        "(или альтернативно: <a href='https://play.google.com/store/apps/details?id=com.v2ray.ang'>v2rayNG</a>).\n"
        "2. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку на подписку.\n"
        "3. Откройте приложение и нажмите <b>«Вставить из буфера»</b> (в v2rayNG: <b>«+»</b> → «Импорт из буфера обмена»).\n"
        "4. Нажмите круглую кнопку подключения в центре экрана. Готово!"
    ),
    "windows": (
        "💻 <b>Windows — Инструкция подключения</b>\n\n"
        "1. Скачайте приложение: <a href='https://github.com/hiddify/hiddify-app/releases/latest'>Hiddify для Windows</a> (файл <code>Hiddify-Windows-Setup-x64.exe</code>).\n"
        "2. Запустите файл и завершите установку программы.\n"
        "3. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку на подписку.\n"
        "4. В Hiddify нажмите <b>«+ New Profile»</b> → <b>«Add from Clipboard»</b>.\n"
        "5. Нажмите большую кнопку <b>Connect</b> по центру для подключения."
    ),
    "macos": (
        "🍎 <b>macOS — Инструкция подключения</b>\n\n"
        "1. Скачайте приложение:\n"
        "• Для Mac с процессорами Apple Silicon (M1/M2/M3/M4): <a href='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973'>HAPP в Mac App Store</a>\n"
        "• Универсальная версия: <a href='https://github.com/hiddify/hiddify-app/releases/latest'>Hiddify Next (.dmg)</a>\n"
        "2. Запустите файл и перетащите приложение в Программы.\n"
        "3. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку.\n"
        "4. В приложении нажмите <b>«Вставить из буфера»</b> (или <b>«+»</b>).\n"
        "5. Нажмите <b>Connect</b> (или включите переключатель) для подключения."
    ),
}

_LINUX_GUIDES: dict[str, str] = {
    "arch": (
        "🐧 <b>Arch / EndeavourOS / Manjaro</b>\n\n"
        "1. Откройте терминал.\n\n"
        "2. Установите nftables:\n"
        "<code>sudo pacman -S nftables</code>\n\n"
        "3. Скачайте последнюю версию v2rayN:\n"
        "<a href='https://github.com/2dust/v2rayN/releases'>GitHub → Releases</a> → Linux → <code>v2rayN-linux-64.zip</code>\n\n"
        "4. Перейдите в папку со скачанным архивом:\n"
        "<code>cd ~/Downloads</code>\n\n"
        "5. Распакуйте архив:\n"
        "<code>unzip \"название-файла.zip\"</code>\n\n"
        "6. Перейдите в распакованную папку:\n"
        "<code>cd \"папка-v2rayN\"</code>\n\n"
        "7. Запустите v2rayN:\n"
        "<code>chmod +x v2rayN\n"
        "./v2rayN</code>\n\n"
        "8. Скопируйте ссылку нужного сервера из «Мои ключи».\n\n"
        "9. Добавьте конфигурацию в v2rayN.\n\n"
        "10. Внизу включите:\n"
        "• <b>System Proxy</b>\n"
        "• <b>Global</b>\n\n"
        "11. Подключитесь."
    ),
    "ubuntu": (
        "🐧 <b>Ubuntu / Debian / Linux Mint</b>\n\n"
        "1. Скачайте v2rayN для Linux (.deb) с официального <a href='https://github.com/2dust/v2rayN/releases'>GitHub → Releases</a> (файл <code>v2rayN-linux-64.deb</code>).\n\n"
        "2. Откройте терминал в папке со скачанным файлом.\n\n"
        "3. Выполните:\n"
        "<code>sudo apt install -y ./v2rayN-linux-64.deb</code>\n\n"
        "4. Откройте v2rayN через меню приложений.\n\n"
        "5. В ByMeVPN откройте «Мои ключи» и скопируйте ссылку.\n\n"
        "6. Добавьте её в v2rayN.\n\n"
        "7. Включите <b>System Proxy</b> и <b>Global</b>.\n\n"
        "8. Подключитесь."
    ),
    "fedora": (
        "🐧 <b>Fedora / RHEL</b>\n\n"
        "1. Скачайте v2rayN для Linux (.rpm) с официального <a href='https://github.com/2dust/v2rayN/releases'>GitHub → Releases</a> (файл <code>v2rayN-linux-rhel-64.rpm</code>).\n\n"
        "2. Откройте терминал в папке со скачанным файлом.\n\n"
        "3. Выполните:\n"
        "<code>sudo dnf install -y ./v2rayN-linux-rhel-64.rpm</code>\n\n"
        "4. Откройте v2rayN.\n\n"
        "5. Скопируйте ссылку из «Мои ключи».\n\n"
        "6. Добавьте конфигурацию.\n\n"
        "7. Включите <b>System Proxy</b> и <b>Global</b>.\n\n"
        "8. Подключитесь."
    ),
    "other": (
        "🐧 <b>Другой Linux</b>\n\n"
        "1. Скачайте <a href='https://github.com/2dust/v2rayN/releases'>v2rayN-linux-64.zip</a> с официального GitHub.\n\n"
        "2. Распакуйте архив.\n\n"
        "3. Откройте терминал в распакованной папке.\n\n"
        "4. Выполните:\n"
        "<code>chmod +x v2rayN\n"
        "./v2rayN</code>\n\n"
        "5. Скопируйте ссылку ByMeVPN из «Мои ключи».\n\n"
        "6. Добавьте конфигурацию.\n\n"
        "7. Включите <b>System Proxy</b> и <b>Global</b>.\n\n"
        "8. Подключитесь."
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
