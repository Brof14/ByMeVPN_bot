from urllib.parse import quote_plus
import time
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot_style import themed_button as InlineKeyboardButton

from constants import (
    VALID_DEVICE_LIMITS, DEFAULT_DEVICE_LIMIT,
    get_monthly_display, get_price_for_months, validate_device_limit,
)

_SUPPORT_URL = (
    "https://t.me/ByMeVPNSupportBot?text="
    + quote_plus("Привет, у меня вопрос по ByMeVPN.")
)
_REISSUE_SUPPORT_URL = (
    "https://t.me/ByMeVPNSupportBot?text="
    + quote_plus("Здравствуйте! Хочу перевыпустить VPN-ключ. Подскажите безопасный порядок.")
)


def _customer_menu(*, has_subscription: bool, trial_used: bool) -> InlineKeyboardMarkup:
    """Single source of truth for every customer home menu."""
    kb = InlineKeyboardBuilder()
    if has_subscription:
        kb.row(InlineKeyboardButton(text="Подписка", callback_data="my_keys"))
    elif not trial_used:
        kb.row(InlineKeyboardButton(text="3 дня бесплатно", callback_data="trial_free"))
    kb.row(InlineKeyboardButton(text="Тарифы", callback_data="buy_vpn", style=None if has_subscription else "primary"))
    kb.row(InlineKeyboardButton(text="Бесплатные дни", callback_data="free_vpn"))
    kb.row(InlineKeyboardButton(text="Поддержка", callback_data="trouble"))
    return kb.as_markup()


def main_menu_new_user() -> InlineKeyboardMarkup:
    return _customer_menu(has_subscription=False, trial_used=False)


def main_menu_existing(trial_used: bool = False) -> InlineKeyboardMarkup:
    return _customer_menu(has_subscription=False, trial_used=trial_used)


def main_menu_with_keys(trial_used: bool = False) -> InlineKeyboardMarkup:
    return _customer_menu(has_subscription=True, trial_used=trial_used)


def main_menu_active(key_id: int) -> InlineKeyboardMarkup:
    """Give a one-key subscriber an immediate connection path, without checkout."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Подписка", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Тарифы", callback_data="buy_vpn"))
    kb.row(InlineKeyboardButton(text="Бесплатные дни", callback_data="free_vpn"))
    kb.row(InlineKeyboardButton(text="Поддержка", callback_data="trouble"))
    return kb.as_markup()


def main_menu_expired(*, key_ids: list[int]) -> InlineKeyboardMarkup:
    """When access has ended, lead with renewal instead of an unused trial.

    A single key can renew directly while users with several keys first
    choose which subscription to extend. This does not touch pricing logic.
    """
    if len(key_ids) == 1:
        renew_action = f"key_renew:{key_ids[0]}"
    elif key_ids:
        renew_action = "my_keys"
    else:
        renew_action = "buy_vpn"

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Продлить VPN", callback_data=renew_action))
    kb.row(InlineKeyboardButton(text="Бесплатные дни", callback_data="free_vpn"))
    kb.row(InlineKeyboardButton(text="Поддержка", callback_data="trouble"))
    return kb.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="← Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()


def support_url() -> str:
    return _SUPPORT_URL


def feedback_url() -> str:
    """Direct users to support with a clear bug-report / suggestion prompt."""
    return (
        "https://t.me/ByMeVPNSupportBot?text="
        + quote_plus("Здравствуйте! Хочу сообщить об ошибке или предложить улучшение ByMeVPN.\n")
    )


def authorized_user_menu() -> InlineKeyboardMarkup:
    """Compact navigation for signed-in app users."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Подписка", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Тарифы", callback_data="buy_vpn"))
    kb.row(InlineKeyboardButton(text="Бесплатные дни", callback_data="free_vpn"))
    kb.row(InlineKeyboardButton(text="Поддержка", callback_data="trouble"))
    return kb.as_markup()


def tariff_selection_kb(
    devices: int = DEFAULT_DEVICE_LIMIT, discount_percent: int = 0,
    promo_tariff_months: int | None = None,
) -> InlineKeyboardMarkup:
    """Tariff selection with device tiers and actual subscription durations."""
    devices = validate_device_limit(devices)
    kb = InlineKeyboardBuilder()

    # Device selector row: 2, 5, 10 devices
    dev_btns = []
    for d in VALID_DEVICE_LIMITS:
        label = f"{d} устр."
        if d == devices:
            label = f"✓ {d} устр."
        dev_btns.append(InlineKeyboardButton(
            text=label, callback_data=f"devtier_{d}",
        ))
    kb.row(*dev_btns)

    # No invisible totals or misleading free months: the existing billing
    # calculation charges for all 1/4/8/15 included months. Show the actual
    # payable amount and effective monthly rate without changing a kopeck.
    for months in (1, 3, 6, 12):
        total_price, included_days = get_price_for_months(months, devices)
        applies = discount_percent > 0 and (promo_tariff_months is None or promo_tariff_months == months)
        if applies:
            total_price = round(total_price * (100 - discount_percent) / 100)
            months_included = included_days // 30
            monthly_display = f"≈{total_price / months_included:.2f}".rstrip("0").rstrip(".")
        else:
            monthly_display = str(get_monthly_display(months, devices))
        text = f"{included_days} дней · {total_price} ₽ · {monthly_display} ₽/30 дней"
        if applies:
            text += f" · −{discount_percent}%"
        if months == 12 and discount_percent == 0:
            text += " · Выгоднее за 30 дней"
        kb.row(InlineKeyboardButton(text=text, callback_data=f"tariff_{months}_{devices}"))

    kb.row(InlineKeyboardButton(text="← Назад", callback_data="back_to_menu"))
    return kb.as_markup()


def payment_kb(price_rub: int, days: int, devices: int = DEFAULT_DEVICE_LIMIT, yookassa_url: str = "") -> InlineKeyboardMarkup:
    """Stars = rubles (1:1, intentional — covers Telegram commission)."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text=f"Telegram Stars · {price_rub} зв.",
        callback_data="pay_stars",
    ))
    kb.row(InlineKeyboardButton(
        text=f"ЮKassa · {price_rub} ₽",
        callback_data="pay_yookassa",
    ))
    kb.row(InlineKeyboardButton(
        text=f"CryptoBot · {price_rub} ₽",
        callback_data="pay_crypto",
    ))
    kb.row(InlineKeyboardButton(text="← Назад", callback_data=f"devtier_{devices}"))
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
    now = int(time.time())
    for k in keys:
        kid = k["id"]
        remark = k.get("remark") or f"Ключ #{kid}"
        status = "✅" if k.get("expiry", 0) > now else "⌛"
        kb.row(InlineKeyboardButton(
            text=f"{status} {remark}"[:64],
            callback_data=f"key_info:{kid}",
        ))
    kb.row(InlineKeyboardButton(text="Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()



# One button per key keeps the list easy to scan.
def my_keys_list_kb(keys: list, has_history: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    now = int(time.time())
    for k in keys:
        kid = k["id"]
        remark = k.get("remark") or f"#{kid}"
        status = "✅" if k.get("expiry", 0) > now else "⌛"
        kb.row(InlineKeyboardButton(
            text=f"{status} {remark}"[:64],
            callback_data=f"key_info:{kid}",
        ))
    if has_history:
        kb.row(InlineKeyboardButton(text="История подписок", callback_data="key_history"))
    kb.row(InlineKeyboardButton(text="Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()


def key_history_kb(keys: list) -> InlineKeyboardMarkup:
    """List completed subscriptions separately from the current VPN key."""
    kb = InlineKeyboardBuilder()
    for key in sorted(keys, key=lambda item: (item.get("expiry", 0), item.get("id", 0)), reverse=True):
        remark = key.get("remark") or f"#{key['id']}"
        kb.row(InlineKeyboardButton(
            text=f"⌛ {remark} · до {time.strftime('%d.%m.%Y', time.localtime(key.get('expiry', 0)))}"[:64],
            callback_data=f"key_info:{key['id']}",
        ))
    kb.row(InlineKeyboardButton(text="← К подписке", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()


# Key detail: keep the two most common actions visible, group secondary ones.
def key_detail_kb(
    key_id: int,
    has_autorenew: bool = False,
    autorenew_active: bool = False,
    subscription_url: str = "",
    has_history: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="Подключить", callback_data=f"key_instructions:{key_id}"),
        InlineKeyboardButton(text="Продлить", callback_data=f"key_renew:{key_id}"),
    )
    # Manual import remains available within the connection instructions.
    if has_history:
        kb.row(InlineKeyboardButton(text="История подписок", callback_data="key_history"))
    kb.row(InlineKeyboardButton(text="Устройства", callback_data=f"key_devices:{key_id}"))
    kb.row(InlineKeyboardButton(text="Управление", callback_data=f"key_settings:{key_id}"))
    kb.row(InlineKeyboardButton(text="← Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()


def key_devices_kb(key_id: int, devices: list[dict] | None = None, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    devices = devices or []
    page_size = 10
    page = max(0, min(int(page), max(0, (len(devices) - 1) // page_size)))
    start = page * page_size
    for device in devices[start:start + page_size]:
        label = device.get("model") or device.get("user_agent") or device.get("os") or "Устройство"
        label = label[:34]
        kb.row(InlineKeyboardButton(
            text=f"{label} · {device.get('last_seen_label', '—')}"[:64],
            callback_data=f"key_device:{key_id}:{device['id']}",
        ))
    pages = max(1, (len(devices) + page_size - 1) // page_size)
    if pages > 1:
        row = []
        if page > 0:
            row.append(InlineKeyboardButton(text="←", callback_data=f"key_devices:{key_id}:{page - 1}"))
        row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"key_devices:{key_id}:{page}"))
        if page + 1 < pages:
            row.append(InlineKeyboardButton(text="→", callback_data=f"key_devices:{key_id}:{page + 1}"))
        kb.row(*row)
    # Each visit to this screen loads fresh device data; no refresh button needed.
    kb.row(InlineKeyboardButton(text="← К подписке", callback_data=f"key_info:{key_id}"))
    return kb.as_markup()


def key_device_detail_kb(key_id: int, device_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="Удалить устройство", callback_data=f"key_device_delete_prompt:{key_id}:{device_id}", style="danger",
    ))
    kb.row(InlineKeyboardButton(text="← К устройствам", callback_data=f"key_devices:{key_id}"))
    return kb.as_markup()


def confirm_delete_device_kb(key_id: int, device_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="Да, удалить устройство", callback_data=f"key_device_delete:{key_id}:{device_id}", style="danger",
    ))
    kb.row(InlineKeyboardButton(text="Отмена", callback_data=f"key_device:{key_id}:{device_id}"))
    return kb.as_markup()


def key_settings_kb(key_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="Запросить перевыпуск",
        url=_REISSUE_SUPPORT_URL,
    ))
    kb.row(InlineKeyboardButton(text="Оплата и автопродление", callback_data=f"manage_payment_methods:{key_id}"))
    # Deliberately do not expose destructive subscription deletion in the customer UI.
    kb.row(InlineKeyboardButton(text="← К подписке", callback_data=f"key_info:{key_id}"))
    return kb.as_markup()


def autorenew_cancel_confirm_kb(key_id: int) -> InlineKeyboardMarkup:
    """Confirm disabling auto-renew. Cancelling NEVER revokes paid access."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Отключить автопродление", callback_data=f"autorenew_unbind_confirm:{key_id}", style="danger"))
    kb.row(InlineKeyboardButton(text="← Назад", callback_data=f"manage_payment_methods:{key_id}"))
    return kb.as_markup()


def manage_payment_methods_kb(
    card_title: str = "",
    is_checked: bool = True,
    key_id: int = 0,
    has_card: bool = False,
    autorenew_active: bool = False,
) -> InlineKeyboardMarkup:
    """Payment-methods screen. Only shows a card when one is really saved."""
    kb = InlineKeyboardBuilder()
    if has_card:
        if not autorenew_active:
            kb.row(InlineKeyboardButton(text="Включить автопродление", callback_data=f"autorenew_enable:{key_id}"))
        kb.row(InlineKeyboardButton(
            text="Отключить автопродление",
            callback_data=f"autorenew_unbind_prompt:{key_id}",
            style="danger",
        ))
    else:
        renew_callback = f"key_renew:{key_id}" if key_id else "buy_vpn"
        kb.row(InlineKeyboardButton(text="Продлить вручную", callback_data=renew_callback))
    kb.row(InlineKeyboardButton(text="← К подписке" if key_id else "🏠 Главное меню", callback_data=f"key_info:{key_id}" if key_id else "back_to_menu"))
    return kb.as_markup()


def confirm_delete_kb(key_id: int) -> InlineKeyboardMarkup:
    """Make cancelling billing plainly distinct from deleting VPN access."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="Удалить доступ сейчас",
        callback_data=f"key_delete_confirm:{key_id}",
        style="danger",
    ))
    kb.row(InlineKeyboardButton(
        text="Только отключить автопродление",
        callback_data=f"manage_payment_methods:{key_id}",
    ))
    kb.row(InlineKeyboardButton(text="← Назад", callback_data=f"key_info:{key_id}"))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# After key delivery
# ---------------------------------------------------------------------------

def after_key_kb(subscription_url: str = "") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Подключить VPN", callback_data="connection_guide"))
    kb.row(
        InlineKeyboardButton(text="Подписка", callback_data="my_keys"),
        InlineKeyboardButton(text="Пригласить друга", callback_data="partner"),
    )
    kb.row(InlineKeyboardButton(text="← Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()


def app_open_kb(redirect_url: str, deep_link: str = "") -> InlineKeyboardMarkup:
    """One-tap handover to the mobile app, with copy as a manual fallback."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="Открыть приложение",
        url=redirect_url,
    ))
    if deep_link:
        if len(deep_link) <= 256:
            kb.row(InlineKeyboardButton(
                text="Скопировать ссылку входа",
                copy_text={"text": deep_link},
            ))
        else:
            # Telegram CopyTextButton is limited to 256 characters.
            # A long credential must not make the entire login message fail.
            kb.row(InlineKeyboardButton(
                text="Моя подписка", callback_data="my_keys",
            ))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Partner / referral
# ---------------------------------------------------------------------------

def connection_guide_kb(key_id: int | None = None, subscription_url: str = "") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    def platform_callback(name: str) -> str:
        return f"guide_{name}:{key_id}" if key_id else f"guide_{name}"

    kb.row(
        InlineKeyboardButton(text="iPhone / iPad", callback_data=platform_callback("ios")),
        InlineKeyboardButton(text="Android", callback_data=platform_callback("android")),
    )
    kb.row(
        InlineKeyboardButton(text="Windows", callback_data=platform_callback("windows")),
        InlineKeyboardButton(text="Linux", callback_data=platform_callback("linux")),
    )
    kb.row(InlineKeyboardButton(text="Другие устройства", callback_data=f"guide_more:{key_id}" if key_id else "guide_more"))
    if subscription_url:
        kb.row(InlineKeyboardButton(
            text="Скопировать ссылку",
            copy_text={"text": subscription_url},
        ))
    back_callback = f"key_info:{key_id}" if key_id else "back_to_menu"
    back_text = "← Назад" if key_id else "← Назад в меню"
    kb.row(InlineKeyboardButton(text=back_text, callback_data=back_callback))
    return kb.as_markup()


def additional_platforms_kb(key_id: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    def platform_callback(name: str) -> str:
        return f"guide_{name}:{key_id}" if key_id else f"guide_{name}"
    kb.row(InlineKeyboardButton(text="Mac", callback_data=platform_callback("macos")))
    kb.row(InlineKeyboardButton(text="Android TV", callback_data=platform_callback("android_tv")))
    back = f"connection_guide:{key_id}" if key_id else "connection_guide"
    kb.row(InlineKeyboardButton(text="← К устройствам", callback_data=back))
    return kb.as_markup()


def linux_distro_kb() -> InlineKeyboardMarkup:
    """Linux distro selection keyboard."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Ubuntu / Debian / Mint", callback_data="guide_linux_ubuntu"))
    kb.row(InlineKeyboardButton(text="Arch / EndeavourOS / Manjaro", callback_data="guide_linux_arch"))
    kb.row(InlineKeyboardButton(text="Fedora / RHEL", callback_data="guide_linux_fedora"))
    kb.row(InlineKeyboardButton(text="Другой Linux", callback_data="guide_linux_other"))
    kb.row(InlineKeyboardButton(text="← Назад", callback_data="connection_guide"))
    return kb.as_markup()


def linux_guide_back_kb() -> InlineKeyboardMarkup:
    """Back button from specific Linux distro guide to Linux distro selection."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="◀️ К выбору Linux", callback_data="guide_linux"))
    kb.row(InlineKeyboardButton(text="Поддержка", callback_data="trouble"))
    kb.row(InlineKeyboardButton(text="← Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()


def guide_back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="◀️ Назад к платформам", callback_data="connection_guide"))
    kb.row(InlineKeyboardButton(text="Поддержка", callback_data="trouble"))
    kb.row(InlineKeyboardButton(text="← Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()


# ---------------------------------------------------------------------------
# Legal
# ---------------------------------------------------------------------------

def legal_kb() -> InlineKeyboardMarkup:
    """Legal information keyboard - matches screenshot exactly."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Договор публичной оферты", url="https://telegra.ph/Publichnaya-oferta-na-okazanie-uslug-ByMeVPN-10-25"))
    kb.row(InlineKeyboardButton(text="Политика конфиденциальности", url="https://telegra.ph/Politika-v-otnoshenii-obrabotki-personalnyh-dannyh-ByMeVPN-10-25"))
    kb.row(InlineKeyboardButton(text="Соглашение об автопродлении", url="https://telegra.ph/Soglashenie-ob-avtomaticheskom-prodlenii-podpiski-ByMeVPN-10-25"))
    kb.row(InlineKeyboardButton(text="Оплата и автопродление", callback_data="manage_payment_methods"))
    kb.row(InlineKeyboardButton(text="← Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()
