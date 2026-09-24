"""
Constants and General Settings for ByMeVPN Bot

This module contains all constant values, pricing configuration,
and utility functions for formatting and validation.
"""

from datetime import datetime

# ============================================================================
# Time Constants
# ============================================================================
TRIAL_DAYS = 3          # Number of free trial days
SECONDS_PER_DAY = 86400  # Seconds in a day
CAPTION_LIMIT = 1024    # Telegram caption character limit

# Days a referrer gets when their referral makes a first paid purchase.
# NOTE: this is the value actually used by subscription.py / handlers/keys.py.
# config.REF_BONUS_DAYS (read from .env) is a *different*, unrelated value used
# only for the "+N days for referral click" bonus — they are intentionally
# separate programs, do not merge them.
REF_BONUS_DAYS = 15

# ============================================================================
# URLs and Links
# ============================================================================
LOGO_URL = "https://i.ibb.co/rG9F5PCS/logo.jpg"
SUPPORT_URL_TEMPLATE = "https://t.me/ByMeVPN_support_bot?text={}"

# ============================================================================
# Device Limits & Configuration (Single Source of Truth)
# ============================================================================
DEFAULT_DEVICE_LIMIT = 2
VALID_DEVICE_LIMITS = (2, 5, 10)

# Base monthly prices for 2 devices
BASE_MONTHLY_PRICES = {
    1: 89,
    3: 79,
    6: 69,
    12: 59,
}

# Fixed device price extra per month relative to 2 devices (no multipliers, no percentages)
DEVICE_PRICE_EXTRA = {
    2: 0,
    5: 20,
    10: 40,
}

# Days included per subscription period
TARIFF_DAYS = {
    1: 30,    # 1 мес.
    3: 120,   # 3 мес. + 1 мес. 🎁
    6: 240,   # 6 мес. + 2 мес. 🎁
    12: 450,  # 12 мес. + 3 мес. 🎁
}


def validate_device_limit(limit: int) -> int:
    """
    Validate and normalize device limit.

    Args:
        limit: Requested device limit

    Returns:
        Validated device limit (2, 5, or 10).
        Defaults to DEFAULT_DEVICE_LIMIT (2) for invalid values.
    """
    try:
        limit = int(limit)
    except (ValueError, TypeError):
        return DEFAULT_DEVICE_LIMIT
    return limit if limit in VALID_DEVICE_LIMITS else DEFAULT_DEVICE_LIMIT


def get_monthly_price(months: int, devices: int = DEFAULT_DEVICE_LIMIT) -> int:
    """
    Per-month display price for tariff selection.
    Formula: BASE_MONTHLY_PRICES[months] + DEVICE_PRICE_EXTRA[devices]
    """
    dev = validate_device_limit(devices)
    m = months if months in BASE_MONTHLY_PRICES else 1
    return BASE_MONTHLY_PRICES[m] + DEVICE_PRICE_EXTRA[dev]


def get_monthly_display(months: int, devices: int = DEFAULT_DEVICE_LIMIT) -> int:
    """Alias for get_monthly_price for tariff buttons."""
    return get_monthly_price(months, devices)


def get_total_price(months: int, devices: int = DEFAULT_DEVICE_LIMIT) -> int:
    """
    Actual purchase price to be charged at payment.
    Formula: get_monthly_price(months, devices) * months
    """
    m = months if months in BASE_MONTHLY_PRICES else 1
    return get_monthly_price(m, devices) * m


def get_tariff_days(months: int) -> int:
    """Get total days granted for given subscription period."""
    return TARIFF_DAYS.get(months, TARIFF_DAYS[1])


def get_price_for_months(months: int, devices: int = DEFAULT_DEVICE_LIMIT) -> tuple[int, int]:
    """
    Get actual total price and total days for a given subscription period and device tier.

    Args:
        months: Number of months (1, 3, 6, or 12)
        devices: Number of devices (2, 5, or 10)

    Returns:
        Tuple of (actual_total_price_in_rub, total_days)
    """
    m = months if months in BASE_MONTHLY_PRICES else 1
    dev = validate_device_limit(devices)
    return get_total_price(m, dev), get_tariff_days(m)


DEVICE_CONFIG = {
    2: {
        "name": "2 устройства",
        "badge": "Базовый",
        "description": "Смартфон + Ноутбук",
        "prices": {
            1: (get_total_price(1, 2), TARIFF_DAYS[1]),    # (89, 30)
            3: (get_total_price(3, 2), TARIFF_DAYS[3]),    # (237, 120)
            6: (get_total_price(6, 2), TARIFF_DAYS[6]),    # (414, 240)
            12: (get_total_price(12, 2), TARIFF_DAYS[12]), # (708, 450)
        },
        "monthly_display": {
            1: get_monthly_price(1, 2),   # 89
            3: get_monthly_price(3, 2),   # 79
            6: get_monthly_price(6, 2),   # 69
            12: get_monthly_price(12, 2), # 59
        },
    },
    5: {
        "name": "5 устройств",
        "badge": "Оптимальный",
        "description": "Для всей семьи или нескольких гаджетов",
        "prices": {
            1: (get_total_price(1, 5), TARIFF_DAYS[1]),    # (109, 30)
            3: (get_total_price(3, 5), TARIFF_DAYS[3]),    # (297, 120)
            6: (get_total_price(6, 5), TARIFF_DAYS[6]),    # (534, 240)
            12: (get_total_price(12, 5), TARIFF_DAYS[12]), # (948, 450)
        },
        "monthly_display": {
            1: get_monthly_price(1, 5),   # 109
            3: get_monthly_price(3, 5),   # 99
            6: get_monthly_price(6, 5),   # 89
            12: get_monthly_price(12, 5), # 79
        },
    },
    10: {
        "name": "10 устройств",
        "badge": "Семейный",
        "description": "Максимальный доступ для всех устройств и друзей",
        "prices": {
            1: (get_total_price(1, 10), TARIFF_DAYS[1]),    # (129, 30)
            3: (get_total_price(3, 10), TARIFF_DAYS[3]),    # (357, 120)
            6: (get_total_price(6, 10), TARIFF_DAYS[6]),    # (654, 240)
            12: (get_total_price(12, 10), TARIFF_DAYS[12]), # (1188, 450)
        },
        "monthly_display": {
            1: get_monthly_price(1, 10),   # 129
            3: get_monthly_price(3, 10),   # 119
            6: get_monthly_price(6, 10),   # 109
            12: get_monthly_price(12, 10), # 99
        },
    },
}

# Backward compatibility defaults (mapped to 2 devices base tier)
PRICE_CONFIG = DEVICE_CONFIG[2]["prices"]
MONTHLY_PRICE_DISPLAY = DEVICE_CONFIG[2]["monthly_display"]

# ============================================================================
# Period Labels (for display in UI)
# ============================================================================
PERIOD_LABELS = {
    1:  "1 мес.",
    3:  "3 мес.",
    6:  "6 мес.",
    12: "1 год",
}


def get_period_label(months: int) -> str:
    """Get display label for a subscription period."""
    return PERIOD_LABELS.get(months, f"{months} мес.")


def format_timestamp(ts: int) -> str:
    """
    Format Unix timestamp to readable date string.

    Args:
        ts: Unix timestamp

    Returns:
        Date string in format "DD.MM.YYYY"
        Returns "—" on error
    """
    try:
        return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
    except Exception:
        return "—"


def format_days_left(expiry: int) -> str:
    """
    Format remaining time until key expiry.

    Args:
        expiry: Unix timestamp of expiry date

    Returns:
        String like "X дн." or "X ч." or "истёк"
    """
    import time
    left = expiry - int(time.time())
    if left <= 0:
        return "истёк"
    d = left // SECONDS_PER_DAY
    if d > 0:
        return f"{d} дн."
    h = left // 3600
    return f"{h} ч."
