"""Local section artwork for ByMeVPN Telegram screens."""

from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "assets" / "sections"


def _asset(name: str) -> str:
    return str(_ASSETS / f"{name.lower()}.png")

MENU = _asset("MENU")
SUPPORT = _asset("SUPPORT")
ABOUT = _asset("ABOUT")
FREE_VPN = _asset("FREE_VPN")
REFERRALS = _asset("REFERRALS")
MY_INVITATIONS = _asset("MY_INVITATIONS")
AD_PROGRAM = _asset("AD_PROGRAM")
PLANS = _asset("PLANS")
KEY = _asset("KEY")
DEVICES = _asset("DEVICES")
CONNECTION = _asset("CONNECTION")
ANDROID = _asset("ANDROID")
IPHONE = _asset("IPHONE")
WINDOWS = _asset("WINDOWS")
LINUX = _asset("LINUX")
RENEWAL = _asset("RENEWAL")
PAYMENT = _asset("PAYMENT")
PROMO_CODE = _asset("PROMO_CODE")
LEGAL = _asset("LEGAL")
AUTO_RENEWAL = _asset("AUTO_RENEWAL")
