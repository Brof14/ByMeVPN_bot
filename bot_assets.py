"""Local artwork for the ByMeVPN Telegram bot's customer sections."""

from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "assets" / "sections"


def _asset(name: str) -> str:
    return str(_ASSETS / f"{name}.png")


MENU = _asset("menu")
SUPPORT = _asset("support")
ABOUT = _asset("about")
FREE_VPN = _asset("free_vpn")
REFERRALS = _asset("referrals")
MY_INVITATIONS = _asset("my_invitations")
AD_PROGRAM = _asset("ad_program")
PLANS = _asset("plans")
KEY = _asset("key")
DEVICES = _asset("devices")
CONNECTION = _asset("connection")
ANDROID = _asset("android")
IPHONE = _asset("iphone")
WINDOWS = _asset("windows")
LINUX = _asset("linux")
RENEWAL = _asset("renewal")
PAYMENT = _asset("payment")
PROMO_CODE = _asset("promo_code")
LEGAL = _asset("legal")
AUTO_RENEWAL = _asset("auto_renewal")

# Keep the matching illustration on every customer-facing section. Repeated
# navigation reuses Telegram's cached photo ID instead of uploading each time.
_SECTION_ARTWORK = frozenset({
    MENU, SUPPORT, ABOUT, FREE_VPN, REFERRALS, MY_INVITATIONS, AD_PROGRAM,
    PLANS, KEY, DEVICES, CONNECTION, ANDROID, IPHONE, WINDOWS, LINUX,
    RENEWAL, PAYMENT, PROMO_CODE, LEGAL, AUTO_RENEWAL,
})


def display_artwork(photo: str | None) -> str | None:
    """Return only a bundled ByMeVPN section image; reject arbitrary paths."""
    return photo if photo in _SECTION_ARTWORK else None
