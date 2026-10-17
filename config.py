"""
Configuration Module for ByMeVPN Bot

This module loads all configuration from environment variables using python-dotenv.
All sensitive credentials should be stored in the .env file (not committed to git).

Environment Variables (.env file):
    BOT_TOKEN: Telegram bot token from BotFather
    Support contact is fixed to @ByMeVPNSupportBot.
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
    YOOKASSA_WEBHOOK_ALLOWED_IPS: Comma-separated YooKassa webhook IP ranges
"""

import os
import logging as _logging
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ============================================================================
# Telegram Bot Configuration
# ============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
SUPPORT_USERNAME = "@ByMeVPNSupportBot"
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
# TLS certificate verification is enabled by default. Set XUI_TLS_VERIFY=false
# only for a deliberately self-signed/private panel certificate.
XUI_TLS_VERIFY = os.getenv("XUI_TLS_VERIFY", "true").strip().lower() not in {"0", "false", "no", "off"}

# A common deployment mistake is reaching an HTTPS panel through localhost.
# The panel then redirects to TLS with a certificate issued for its public
# hostname, which cannot validate against 127.0.0.1/localhost.
_api_parsed = urlparse(XUI_API_URL)
_public_parsed = urlparse(XUI_URL)
if (
    XUI_TLS_VERIFY
    and _api_parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    and _public_parsed.scheme == "https"
    and _public_parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
):
    _logging.getLogger(__name__).warning(
        "XUI_API_URL points to localhost while XUI_URL uses HTTPS. If the local "
        "endpoint redirects to HTTPS, use the panel hostname in XUI_API_URL so "
        "TLS certificate verification matches; do not disable verification."
    )

# Принудительно ограничиваем выдаваемые подписки этими четырьмя инбаундами.
# Значение из .env намеренно игнорируется, чтобы состав подписки не менялся случайно.
XUI_INBOUND_IDS: list[int] = [12, 13, 14, 15]
XUI_INBOUND_ID = XUI_INBOUND_IDS[0]  # primary (обратная совместимость)
# Current panel remarks, verified against the live inbounds on 2026-10-17.
# The panel remark is preferred at runtime; these labels are a safe fallback.
XUI_INBOUND_NAMES = {
    12: "🇩🇪Germany-1",
    13: "🇩🇪Germany-2",
    14: "🇩🇪Germany-3",
    15: "🇩🇪Germany-4",
}
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
# YooKassa documents these source ranges for incoming HTTP notifications.
# Keep this overrideable so the allowlist can be updated without code changes.
YOOKASSA_WEBHOOK_ALLOWED_IPS = os.getenv(
    "YOOKASSA_WEBHOOK_ALLOWED_IPS",
    "185.71.76.0/27,185.71.77.0/27,77.75.153.0/25,77.75.156.11,"
    "77.75.156.35,77.75.154.128/25,2a02:5180::/32",
)
WEBHOOK_URL = (
    os.getenv("WEBHOOK_URL") or "https://bymevpn.duckdns.org:8443/webhook/telegram"
)

# ============================================================================
# Trial and Referral Configuration
# ============================================================================
TRIAL_DAYS = 3
TRIAL_PRICE = 0

# ============================================================================
# Pricing Configuration
# Aligned with constants.py single source of truth:
#   1  мес.            → 89 ₽ (89 ₽/мес)
#   3  мес. + 1 мес 🎁 → 237 ₽ (79 ₽/мес)
#   6  мес. + 2 мес 🎁 → 414 ₽ (69 ₽/мес)
#   12 мес. + 3 мес 🎁 → 708 ₽ (59 ₽/мес)
# ============================================================================
from constants import get_total_price, TARIFF_DAYS

PRICE_1_MONTH = get_total_price(1, 2)    # 89 ₽
PRICE_3_MONTHS = get_total_price(3, 2)   # 237 ₽ (79 * 3)
PRICE_6_MONTHS = get_total_price(6, 2)   # 414 ₽ (69 * 6)
PRICE_12_MONTHS = get_total_price(12, 2) # 708 ₽ (59 * 12)

DAYS_1M  = TARIFF_DAYS[1]   # 30
DAYS_3M  = TARIFF_DAYS[3]   # 120
DAYS_6M  = TARIFF_DAYS[6]   # 240
DAYS_12M = TARIFF_DAYS[12]  # 450

# ============================================================================
# Logging
# ============================================================================
_logging.getLogger(__name__).info(
    "3x-ui config → api_url=%s username=%s inbounds=%s",
    XUI_API_URL,
    XUI_USERNAME,
    XUI_INBOUND_IDS,
)
