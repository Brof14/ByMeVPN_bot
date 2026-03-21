"""All inline keyboards for ByMeVPN bot."""
from urllib.parse import quote_plus
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

_SUPPORT_URL = (
    "https://t.me/ByMeVPN_support?text="
    + quote_plus("Привет, у меня вопрос по ByMeVPN.")
)

# ---------------------------------------------------------------------------
# Main menus
# ---------------------------------------------------------------------------

def main_menu_new_user() -> InlineKeyboardMarkup:
    """Menu for brand-new users (trial not used, no keys ever)."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Попробовать бесплатно 3 дня", callback_data="trial"))
    kb.row(InlineKeyboardButton(text="Купить от 33 ₽ в месяц", callback_data="buy_vpn"))
    kb.row(InlineKeyboardButton(text="Мои ключи", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Партнёрская программа", callback_data="partner"))
    kb.row(
        InlineKeyboardButton(text="О сервисе", callback_data="about"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL),
    )
    return kb.as_markup()


def main_menu_existing() -> InlineKeyboardMarkup:
    """Menu for users who already used trial or had any key."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Продление подписки", callback_data="buy_vpn"))
    kb.row(InlineKeyboardButton(text="Мои ключи доступа", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Партнёрская программа", callback_data="partner"))
    kb.row(
        InlineKeyboardButton(text="О сервисе", callback_data="about"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL),
    )
    return kb.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Назад в меню", callback_data="back_to_menu"))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Buy flow
# ---------------------------------------------------------------------------

def plan_type_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="1 устройство — 79 ₽/мес", callback_data="type_personal"))
    kb.row(InlineKeyboardButton(text="2 устройства — 99 ₽/мес", callback_data="type_duo"))
    kb.row(InlineKeyboardButton(text="5 устройств — 129 ₽/мес", callback_data="type_family"))
    kb.row(InlineKeyboardButton(text="Назад", callback_data="back_to_menu"))
    return kb.as_markup()


def period_kb_1d() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="1 месяц — 79 ₽", callback_data="period_1"))
    kb.row(InlineKeyboardButton(text="6 мес. + 2 мес.🎁 — 62 ₽/мес", callback_data="period_6"))
    kb.row(InlineKeyboardButton(text="1 год + 3 мес.🎁 — 47 ₽/мес", callback_data="period_12"))
    kb.row(InlineKeyboardButton(text="2 года + 6 мес.🎁 — 33 ₽/мес", callback_data="period_24"))
    kb.row(InlineKeyboardButton(text="Назад", callback_data="buy_vpn"))
    return kb.as_markup()


def period_kb_2d() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="1 месяц — 99 ₽", callback_data="period_1"))
    kb.row(InlineKeyboardButton(text="6 мес. + 2 мес.🎁 — 62 ₽/мес", callback_data="period_6"))
    kb.row(InlineKeyboardButton(text="1 год + 3 мес.🎁 — 47 ₽/мес", callback_data="period_12"))
    kb.row(InlineKeyboardButton(text="2 года + 6 мес.🎁 — 33 ₽/мес", callback_data="period_24"))
    kb.row(InlineKeyboardButton(text="Назад", callback_data="buy_vpn"))
    return kb.as_markup()


def period_kb_5d() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="1 месяц — 129 ₽", callback_data="period_1"))
    kb.row(InlineKeyboardButton(text="6 мес. + 2 мес.🎁 — 62 ₽/мес", callback_data="period_6"))
    kb.row(InlineKeyboardButton(text="1 год + 3 мес.🎁 — 47 ₽/мес", callback_data="period_12"))
    kb.row(InlineKeyboardButton(text="2 года + 6 мес.🎁 — 33 ₽/мес", callback_data="period_24"))
    kb.row(InlineKeyboardButton(text="Назад", callback_data="buy_vpn"))
    return kb.as_markup()


def payment_kb(price_rub: int, days: int) -> InlineKeyboardMarkup:
    """Stars = rubles (1:1, intentional — covers Telegram commission)."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text=f"⭐ Telegram Stars ({price_rub} звёзд)",
        callback_data="pay_stars",
    ))
    kb.row(InlineKeyboardButton(
        text=f"💳 ЮKassa {price_rub} ₽",
        callback_data="pay_yookassa",
    ))
    kb.row(InlineKeyboardButton(text="Назад", callback_data="back_to_menu"))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Config name prompt
# ---------------------------------------------------------------------------

def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Отмена", callback_data="back_to_menu"))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# My keys
# ---------------------------------------------------------------------------

def my_keys_kb(keys: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for k in keys:
        kid = k["id"]
        remark = k.get("remark") or f"Ключ #{kid}"
        kb.row(InlineKeyboardButton(
            text=f"🔑 {remark}",
            callback_data=f"key_info:{kid}",
        ))
        kb.row(
            InlineKeyboardButton(text="🔄 Продлить", callback_data=f"key_renew:{kid}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"key_delete:{kid}"),
        )
    kb.row(InlineKeyboardButton(text="📱 Инструкция подключения", callback_data="connection_guide"))
    kb.row(InlineKeyboardButton(text="Назад", callback_data="back_to_menu"))
    return kb.as_markup()


def confirm_delete_kb(key_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"key_delete_confirm:{key_id}"),
        InlineKeyboardButton(text="Отмена", callback_data="my_keys"),
    )
    return kb.as_markup()


# ---------------------------------------------------------------------------
# After key delivery
# ---------------------------------------------------------------------------

def after_key_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📱 Инструкция подключения", callback_data="connection_guide"))
    kb.row(InlineKeyboardButton(text="Назад в меню", callback_data="back_to_menu"))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Partner / referral
# ---------------------------------------------------------------------------

def partner_kb(link: str) -> InlineKeyboardMarkup:
    share_text = quote_plus(
        f"Присоединяйся к ByMeVPN! Отличный VPN-сервис от 33 ₽ в месяц.\n"
        f"Моя реферальная ссылка: {link}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="Поделиться ссылкой",
        url=f"https://t.me/share/url?url={quote_plus(link)}&text={share_text}",
    ))
    kb.row(InlineKeyboardButton(text="Назад", callback_data="back_to_menu"))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Connection guide
# ---------------------------------------------------------------------------

def connection_guide_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for name, cb in [
        ("🍎 iOS", "guide_ios"),
        ("📱 Android", "guide_android"),
        ("💻 Windows", "guide_windows"),
        ("🍏 macOS", "guide_macos"),
        ("🐧 Linux", "guide_linux"),
    ]:
        kb.row(InlineKeyboardButton(text=name, callback_data=cb))
    kb.row(InlineKeyboardButton(text="Назад", callback_data="back_to_menu"))
    return kb.as_markup()


def guide_back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Назад к платформам", callback_data="connection_guide"))
    kb.row(InlineKeyboardButton(text="В главное меню", callback_data="back_to_menu"))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Legal
# ---------------------------------------------------------------------------

def legal_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="Договор публичной оферты",
        url="https://telegra.ph/DOGOVOR-PUBLICHNOJ-OFERTY-ByMyVPN-03-12",
    ))
    kb.row(InlineKeyboardButton(
        text="Политика конфиденциальности",
        url="https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-ByMeVPN-03-12",
    ))
    kb.row(InlineKeyboardButton(
        text="Соглашение о регулярных платежах",
        url="https://telegra.ph/SOGLASHENIE-O-REGULYARNYH-REKURRENTNYH-PLATEZHAH-ByMeVPN-03-12",
    ))
    kb.row(
        InlineKeyboardButton(text="Назад", callback_data="back_to_menu"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL),
    )
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def admin_main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    kb.row(InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_broadcast"))
    return kb.as_markup()


def admin_back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_menu"))
    return kb.as_markup()
