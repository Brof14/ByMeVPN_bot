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
# Pricing Configuration
# Format: {months: (price_rub, total_days)}
# Includes bonus days for longer subscriptions:
#   1  мес.            → 30  дней
#   3  мес. + 1 мес 🎁 → 120 дней (4 мес)
#   6  мес. + 2 мес 🎁 → 240 дней (8 мес)
#   12 мес. + 3 мес 🎁 → 450 дней (15 мес)
# ============================================================================
PRICE_CONFIG = {
    1:  (69,  30),   # 1 мес.            — 69 ₽
    3:  (236, 120),  # 3 мес. + 1 мес 🎁 — 59 ₽/мес (итого 236 ₽)
    6:  (392, 240),  # 6 мес. + 2 мес 🎁 — 49 ₽/мес (итого 392 ₽)
    12: (585, 450),  # 12 мес. + 3 мес 🎁 — 39 ₽/мес (итого 585 ₽)
}

# Monthly display price (per-month cost, shown in tariff buttons)
MONTHLY_PRICE_DISPLAY = {
    1:  69,
    3:  59,
    6:  49,
    12: 39,
}

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
# Device Limits
# ============================================================================
VALID_DEVICE_LIMITS = (1, 2, 5)  # Allowed device count limits


# ============================================================================
# Utility Functions
# ============================================================================

def get_price_for_months(months: int) -> tuple[int, int]:
    """
    Get price and total days for a given subscription period.

    Args:
        months: Number of months (1, 3, 6, or 12)

    Returns:
        Tuple of (price_in_rub, total_days)
        Defaults to (69, 30) for invalid month values
    """
    return PRICE_CONFIG.get(months, (69, 30))


def get_period_label(months: int) -> str:
    """
    Get display label for a subscription period.

    Args:
        months: Number of months

    Returns:
        String label (e.g., "1 мес.", "3 мес.", "1 год")
        Defaults to "{months} мес." for unknown values
    """
    return PERIOD_LABELS.get(months, f"{months} мес.")


def validate_device_limit(limit: int) -> int:
    """
    Validate and normalize device limit.

    Args:
        limit: Requested device limit

    Returns:
        Validated device limit (1, 2, or 5)
        Defaults to 1 for invalid values
    """
    return limit if limit in VALID_DEVICE_LIMITS else 1


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
