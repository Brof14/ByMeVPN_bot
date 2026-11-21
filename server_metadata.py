"""Conservative server location parsing from explicit human-readable node labels.

Do not infer physical hosting locations from two-letter substrings, and do
not invent a city when the operator supplied only the country.
"""
from __future__ import annotations

import re

# code, Russian name, English name, flag, unambiguous full-name aliases
COUNTRIES = (
    ("de", "Германия", "Germany", "🇩🇪", ("germany", "deutschland", "германия", "германии")),
    ("nl", "Нидерланды", "Netherlands", "🇳🇱", ("netherlands", "holland", "нидерланды", "нидерландов")),
    ("us", "США", "United States", "🇺🇸", ("usa", "united states", "сша", "america")),
    ("fi", "Финляндия", "Finland", "🇫🇮", ("finland", "финляндия", "финляндии")),
    ("se", "Швеция", "Sweden", "🇸🇪", ("sweden", "швеция", "швеции")),
    ("gb", "Великобритания", "United Kingdom", "🇬🇧", ("united kingdom", "great britain", "britain", "англия", "великобритания")),
    ("tr", "Турция", "Turkey", "🇹🇷", ("turkey", "türkiye", "турция", "турции")),
    ("fr", "Франция", "France", "🇫🇷", ("france", "франция", "франции")),
    ("pl", "Польша", "Poland", "🇵🇱", ("poland", "польша", "польши")),
    ("ru", "Россия", "Russia", "🇷🇺", ("russia", "россия", "россии")),
    ("ca", "Канада", "Canada", "🇨🇦", ("canada", "канада", "канады")),
    ("jp", "Япония", "Japan", "🇯🇵", ("japan", "япония", "японии")),
    ("sg", "Сингапур", "Singapore", "🇸🇬", ("singapore", "сингапур")),
    ("ch", "Швейцария", "Switzerland", "🇨🇭", ("switzerland", "швейцария", "швейцарии")),
    ("at", "Австрия", "Austria", "🇦🇹", ("austria", "австрия", "австрии")),
    ("es", "Испания", "Spain", "🇪🇸", ("spain", "испания", "испании")),
    ("it", "Италия", "Italy", "🇮🇹", ("italy", "италия", "италии")),
    ("ee", "Эстония", "Estonia", "🇪🇪", ("estonia", "эстония")),
    ("ge", "Грузия", "Georgia", "🇬🇪", ("georgia", "грузия")),
    ("lt", "Литва", "Lithuania", "🇱🇹", ("lithuania", "литва")),
    ("md", "Молдова", "Moldova", "🇲🇩", ("moldova", "молдова")),
    ("al", "Албания", "Albania", "🇦🇱", ("albania", "албания")),
    ("il", "Израиль", "Israel", "🇮🇱", ("israel", "израиль")),
    ("in", "Индия", "India", "🇮🇳", ("india", "индия")),
    ("pk", "Пакистан", "Pakistan", "🇵🇰", ("pakistan", "пакистан")),
    ("by", "Беларусь", "Belarus", "🇧🇾", ("belarus", "беларусь")),
)

# Map only explicitly present city labels. No fabricated capitals.
CITIES = {
    "frankfurt": "Франкфурт", "франкфурт": "Франкфурт",
    "amsterdam": "Амстердам", "амстердам": "Амстердам",
    "new york": "Нью-Йорк", "нью-йорк": "Нью-Йорк",
    "helsinki": "Хельсинки", "хельсинки": "Хельсинки",
    "stockholm": "Стокгольм", "стокгольм": "Стокгольм",
    "london": "Лондон", "лондон": "Лондон",
    "paris": "Париж", "париж": "Париж",
    "warsaw": "Варшава", "варшава": "Варшава",
    "moscow": "Москва", "москва": "Москва",
    "miami": "Майами", "майами": "Майами",
    "istanbul": "Стамбул", "стамбул": "Стамбул",
    "zurich": "Цюрих", "цюрих": "Цюрих",
    "vienna": "Вена", "вена": "Вена",
    "milan": "Милан", "милан": "Милан",
}

def _has_token(label: str, alias: str) -> bool:
    # Explicit word boundaries (including Cyrillic); never match "nl" in "Finland".
    return re.search(r"(?<![\w])" + re.escape(alias) + r"(?![\w])", label, flags=re.I) is not None


def detect_server_geo(remark: str) -> tuple[str, str, str, str, str]:
    """Return (country_ru, iso_code, city_ru, flag, country_en).

    Unknown location is intentionally unknown. No IP geolocation or hidden
    network lookups are performed during subscription parsing.
    """
    label = str(remark or "").casefold()
    explicit = next((entry for entry in COUNTRIES if entry[3] in label), None)
    if explicit is None:
        explicit = next(
            (entry for entry in COUNTRIES if any(_has_token(label, name) for name in entry[4])),
            None,
        )
    if explicit is None:
        explicit = next(
            (entry for entry in COUNTRIES if _has_token(label, entry[0])),
            None,
        )
    if explicit is None:
        return "Сервер", "", "", "🌐", "Server"

    city = next(
        (localized for alias, localized in CITIES.items() if _has_token(label, alias)),
        "",
    )
    return explicit[1], explicit[0], city, explicit[3], explicit[2]
