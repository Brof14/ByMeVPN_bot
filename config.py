"""
Configuration Module for ByMeVPN Bot

This module loads all configuration from environment variables using python-dotenv.
All sensitive credentials should be stored in the .env file (not committed to git).

Environment Variables (.env file):
    BOT_TOKEN: Telegram bot token from BotFather
    SUPPORT_USERNAME: Support bot username (default: @ByMeVPN_support_bot)
    MENU_PHOTO: URL or file ID for menu photo
    DB_FILE: Database file path (default: data/vpnbot.db)
    ADMIN_IDS: Comma-separated list of admin Telegram user IDs
    XUI_URL: 3x-ui panel PUBLIC URL (shown to users in subscription links)
    XUI_API_URL: 3x-ui panel URL the BOT uses for API calls (use 127.0.0.1 if same server)
    XUI_USERNAME: 3x-ui admin username
    XUI_PASSWORD: 3x-ui admin password
    XUI_INBOUND_IDS: Comma-separated inbound IDs (e.g., "2,3")
    XUI_SUB_PATH: path for subscriptions (default: sub)
    YOOKASSA_SHOP_ID: YooKassa payment shop ID
    YOOKASSA_SECRET_KEY: YooKassa payment secret key
    SMTP_HOST: SMTP server host for email auth
    SMTP_PORT: SMTP server port (default: 465)
    SMTP_USER: SMTP username
    SMTP_PASSWORD: SMTP password
    WEBHOOK_HOST: Webhook server host (default: 0.0.0.0)
    WEBHOOK_PORT: Webhook server port (default: 8080)
    REF_BONUS_DAYS: Referral bonus days (default: 3)
"""

import os
import logging as _logging

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ============================================================================
# Telegram Bot Configuration
# ============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@ByMeVPN_support_bot")
MENU_PHOTO = os.getenv("MENU_PHOTO", "")
# DB_FILE is NOT defined independently here anymore — database.py is the single
# source of truth (it reads the same .env var). Re-exporting it avoids the old
# bug where config.DB_FILE and database.DB_FILE pointed at two different files.
from database import DB_FILE  # noqa: E402

# Admin configuration - supports multiple admins via comma-separated list
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "0")).split(",")
    if x.strip()
]
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else 0  # Legacy single admin support

# ============================================================================
# 3x-ui Panel Configuration (VPN Management via py3xui HTTP API)
# ============================================================================
XUI_URL = os.getenv("XUI_URL", "")

# XUI_API_URL — the URL the bot uses for panel API calls.
# If not set, falls back to XUI_URL (public URL).
# For same-server deployments, set to: http://127.0.0.1:<PORT>/<SECRET_PATH>
XUI_API_URL = os.getenv("XUI_API_URL", "")

# Validate: XUI_API_URL must look like a URL.
if XUI_API_URL and not XUI_API_URL.startswith(("http://", "https://")):
    _logging.getLogger(__name__).warning(
        "XUI_API_URL value %r is not a valid URL — ignoring. "
        "Check your .env: XUI_API_URL must start with http:// or https://. "
        "Falling back to XUI_URL for API calls.",
        XUI_API_URL,
    )
    XUI_API_URL = ""
if not XUI_API_URL:
    XUI_API_URL = XUI_URL

XUI_USERNAME = os.getenv("XUI_USERNAME", "")
XUI_PASSWORD = os.getenv("XUI_PASSWORD", "")

# Список инбаундов через запятую: XUI_INBOUND_IDS=2,3 (VLESS)
_xui_ids_raw = os.getenv("XUI_INBOUND_IDS", os.getenv("XUI_INBOUND_ID", "2,3"))
XUI_INBOUND_IDS: list[int] = [
    int(x.strip()) for x in _xui_ids_raw.split(",") if x.strip()
]
XUI_INBOUND_ID = XUI_INBOUND_IDS[0]  # primary (обратная совместимость)
XUI_SUB_PATH = os.getenv("XUI_SUB_PATH", "sub")

# ============================================================================
# YooKassa Payment Configuration
# ============================================================================
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")

# ============================================================================
# Crypto Bot Payment Configuration (@send)
# ============================================================================
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "")

# ============================================================================
# Email Authentication Configuration
# ============================================================================
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# ============================================================================
# Webhook Server Configuration
# ============================================================================
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST") or "0.0.0.0"
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT") or "8080")
WEBHOOK_URL = (
    os.getenv("WEBHOOK_URL") or "https://bymevpn.duckdns.org:8443/webhook/telegram"
)

# ============================================================================
# Trial and Referral Configuration
# ============================================================================
TRIAL_DAYS = 3
TRIAL_PRICE = 0
REF_BONUS_DAYS = int(os.getenv("REF_BONUS_DAYS", "3"))

# ============================================================================
# Pricing Configuration
# Updated prices (aligned with constants.py PRICE_CONFIG):
#   1  мес.            → 69 ₽
#   3  мес. + 1 мес 🎁 → 236 ₽ (59 ₽/мес)
#   6  мес. + 2 мес 🎁 → 392 ₽ (49 ₽/мес)
#   12 мес. + 3 мес 🎁 → 585 ₽ (39 ₽/мес)
# ============================================================================
PRICE_1_MONTH = 69
PRICE_3_MONTHS = 236
PRICE_6_MONTHS = 392
PRICE_12_MONTHS = 585

DAYS_1M  = 30
DAYS_3M  = 120
DAYS_6M  = 240
DAYS_12M = 450

# ============================================================================
# Logging
# ============================================================================
_logging.getLogger(__name__).info(
    "3x-ui config → api_url=%s username=%s inbounds=%s",
    XUI_API_URL,
    XUI_USERNAME,
    XUI_INBOUND_IDS,
)
