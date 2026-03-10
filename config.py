import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
REF_BONUS_DAYS = int(os.getenv("REF_BONUS_DAYS", 7))
REF_BONUS_RUB = int(os.getenv("REF_BONUS_RUB", 50))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@ByMeVPN_support")
MENU_PHOTO = os.getenv("MENU_PHOTO", "")

XUI_HOST = os.getenv("XUI_HOST")
XUI_BASE_PATH = os.getenv("XUI_BASE_PATH", "/")
XUI_USERNAME = os.getenv("XUI_USERNAME")
XUI_PASSWORD = os.getenv("XUI_PASSWORD")
INBOUND_ID = int(os.getenv("INBOUND_ID", 1))

REALITY_HOST = os.getenv("REALITY_HOST")
REALITY_PORT = int(os.getenv("REALITY_PORT", 443))
REALITY_SNI = os.getenv("REALITY_SNI", "www.microsoft.com")
REALITY_FP = os.getenv("REALITY_FP", "firefox")
REALITY_PBK = os.getenv("REALITY_PBK")
REALITY_SID = os.getenv("REALITY_SID", "")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")

PROMO_CODE_FIRST = os.getenv("PROMO_CODE_FIRST", "FIRST30")
PROMO_DISCOUNT_FIRST = int(os.getenv("PROMO_DISCOUNT_FIRST", 30))

STAR_RATE_RUB = 1.81

required_vars = [BOT_TOKEN, XUI_HOST, XUI_USERNAME, XUI_PASSWORD, REALITY_PBK]
if not all(required_vars):
    raise ValueError("Обязательные переменные в .env отсутствуют")