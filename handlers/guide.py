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
        "🍏 <b>iOS (iPhone / iPad) — Инструкция подключения</b>\n\n"
        "<b>Вариант 1: HAPP Proxy (Рекомендуемый)</b>\n"
        "1. Установите: <a href='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973'>HAPP Proxy Utility Plus</a> "
        "(или <a href='https://apps.apple.com/us/app/happ-proxy-utility/id6504287215'>US версия</a>)\n"
        "2. Скопируйте ссылку на подписку из раздела «Мои ключи»\n"
        "3. В HAPP нажмите кнопку <b>«Вставить из буфера»</b> в левом нижнем углу\n"
        "4. Включите переключатель по центру для подключения.\n\n"
        "<b>Вариант 2: Streisand (Альтернативный)</b>\n"
        "1. Установите: <a href='https://apps.apple.com/app/streisand/id6450534064'>Streisand в App Store</a>\n"
        "2. Скопируйте ссылку на подписку\n"
        "3. В Streisand нажмите <b>«+»</b> вверху справа → <b>«Import from Clipboard»</b>\n"
        "4. Выберите нужную локацию (Нидерланды / Германия) и нажмите Подключить."
    ),
    "android": (
        "🤖 <b>Android — Инструкция подключения</b>\n\n"
        "<b>1. Скачайте приложение HAPP Proxy</b>\n"
        "Google Play → <a href='https://play.google.com/store/apps/details?id=com.happproxy'>HAPP Proxy</a>\n"
        "(или альтернативно: <a href='https://play.google.com/store/apps/details?id=com.v2ray.ang'>v2rayNG</a>)\n\n"
        "<b>2. Скопируйте ссылку на подписку</b>\n"
        "В боте перейдите в «Мои ключи» и нажмите на ссылку\n\n"
        "<b>3. Добавьте в приложение</b>\n"
        "Откройте HAPP и нажмите <b>«Вставить из буфера»</b> (или в v2rayNG: «+» → «Импорт из буфера обмена»)\n\n"
        "<b>4. Подключитесь</b>\n"
        "Нажмите большую круглую кнопку подключения в центре экрана. Готово!"
    ),
    "windows": (
        "💻 <b>Windows — Инструкция подключения</b>\n\n"
        "<b>1. Скачайте Hiddify</b>\n"
        "<a href='https://github.com/hiddify/hiddify-next/releases/latest'>Hiddify Releases</a>\n"
        "→ скачайте файл <b>windows-setup-x64.exe</b>\n\n"
        "<b>2. Установите и запустите</b>\n"
        "Пройдите стандартную установку программы\n\n"
        "<b>3. Добавьте подписку</b>\n"
        "Скопируйте ссылку в боте → в Hiddify нажмите <b>«+ New Profile»</b> → <b>«Add from Clipboard»</b>\n\n"
        "<b>4. Подключитесь</b>\n"
        "Нажмите большую кнопку <b>Connect</b> в центре."
    ),
    "macos": (
        "🍎 <b>macOS — Инструкция подключения</b>\n\n"
        "<b>1. Скачайте приложение</b>\n"
        "Вариант А: <a href='https://apps.apple.com/ru/app/happ-proxy-utility-plus/id6746188973'>HAPP в Mac App Store</a> (для чипов Apple Silicon M1/M2/M3/M4)\n"
        "Вариант Б: <a href='https://github.com/hiddify/hiddify-next/releases/latest'>Hiddify Next (.dmg)</a>\n\n"
        "<b>2. Установите</b>\n"
        "Перетащите приложение в папку Applications\n\n"
        "<b>3. Добавьте подписку</b>\n"
        "Скопируйте ссылку подписки в боте → добавьте через кнопку «+» / «Из буфера»\n\n"
        "<b>4. Подключитесь</b>\n"
        "Нажмите Подключить."
    ),
    "linux": (
        "🐧 <b>Linux — Инструкция подключения</b>\n\n"
        "<b>Вариант 1: Hiddify Next (Графический интерфейс)</b>\n"
        "• <b>Ubuntu / Debian / Mint:</b>\n"
        "  Скачайте <code>.deb</code> с <a href='https://github.com/hiddify/hiddify-next/releases/latest'>GitHub Hiddify</a>\n"
        "  <code>sudo dpkg -i hiddify-linux-x64.deb</code>\n"
        "• <b>Arch Linux / Manjaro:</b>\n"
        "  <code>yay -S hiddify-next-bin</code>\n"
        "• <b>Fedora / RHEL:</b>\n"
        "  <code>sudo dnf install ./hiddify-linux-x64.rpm</code>\n"
        "• <b>Универсальный AppImage:</b>\n"
        "  <code>chmod +x Hiddify-Linux-x64.AppImage && ./Hiddify-Linux-x64.AppImage</code>\n\n"
        "<b>Вариант 2: Nekoray / sing-box CLI</b>\n"
        "1. Скопируйте ссылку на подписку из бота\n"
        "2. Вставьте в программу через «Preferences» → «Subscription Groups»\n"
        "3. Обновите подписку и включите режим VPN (System Proxy / TUN mode)."
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
