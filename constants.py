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

DEVICE_CONFIG = {
    2: {
        "name": "2 устройства",
        "badge": "Базовый",
        "description": "Смартфон + Ноутбук",
        "multiplier": 1.0,
        "prices": {
            1: (89, 30),     # 89 ₽ / мес
            3: (316, 120),   # 79 ₽ / мес (+1 мес в подарок)
            6: (552, 240),   # 69 ₽ / мес (+2 мес в подарок)
            12: (885, 450),  # 59 ₽ / мес (+3 мес в подарок)
        },
        "monthly_display": {1: 89, 3: 79, 6: 69, 12: 59},
    },
    5: {
        "name": "5 устройств",
        "badge": "Оптимальный",
        "description": "Для всей семьи или нескольких гаджетов",
        "multiplier": 1.4,
        "prices": {
            1: (129, 30),    # ~129 ₽ / мес
            3: (449, 120),   # ~112 ₽ / мес (+1 мес в подарок)
            6: (779, 240),   # ~97 ₽ / мес (+2 мес в подарок)
            12: (1249, 450), # ~83 ₽ / мес (+3 мес в подарок)
        },
        "monthly_display": {1: 129, 3: 112, 6: 97, 12: 83},
    },
    10: {
        "name": "10 устройств",
        "badge": "Семейный",
        "description": "Максимальный доступ для всех устройств и друзей",
        "multiplier": 2.0,
        "prices": {
            1: (179, 30),    # ~179 ₽ / мес
            3: (639, 120),   # ~160 ₽ / мес (+1 мес в подарок)
            6: (1099, 240),  # ~137 ₽ / мес (+2 мес в подарок)
            12: (1769, 450), # ~118 ₽ / мес (+3 мес в подарок)
        },
        "monthly_display": {1: 179, 3: 160, 6: 137, 12: 118},
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

# ============================================================================
# Utility Functions
# ============================================================================

def get_price_for_months(months: int, devices: int = DEFAULT_DEVICE_LIMIT) -> tuple[int, int]:
    """
    Get price and total days for a given subscription period and device tier.

    Args:
        months: Number of months (1, 3, 6, or 12)
        devices: Number of devices (2, 5, or 10)

    Returns:
        Tuple of (price_in_rub, total_days)
    """
    dev = validate_device_limit(devices)
    tier_prices = DEVICE_CONFIG[dev]["prices"]
    return tier_prices.get(months, tier_prices[1])


def get_monthly_display(months: int, devices: int = DEFAULT_DEVICE_LIMIT) -> int:
    """Get per-month display price for tariff buttons."""
    dev = validate_device_limit(devices)
    return DEVICE_CONFIG[dev]["monthly_display"].get(months, 89)


def get_period_label(months: int) -> str:
    """Get display label for a subscription period."""
    return PERIOD_LABELS.get(months, f"{months} мес.")


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
