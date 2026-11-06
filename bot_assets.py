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

# Only landmark screens carry large artwork. Secondary operations stay text-first.
# Files for other sections remain bundled for rollback and future use.
_LANDMARK_ARTWORK = frozenset({MENU, ABOUT, KEY})


def display_artwork(photo: str | None) -> str | None:
    """Return an allowed banner, or None for an ordinary customer screen."""
    return photo if photo in _LANDMARK_ARTWORK else None
