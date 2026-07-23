from urllib.parse import quote_plus
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from constants import PRICE_CONFIG, PERIOD_LABELS, MONTHLY_PRICE_DISPLAY

_SUPPORT_URL = (
    "https://t.me/ByMeVPNSupportBot?text="
    + quote_plus("Привет, у меня вопрос по ByMeVPN.")
)


def main_menu_new_user() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Попробовать БЕСПЛАТНО 3 дня", callback_data="trial", style="success"))
    kb.row(InlineKeyboardButton(text="Купить от 39 ₽ в месяц", callback_data="buy_vpn", style="primary"))
    kb.row(InlineKeyboardButton(text="Я уже клиент ByMeVPN", callback_data="auth_existing_client"))
    kb.row(InlineKeyboardButton(text="Партнёрская программа", callback_data="partner"))
    kb.row(
        InlineKeyboardButton(text="О сервисе", callback_data="about"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL),
    )
    return kb.as_markup()


def main_menu_existing() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Купить от 39 ₽ в месяц", callback_data="buy_vpn", style="primary"))
    kb.row(InlineKeyboardButton(text="Я уже клиент ByMeVPN", callback_data="auth_existing_client"))
    kb.row(InlineKeyboardButton(text="Партнёрская программа", callback_data="partner"))
    kb.row(
        InlineKeyboardButton(text="О сервисе", callback_data="about"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL),
    )
    return kb.as_markup()


def main_menu_with_keys(trial_used: bool = False) -> InlineKeyboardMarkup:
    """Main menu for users who have at least one key (trial or paid).
    
    Args:
        trial_used: If True, hide the trial button (user already used trial).
    """
    kb = InlineKeyboardBuilder()
    # Show trial button only if user hasn't used trial yet
    if not trial_used:
        kb.row(InlineKeyboardButton(text="Попробовать БЕСПЛАТНО 3 дня", callback_data="trial", style="success"))
    kb.row(InlineKeyboardButton(text="Купить от 39 ₽ в месяц", callback_data="buy_vpn", style="primary"))
    kb.row(InlineKeyboardButton(text="Мои ключи", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Войти в другой аккаунт", callback_data="auth_existing_client"))
    kb.row(InlineKeyboardButton(text="Партнёрская программа", callback_data="partner"))
    kb.row(
        InlineKeyboardButton(text="О сервисе", callback_data="about"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL),
    )
    return kb.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="Назад в меню", callback_data="back_to_menu"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL)
    )
    return kb.as_markup()


def authorized_user_menu() -> InlineKeyboardMarkup:
    """Menu for authorized existing clients (4 buttons in strict order)."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Продление подписки", callback_data="buy_vpn"))
    kb.row(InlineKeyboardButton(text="Мои ключи", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Партнёрская программа", callback_data="partner"))
    kb.row(InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL))
    return kb.as_markup()


def tariff_selection_kb(discount_percent: int = 0) -> InlineKeyboardMarkup:
    """Tariff selection with bonuses. Shows per-month price.
    
    Тарифная сетка (без скидки):
      1 мес.            — 69 ₽
      3 мес. + 1 мес 🎁 — 59 ₽ / мес
      6 мес. + 2 мес 🎁 — 49 ₽ / мес
      12 мес. + 3 мес 🎁 — 39 ₽ / мес
    """
    kb = InlineKeyboardBuilder()

    # (months, bonus_label)
    tariff_rows = [
        (1,  ""),
        (3,  " + 1 мес. 🎁"),
        (6,  " + 2 мес. 🎁"),
        (12, " + 3 мес. 🎁"),
    ]

    for months, bonus in tariff_rows:
        total_price = PRICE_CONFIG[months][0]
        monthly_price = MONTHLY_PRICE_DISPLAY[months]

        if discount_percent > 0:
            discounted_total = int(total_price * (100 - discount_percent) / 100)
            discounted_monthly = int(monthly_price * (100 - discount_percent) / 100)
            if bonus:
                text = (
                    f"{months} мес.{bonus} — "
                    f"{discounted_monthly} ₽ / мес (-{discount_percent}%)"
                )
            else:
                text = f"1 мес. — {discounted_total} ₽ (-{discount_percent}%)"
        else:
            if bonus:
                text = f"{months} мес.{bonus} — {monthly_price} ₽ / мес"
            else:
                text = f"1 мес. — {total_price} ₽"

        kb.row(InlineKeyboardButton(
            text=text,
            callback_data=f"tariff_{months}",
        ))

    kb.row(
        InlineKeyboardButton(text="Назад", callback_data="back_to_menu"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL),
    )
    return kb.as_markup()


def payment_kb(price_rub: int, days: int, yookassa_url: str = "") -> InlineKeyboardMarkup:
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
    kb.row(InlineKeyboardButton(
        text=f"₿CryptoBot {price_rub} ₽",
        callback_data="pay_crypto",
    ))
    kb.row(InlineKeyboardButton(text="Назад", callback_data="back_to_menu"))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Config name prompt
# ---------------------------------------------------------------------------

def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="Отмена", callback_data="back_to_menu"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL)
    )
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
            text=f"{remark}",
            callback_data=f"key_info:{kid}",
        ))
        kb.row(
            InlineKeyboardButton(text="Продлить", callback_data=f"key_renew:{kid}"),
            InlineKeyboardButton(text="Удалить", callback_data=f"key_delete:{kid}"),
        )
    kb.row(InlineKeyboardButton(text="Инструкция подключения", callback_data="connection_guide"))
    kb.row(InlineKeyboardButton(text="Назад", callback_data="back_to_menu"))
    return kb.as_markup()



# My keys - list view (keys only + Back/Support)
def my_keys_list_kb(keys: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for k in keys:
        kid = k["id"]
        remark = k.get("remark") or f"#{kid}"
        kb.row(InlineKeyboardButton(
            text=f"Key {remark}",
            callback_data=f"key_info:{kid}",
        ))
    kb.row(
        InlineKeyboardButton(text="Back", callback_data="back_to_menu"),
        InlineKeyboardButton(text="Support", url=_SUPPORT_URL)
    )
    return kb.as_markup()


# Key detail view (Renew/Delete/Instruction/Back/Support)
def key_detail_kb(key_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🔄 Продлить", callback_data=f"key_renew:{key_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"key_delete:{key_id}"),
    )
    kb.row(InlineKeyboardButton(text="📋 Инструкция подключения", callback_data="connection_guide"))
    kb.row(
        InlineKeyboardButton(text="◀️ Назад", callback_data="my_keys"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="back_to_menu")
    )
    return kb.as_markup()
def confirm_delete_kb(key_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="Да, удалить", callback_data=f"key_delete_confirm:{key_id}"),
        InlineKeyboardButton(text="Отмена", callback_data="my_keys"),
    )
    return kb.as_markup()


# ---------------------------------------------------------------------------
# After key delivery
# ---------------------------------------------------------------------------

def after_key_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Инструкция подключения", callback_data="connection_guide"))
    kb.row(
        InlineKeyboardButton(text="Назад в меню", callback_data="back_to_menu"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL)
    )
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Partner / referral
# ---------------------------------------------------------------------------

def partner_kb(link: str) -> InlineKeyboardMarkup:
    share_text = quote_plus(
        "Если у тебя не работает YouTube / Telegram — вот решение.\n\n"
        "Сам пользуюсь — реально норм VPN.\n\n"
        "🎁 3 дня бесплатно (без карты)\n"
        "📱 До 5 устройств\n"
        "⚡ Всё открывается без лагов\n\n"
        "💰 От 39 ₽/мес\n\n"
        f"Попробуй:\n{link}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="Поделиться ссылкой",
        url=f"https://t.me/share/url?url={quote_plus(link)}&text={share_text}",
    ))
    kb.row(
        InlineKeyboardButton(text="Назад", callback_data="back_to_menu"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL)
    )
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Connection guide
# ---------------------------------------------------------------------------

def connection_guide_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for name, cb in [
        ("iOS", "guide_ios"),
        ("Android", "guide_android"),
        ("Windows", "guide_windows"),
        ("macOS", "guide_macos"),
        ("Linux", "guide_linux"),
    ]:
        kb.row(InlineKeyboardButton(text=name, callback_data=cb))
    kb.row(
        InlineKeyboardButton(text="Назад", callback_data="back_to_menu"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL)
    )
    return kb.as_markup()


def guide_back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Назад к платформам", callback_data="connection_guide"))
    kb.row(
        InlineKeyboardButton(text="В главное меню", callback_data="back_to_menu"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL)
    )
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Legal
# ---------------------------------------------------------------------------

def legal_kb() -> InlineKeyboardMarkup:
    """Legal information keyboard - matches screenshot exactly."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Договор публичной оферты", url="https://telegra.ph/DOGOVOR-PUBLICHNOJ-OFERTY-ByMyVPN-03-12"))
    kb.row(InlineKeyboardButton(text="Политика конфиденциальности", url="https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-ByMeVPN-03-12"))
    kb.row(InlineKeyboardButton(text="Соглашение о регулярных платежах", url="https://telegra.ph/SOGLASHENIE-O-REGULYARNYH-REKURRENTNYH-PLATEZHAH-ByMeVPN-03-12"))
    kb.row(
        InlineKeyboardButton(text="Назад", callback_data="back_to_menu"),
        InlineKeyboardButton(text="Поддержка", url=_SUPPORT_URL)
    )
    return kb.as_markup()
