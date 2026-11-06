"""Artwork retained for the active main menu and key delivery."""

from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "assets" / "sections"


def _asset(name: str) -> str:
    return str(_ASSETS / f"{name}.png")


MENU = _asset("menu")
ABOUT = _asset("about")
KEY = _asset("key")
