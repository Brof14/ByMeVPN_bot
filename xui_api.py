# xui_api.py
import asyncio
import json
import time
import uuid
import logging

import httpx

from config import XUI_HOST, XUI_BASE_PATH, XUI_USERNAME, XUI_PASSWORD, INBOUND_ID, REALITY_HOST, REALITY_PORT, REALITY_SNI, REALITY_FP, REALITY_PBK, REALITY_SID

logger = logging.getLogger(__name__)

session = httpx.AsyncClient(verify=False, timeout=15.0)
_logged_in = False
BASE_URL = f"{XUI_HOST.rstrip('/')}/{XUI_BASE_PATH.strip('/')}"


async def login() -> bool:
    """Авторизация в XUI с повторными попытками"""
    global _logged_in
    if _logged_in:
        return True
    for attempt in range(3):
        try:
            r = await session.post(f"{BASE_URL}/login", data={"username": XUI_USERNAME, "password": XUI_PASSWORD})
            if r.status_code == 200 and r.json().get("success"):
                _logged_in = True
                return True
        except Exception as e:
            logger.warning(f"Попытка {attempt + 1} логина: {e}")
            await asyncio.sleep(2)
    logger.error("Авторизация в XUI провалена")
    return False


async def add_client(days: int, remark: str) -> str:
    """Добавление клиента в XUI с повторными попытками"""
    if not await login():
        raise RuntimeError("Не удалось авторизоваться в панели XUI")
    client_uuid = str(uuid.uuid4())
    expiry_ms = int(time.time() * 1000) + days * 86400 * 1000
    client = {
        "id": client_uuid,
        "flow": "",
        "email": remark,
        "limitIp": 0,
        "totalGB": 0,
        "expiryTime": expiry_ms,
        "enable": True,
        "tgId": "",
        "subId": str(uuid.uuid4())[:8],
        "reset": 0
    }
    settings = json.dumps({"clients": [client]})
    payload = {"id": INBOUND_ID, "settings": settings}
    for attempt in range(3):
        try:
            r = await session.post(f"{BASE_URL}/panel/api/inbounds/addClient", data=payload)
            if r.status_code == 200 and r.json().get("success"):
                return client_uuid
            logger.warning(f"Попытка {attempt + 1} добавления клиента: {r.text}")
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"Попытка {attempt + 1}: {e}")
    raise RuntimeError("Не удалось добавить клиента в XUI")


def generate_vless(client_uuid: str) -> str:
    """Генерация VLESS-ключа"""
    params = f"type=xhttp&security=reality&fp={REALITY_FP}&pbk={REALITY_PBK}&sni={REALITY_SNI}&sid={REALITY_SID}&spx=%2F"
    return f"vless://{client_uuid}@{REALITY_HOST}:{REALITY_PORT}?{params}#ByMeVPN"