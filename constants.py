"""
Constants and General Settings for ByMeVPN Bot

This module contains all constant values, pricing configuration,
and utility functions for formatting and validation.
"""

from datetime import datetime

# ============================================================================
# Time Constants
# ============================================================================
TRIAL_DAYS = 3  # Number of free trial days
SECONDS_PER_DAY = 86400  # Seconds in a day
CAPTION_LIMIT = 1024  # Telegram caption character limit

# ============================================================================
# URLs and Links
# ============================================================================
LOGO_URL = "https://i.ibb.co/rG9F5PCS/logo.jpg"
SUPPORT_URL_TEMPLATE = "https://t.me/ByMeVPN_support_bot?text={}"

# ============================================================================
# Pricing Configuration
# Format: {months: (price_rub, total_days)}
# Includes bonus days for longer subscriptions
# ============================================================================
PRICE_CONFIG = {
    1:  (99, 30),    # 1 month - 99 rub, 30 days
    3:  (316, 120),  # 3 months + 1 month bonus - 316 rub (79 rub/mo), 120 days
    6:  (552, 240),  # 6 months + 2 months bonus - 552 rub (69 rub/mo), 240 days
    12: (885, 450),  # 1 year + 3 months bonus - 885 rub (59 rub/mo), 450 days
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
        Defaults to (149, 30) for invalid month values
    """
    return PRICE_CONFIG.get(months, (149, 30))


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
