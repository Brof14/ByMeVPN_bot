import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_IDS: list[int] = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "0")).split(",")
    if x.strip()
]
ADMIN_ID: int = ADMIN_IDS[0] if ADMIN_IDS else 0

SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "@ByMeVPN_support")
MENU_PHOTO: str = os.getenv("MENU_PHOTO", "")

# 3x-UI settings
XUI_HOST: str = os.getenv("XUI_HOST", "")
XUI_USERNAME: str = os.getenv("XUI_USERNAME", "")
XUI_PASSWORD: str = os.getenv("XUI_PASSWORD", "")
INBOUND_ID: int = int(os.getenv("INBOUND_ID", "3"))

# VLESS/Reality settings
REALITY_HOST: str = os.getenv("REALITY_HOST", "")
REALITY_PORT: int = int(os.getenv("REALITY_PORT", "443"))
REALITY_SNI: str = os.getenv("REALITY_SNI", "www.microsoft.com")
REALITY_FP: str = os.getenv("REALITY_FP", "chrome")
REALITY_PBK: str = os.getenv("REALITY_PBK", "")
REALITY_SID: str = os.getenv("REALITY_SID", "")

# YooKassa settings
YOOKASSA_SHOP_ID: str = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY: str = os.getenv("YOOKASSA_SECRET_KEY", "")

# Trial settings — 3 days FREE (0 rubles)
TRIAL_DAYS: int = 3
TRIAL_PRICE: int = 0  # Free trial

# Referral bonus
REF_BONUS_DAYS: int = int(os.getenv("REF_BONUS_DAYS", "3"))

# Prices (rubles). Stars = same number as rubles (1:1, no conversion)
# 1 device
BASE_PRICE_1_MONTH: int = 79
BASE_PRICE_6_MONTHS: int = 499
BASE_PRICE_12_MONTHS: int = 699
BASE_PRICE_24_MONTHS: int = 999

# 2 devices
PRICE_1_MONTH_2D: int = 99
PRICE_6_MONTHS_2D: int = 499
PRICE_12_MONTHS_2D: int = 699
PRICE_24_MONTHS_2D: int = 999

# 5 devices
PRICE_1_MONTH_5D: int = 129
PRICE_6_MONTHS_5D: int = 499
PRICE_12_MONTHS_5D: int = 799
PRICE_24_MONTHS_5D: int = 999

# Days for each period (including bonus months)
DAYS_1M: int = 30
DAYS_6M: int = 240   # 6+2 months
DAYS_12M: int = 450  # 12+3 months
DAYS_24M: int = 900  # 24+6 months
