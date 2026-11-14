import time
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot_style import themed_button as InlineKeyboardButton

from constants import (
    VALID_DEVICE_LIMITS, DEFAULT_DEVICE_LIMIT,
    TARIFF_BONUS_MONTHS, get_monthly_display, get_price_for_months, validate_device_limit,
)

_SUPPORT_URL = "https://t.me/ByMeVPNSupportBot"


def _customer_menu(*, has_subscription: bool, trial_used: bool) -> InlineKeyboardMarkup:
    """Single source of truth for every customer home menu."""
    kb = InlineKeyboardBuilder()
    if has_subscription:
        kb.row(InlineKeyboardButton(text="Подписка", callback_data="my_keys"))
    elif not trial_used:
        kb.row(InlineKeyboardButton(text="3 дня бесплатно", callback_data="trial_free"))
    kb.row(InlineKeyboardButton(text="Тарифы", callback_data="buy_vpn", style="primary"))
    kb.row(InlineKeyboardButton(text="Бесплатные дни", callback_data="free_vpn", style="primary"))
    kb.row(
        InlineKeyboardButton(text="О сервисе", callback_data="about"),
        InlineKeyboardButton(text="Поддержка", url=support_url()),
    )
    return kb.as_markup()


def main_menu_new_user() -> InlineKeyboardMarkup:
    return _customer_menu(has_subscription=False, trial_used=False)


def main_menu_existing(trial_used: bool = False) -> InlineKeyboardMarkup:
    return _customer_menu(has_subscription=False, trial_used=trial_used)


def main_menu_with_keys(trial_used: bool = False) -> InlineKeyboardMarkup:
    return _customer_menu(has_subscription=True, trial_used=trial_used)


def main_menu_active(key_id: int, subscription_url: str = "", user_id: int | None = None) -> InlineKeyboardMarkup:
    """Put the active subscriber's connection action first in the home menu."""
    kb = InlineKeyboardBuilder()
    connect_url = None
    if subscription_url:
        try:
            from app_links import build_connect_page_url
            connect_url = build_connect_page_url(subscription_url, user_id=user_id, key_id=key_id)
        except (RuntimeError, ValueError):
            connect_url = None
    connect_button = (
        InlineKeyboardButton(text="Подключить VPN", url=connect_url)
        if connect_url else
        InlineKeyboardButton(text="Подключить VPN", callback_data=f"key_instructions:{key_id}")
    )
    kb.row(connect_button)
    # Device management belongs to the subscription details, not the home hub.
    kb.row(InlineKeyboardButton(text="Подписка", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Бесплатные дни", callback_data="free_vpn", style="primary"))
    kb.row(
        InlineKeyboardButton(text="О сервисе", callback_data="about"),
        InlineKeyboardButton(text="Поддержка", url=support_url()),
    )
    return kb.as_markup()


def main_menu_panel_active(subscription_url: str = "", user_id: int | None = None) -> InlineKeyboardMarkup:
    """Server-only entitlement: do not pretend a missing local key exists."""
    kb = InlineKeyboardBuilder()
    if subscription_url:
        try:
            from app_links import build_connect_page_url
            url = build_connect_page_url(subscription_url, user_id=user_id)
        except (RuntimeError, ValueError):
            url = ""
        if url:
            kb.row(InlineKeyboardButton(text="Подключить VPN", url=url))
    kb.row(InlineKeyboardButton(text="Подписка", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Бесплатные дни", callback_data="free_vpn", style="primary"))
    kb.row(
        InlineKeyboardButton(text="О сервисе", callback_data="about"),
        InlineKeyboardButton(text="Поддержка", url=support_url()),
    )
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
    kb.row(InlineKeyboardButton(text="Бесплатные дни", callback_data="free_vpn", style="primary"))
    kb.row(
        InlineKeyboardButton(text="О сервисе", callback_data="about"),
        InlineKeyboardButton(text="Поддержка", url=support_url()),
    )
    return kb.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="← Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()


def support_url() -> str:
    return _SUPPORT_URL


def feedback_url() -> str:
    """Open the support bot using a standard Telegram deep link."""
    return "https://t.me/ByMeVPNSupportBot"


def authorized_user_menu() -> InlineKeyboardMarkup:
    """Compact navigation for signed-in app users."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Подписка", callback_data="my_keys"))
    kb.row(InlineKeyboardButton(text="Тарифы", callback_data="buy_vpn"))
    kb.row(InlineKeyboardButton(text="Бесплатные дни", callback_data="free_vpn", style="primary"))
    kb.row(
        InlineKeyboardButton(text="О сервисе", callback_data="about"),
        InlineKeyboardButton(text="Поддержка", url=support_url()),
    )
    return kb.as_markup()


def tariff_selection_kb(
    devices: int = DEFAULT_DEVICE_LIMIT, discount_percent: int = 0,
    promo_tariff_months: int | None = None,
) -> InlineKeyboardMarkup:
    """Tariff selection with device tiers and actual subscription durations."""
    devices = validate_device_limit(devices)
    kb = InlineKeyboardBuilder()

    # Show the two smaller options side by side, with 10 devices below.
    for pair in ((2, 5), (10,)):
        buttons = []
        for count in pair:
            selected = "✓ " if count == devices else ""
            buttons.append(InlineKeyboardButton(
                text=f"{selected}{count} устройства" if count == 2 else f"{selected}{count} устройств",
                callback_data=f"devtier_{count}",
            ))
        kb.row(*buttons)

    # The monthly rate applies to the complete 1 / 4 / 8 / 15-month package,
    # including its advertised bonus months. Never hide the total to pay.
    for months in (1, 3, 6, 12):
        total_price, _ = get_price_for_months(months, devices)
        applies = discount_percent > 0 and (
            promo_tariff_months is None or promo_tariff_months == months
        )
        included_months = months + TARIFF_BONUS_MONTHS[months]
        if applies:
            total_price = round(total_price * (100 - discount_percent) / 100)
            monthly_display = round(total_price / included_months)
        else:
            monthly_display = get_monthly_display(months, devices)

        total_label = f"{total_price:,}".replace(",", " ")
        if months == 1:
            label = f"1 мес. — {total_label} ₽"
        else:
            label = (
                f"{months}+{TARIFF_BONUS_MONTHS[months]} мес. 🎁 — "
                f"{monthly_display} ₽/мес. · {total_label} ₽"
            )
        if applies:
            label += f" · −{discount_percent}%"
        kb.row(InlineKeyboardButton(text=label, callback_data=f"tariff_{months}_{devices}"))

    kb.row(InlineKeyboardButton(text="Есть промокод?", callback_data="promo_enter"))
    kb.row(InlineKeyboardButton(text="← Назад", callback_data="back_to_menu"))
    return kb.as_markup()


def payment_kb(
    price_rub: int, days: int, devices: int = DEFAULT_DEVICE_LIMIT,
    yookassa_url: str = "",
    autorenew_enabled: bool = True,
) -> InlineKeyboardMarkup:
    """Show provider choices and the real YooKassa recurring option."""
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
    kb.row(InlineKeyboardButton(
        text=f"Автопродление ЮKassa: {'включено' if autorenew_enabled else 'выключено'} · изменить",
        callback_data="checkout_autorenew_toggle",
    ))
    kb.row(InlineKeyboardButton(text="Есть промокод?", callback_data="promo_enter"))
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
    is_active: bool = True,
    user_id: int | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    connect_url = None
    if subscription_url:
        try:
            from app_links import build_connect_page_url
            connect_url = build_connect_page_url(subscription_url, user_id=user_id, key_id=key_id)
        except (RuntimeError, ValueError):
            connect_url = None
    connect_button = (
        InlineKeyboardButton(text="Подключить", url=connect_url)
        if connect_url else
        InlineKeyboardButton(text="Подключить", callback_data=f"key_instructions:{key_id}")
    )
    if is_active:
        kb.row(connect_button)
        kb.row(
            InlineKeyboardButton(text="Продлить", callback_data=f"key_renew:{key_id}"),
            InlineKeyboardButton(text="Устройства", callback_data=f"key_devices:{key_id}"),
        )
    else:
        # An expired entitlement must not suggest that the tunnel is usable.
        kb.row(InlineKeyboardButton(text="Продлить VPN", callback_data=f"key_renew:{key_id}"))
        kb.row(InlineKeyboardButton(text="Устройства", callback_data=f"key_devices:{key_id}"))
    kb.row(InlineKeyboardButton(text="Управление подпиской", callback_data=f"key_settings:{key_id}"))
    if has_history:
        kb.row(InlineKeyboardButton(text="История подписок", callback_data="key_history"))
    kb.row(InlineKeyboardButton(text="← Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()


def key_devices_kb(
    key_id: int, devices: list[dict] | None = None, page: int = 0,
    *, current_limit: int | None = None, active: bool = True,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    devices = devices or []
    page_size = 10
    page = max(0, min(int(page), max(0, (len(devices) - 1) // page_size)))
    start = page * page_size
    for device in devices[start:start + page_size]:
        label = device.get("model") or device.get("os") or "Устройство"
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
    if active and (current_limit is None or 0 < current_limit < 10):
        kb.row(InlineKeyboardButton(text="Увеличить лимит устройств", callback_data=f"device_upgrade:{key_id}"))
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
    kb.row(InlineKeyboardButton(text="Оплата и автопродление", callback_data=f"manage_payment_methods:{key_id}"))
    kb.row(InlineKeyboardButton(
        text="Перевыпустить подписку",
        callback_data=f"key_reissue_confirm:{key_id}",
    ))
    # Deliberately do not expose destructive subscription deletion in the customer UI.
    kb.row(InlineKeyboardButton(text="← К подписке", callback_data=f"key_info:{key_id}"))
    return kb.as_markup()


def confirm_reissue_kb(key_id: int, challenge: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="Да, перевыпустить",
        callback_data=f"key_reissue:{key_id}:{challenge}",
        style="danger",
    ))
    kb.row(InlineKeyboardButton(text="Отмена", callback_data=f"key_settings:{key_id}"))
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

def after_key_kb(subscription_url: str = "", user_id: int | None = None, key_id: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    connect_url = None
    if subscription_url:
        try:
            from app_links import build_connect_page_url
            connect_url = build_connect_page_url(subscription_url, user_id=user_id, key_id=key_id)
        except (RuntimeError, ValueError):
            connect_url = None
    kb.row(InlineKeyboardButton(
        text="Подключить VPN", url=connect_url,
    ) if connect_url else InlineKeyboardButton(text="Подключить VPN", callback_data="connection_guide"))
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
# Legal
# ---------------------------------------------------------------------------

def legal_kb() -> InlineKeyboardMarkup:
    """Legal information keyboard - matches screenshot exactly."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Договор публичной оферты", url="https://telegra.ph/Publichnaya-oferta-na-okazanie-uslug-ByMeVPN-10-25"))
    kb.row(InlineKeyboardButton(text="Политика конфиденциальности", url="https://telegra.ph/Politika-v-otnoshenii-obrabotki-personalnyh-dannyh-ByMeVPN-10-25"))
    kb.row(InlineKeyboardButton(text="Соглашение об автопродлении", url="https://telegra.ph/Soglashenie-ob-avtomaticheskom-prodlenii-podpiski-ByMeVPN-10-25"))
    kb.row(InlineKeyboardButton(text="Управлять автопродлением", callback_data="manage_payment_methods"))
    kb.row(InlineKeyboardButton(text="← Главное меню", callback_data="back_to_menu"))
    return kb.as_markup()
