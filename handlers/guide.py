"""Connection guide for all platforms — with logo photo and updated links."""
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from keyboards import connection_guide_kb, guide_back_kb
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
        "🍎 <b>iOS — Инструкция подключения</b>\n\n"
        "<b>1. Скачайте HAPP</b>\n"
        "App Store → <a href='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973'>HAPP Proxy Utility Plus</a>\n\n"
        "<b>2. Откройте приложение</b>\n"
        "Нажмите «+» в правом верхнем углу\n\n"
        "<b>3. Импортируйте ключ</b>\n"
        "Выберите «Import from clipboard»\n"
        "Скопируйте и вставьте ваш ключ\n\n"
        "<b>4. Подключитесь</b>\n"
        "Включите переключатель — готово!\n\n"
        "✅ Вы в безопасном интернете"
    ),
    "android": (
        "📱 <b>Android — Инструкция подключения</b>\n\n"
        "<b>1. Скачайте HAPP</b>\n"
        "Google Play → <a href='https://play.google.com/store/apps/details?id=com.happproxy'>HAPP Proxy</a>\n\n"
        "<b>2. Откройте приложение</b>\n"
        "Нажмите «+» в правом верхнем углу\n\n"
        "<b>3. Импортируйте ключ</b>\n"
        "Выберите «Import from clipboard»\n"
        "Скопируйте и вставьте ваш ключ\n\n"
        "<b>4. Подключитесь</b>\n"
        "Нажмите на профиль ByMeVPN и включите VPN\n\n"
        "✅ Готово!"
    ),
    "windows": (
        "💻 <b>Windows — Инструкция подключения</b>\n\n"
        "<b>1. Скачайте Hiddify</b>\n"
        "<a href='https://github.com/hiddify/hiddify-next/releases'>github.com/hiddify/hiddify-next/releases</a>\n"
        "→ выберите файл <b>windows-setup-x64.exe</b>\n\n"
        "<b>2. Установите</b>\n"
        "Запустите установщик, следуйте инструкциям\n\n"
        "<b>3. Добавьте ключ</b>\n"
        "Нажмите «+» → «Add from clipboard»\n"
        "Вставьте ваш ключ\n\n"
        "<b>4. Подключитесь</b>\n"
        "Выберите профиль ByMeVPN → Connect\n\n"
        "✅ Готово!"
    ),
    "macos": (
        "🍏 <b>macOS — Инструкция подключения</b>\n\n"
        "<b>1. Скачайте Hiddify</b>\n"
        "<a href='https://github.com/hiddify/hiddify-next/releases'>github.com/hiddify/hiddify-next/releases</a>\n"
        "→ hiddify-macos.dmg\n\n"
        "<b>2. Установите</b>\n"
        "Откройте .dmg → перетащите в Applications\n\n"
        "<b>3. Добавьте ключ</b>\n"
        "Нажмите «+» → «Add from clipboard»\n"
        "Вставьте ваш ключ\n\n"
        "<b>4. Подключитесь</b>\n"
        "Выберите профиль ByMeVPN → Connect\n\n"
        "✅ Готово!"
    ),
    "linux": (
        "🐧 <b>Linux — Инструкция подключения</b>\n\n"
        "<b>1. Скачайте Hiddify</b>\n"
        "<a href='https://github.com/hiddify/hiddify-next/releases'>github.com/hiddify/hiddify-next/releases</a>\n"
        "→ hiddify-linux-x64.zip\n\n"
        "<b>2. Установите</b>\n"
        "<code>chmod +x hiddify && ./hiddify</code>\n\n"
        "<b>3. Добавьте ключ</b>\n"
        "Нажмите «+» → «Add from clipboard»\n"
        "Вставьте ваш ключ\n\n"
        "<b>4. Подключитесь</b>\n"
        "Выберите профиль ByMeVPN → Connect\n\n"
        "✅ Готово!"
    ),
}


@router.callback_query(F.data.startswith("guide_"))
async def cb_platform_guide(callback: CallbackQuery, bot: Bot):
    await safe_answer(callback)
    platform = callback.data.split("_", 1)[1]
    text = _GUIDES.get(
        platform,
        "Инструкция для этой платформы будет добавлена в ближайшее время.",
    )
    await send_with_photo(bot, callback, text, guide_back_kb())
