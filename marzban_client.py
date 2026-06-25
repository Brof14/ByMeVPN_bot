"""
3x-ui API wrapper — управление VPN-пользователями через панель 3x-ui.
Хирургически исправлен для обхода ошибок 403 Forbidden и корректного формата API 3x-ui.
"""

import json
import logging
import uuid as uuid_lib
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from config import (
    XUI_INBOUND_ID,
    XUI_INBOUND_IDS,
    XUI_PASSWORD,
    XUI_SUB_PATH,
    XUI_URL,
    XUI_USERNAME,
)

logger = logging.getLogger(__name__)

_cookies: Optional[dict] = None

# ---------------------------------------------------------------------------
# Вспомогательные функции для очистки URL
# ---------------------------------------------------------------------------


def _get_base_url_and_cookie_path() -> tuple[str, str]:
    """
    Разделяет кастомный URL на чистый базовый URL панели и секретный путь (cookie path).
    3x-ui требует, чтобы сессионные куки привязывались к секретному пути, если он задан.
    """
    parsed = urlparse(XUI_URL)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    return base_url, path


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------


async def _login() -> dict:
    """Авторизоваться в 3x-ui и вернуть cookies."""
    global _cookies
    if not XUI_URL:
        raise RuntimeError("XUI_URL not configured. Set XUI_URL in .env")
    if not XUI_USERNAME or not XUI_PASSWORD:
        raise RuntimeError("XUI credentials not configured.")

    base_url, secret_path = _get_base_url_and_cookie_path()
    # Если есть секретный путь, логин идет на base/secret/login, иначе на base/login
    login_url = f"{base_url}{secret_path}/login" if secret_path else f"{base_url}/login"

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.post(
            login_url,
            data={"username": XUI_USERNAME, "password": XUI_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"3x-ui login failed: {data.get('msg', 'unknown')}")

        _cookies = dict(resp.cookies)
        logger.info("3x-ui login OK, url=%s", login_url)
        return _cookies


async def _get_cookies() -> dict:
    global _cookies
    if not _cookies:
        await _login()
    return _cookies


async def _api(method: str, path: str, **kwargs) -> dict:
    """Авторизованный запрос с автоповтором при 401."""
    global _cookies
    cookies = await _get_cookies()
    base_url, secret_path = _get_base_url_and_cookie_path()

    # Формируем корректный эндпоинт API с учетом секретного пути панели
    full_url = f"{base_url}{secret_path}{path}"

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        resp = await client.request(method, full_url, cookies=cookies, **kwargs)
        if resp.status_code == 401:
            logger.warning("3x-ui session expired, re-login")
            _cookies = None
            cookies = await _login()
            resp = await client.request(method, full_url, cookies=cookies, **kwargs)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Взаимодействие с Инбаундами
# ---------------------------------------------------------------------------


async def _get_client_from_inbound(inbound_id: int, email: str) -> Optional[dict]:
    """Найти клиента в конкретном инбаунде по email."""
    try:
        data = await _api("GET", f"/panel/api/inbounds/get/{inbound_id}")
        if not data.get("success"):
            return None

        obj = data.get("obj", {})
        if not obj:
            return None

        settings = json.loads(obj.get("settings", "{}"))
        for client in settings.get("clients", []):
            if client.get("email") == email:
                return client
        return None
    except Exception as e:
        logger.error(
            "_get_client_from_inbound(%d) error for %s: %s", inbound_id, email, e
        )
        return None


async def _get_client_by_email(email: str) -> Optional[dict]:
    """Найти клиента в первом инбаунде, где он присутствует."""
    for inbound_id in XUI_INBOUND_IDS:
        client = await _get_client_from_inbound(inbound_id, email)
        if client:
            return client
    return None


def _build_sub_url(sub_id: str) -> str:
    base_url, secret_path = _get_base_url_and_cookie_path()
    return f"{base_url}{secret_path}/{XUI_SUB_PATH.strip('/')}/{sub_id}"


# ---------------------------------------------------------------------------
# Stub-совместимость
# ---------------------------------------------------------------------------


def get_api_client():
    return None


def get_auth_token() -> Optional[str]:
    return None


# ---------------------------------------------------------------------------
# Валидация и тест соединения
# ---------------------------------------------------------------------------


def validate_marzban_config() -> tuple[bool, str]:
    errors = []
    if not XUI_URL:
        errors.append("XUI_URL не указан")
    elif not XUI_URL.startswith(("http://", "https://")):
        errors.append("XUI_URL должен начинаться с http:// или https://")
    if not XUI_USERNAME:
        errors.append("XUI_USERNAME не указан")
    if not XUI_PASSWORD:
        errors.append("XUI_PASSWORD не указан")
    if not XUI_INBOUND_IDS:
        errors.append("XUI_INBOUND_IDS не указан")
    return (False, "; ".join(errors)) if errors else (True, "")


async def test_marzban_connection() -> tuple[bool, str]:
    is_valid, err = validate_marzban_config()
    if not is_valid:
        return False, f"Ошибка конфигурации: {err}"
    try:
        global _cookies
        _cookies = None
        await _login()
        data = await _api("GET", f"/panel/api/inbounds/get/{XUI_INBOUND_IDS[0]}")
        if data.get("success"):
            ids_str = ",".join(map(str, XUI_INBOUND_IDS))
            return True, f"Соединение с 3x-ui успешно (инбаунды: {ids_str})"
        return False, f"Инбаунд {XUI_INBOUND_IDS[0]} недоступен: {data.get('msg')}"
    except Exception as e:
        return False, f"Ошибка подключения к 3x-ui: {str(e)}"


# ---------------------------------------------------------------------------
# Основные операции
# ---------------------------------------------------------------------------


async def create_marzban_user(
    user_id: int,
    days: int,
    data_limit_gb: int = 0,
) -> Optional[Dict[str, Any]]:
    """Добавить клиента во все инбаунды 3x-ui."""
    email = f"tg_{user_id}"
    expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
    total_bytes = data_limit_gb * 1024**3 if data_limit_gb > 0 else 0

    logger.info(
        "create_marzban_user: email=%s days=%d inbounds=%s",
        email,
        days,
        XUI_INBOUND_IDS,
    )

    try:
        existing = await _get_client_by_email(email)
        if existing:
            client_uuid = existing.get("id", "")
            sub_id = existing.get("subId", client_uuid.replace("-", "")[:16])
        else:
            client_uuid = str(uuid_lib.uuid4())
            sub_id = client_uuid.replace("-", "")[:16]

        errors = []
        for inbound_id in XUI_INBOUND_IDS:
            try:
                existing_in_inbound = await _get_client_from_inbound(inbound_id, email)

                client_data = {
                    "id": client_uuid,
                    "alterId": 0,
                    "email": email,
                    "limitIp": 0,
                    "totalGB": total_bytes,
                    "expiryTime": expiry_ms,
                    "enable": True,
                    "tgId": "",
                    "subId": sub_id,
                    "reset": 0,
                }

                # Фикс Payload для 3x-ui API: settings должен быть строкой JSON, содержащей массив клиентов
                payload = {
                    "id": inbound_id,
                    "settings": json.dumps({"clients": [client_data]}),
                }

                if existing_in_inbound:
                    # Обновление существующего клиента по его UUID
                    result = await _api(
                        "POST",
                        f"/panel/api/inbounds/updateClient/{client_uuid}",
                        json=payload,
                    )
                    action = "updated"
                else:
                    # Добавление нового клиента в инбаунд
                    result = await _api(
                        "POST",
                        "/panel/api/inbounds/addClient",
                        json=payload,
                    )
                    action = "created"

                if result.get("success"):
                    logger.info(
                        "3x-ui client %s in inbound %d: email=%s",
                        action,
                        inbound_id,
                        email,
                    )
                else:
                    errors.append(f"inbound {inbound_id}: {result.get('msg')}")
                    logger.error(
                        "Failed %s client in inbound %d: %s",
                        action,
                        inbound_id,
                        result.get("msg"),
                    )

            except Exception as e:
                errors.append(f"inbound {inbound_id}: {e}")
                logger.error(
                    "Error processing inbound %d for %s: %s", inbound_id, email, e
                )

        if len(errors) == len(XUI_INBOUND_IDS):
            logger.error(
                "create_marzban_user: failed in ALL inbounds for %s: %s", email, errors
            )
            return None

        subscription_url = _build_sub_url(sub_id)
        logger.info("Subscription URL for %s: %s", email, subscription_url)
        return {
            "subscription_url": subscription_url,
            "username": email,
            "vless_links": [],
        }

    except Exception as e:
        logger.exception("create_marzban_user FAILED for user=%d: %s", user_id, e)
        return None


async def get_marzban_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Получить трафик клиента."""
    email = f"tg_{user_id}"
    try:
        total_up = 0
        total_down = 0
        expire = 0
        data_limit = 0
        found = False

        for inbound_id in XUI_INBOUND_IDS:
            try:
                data = await _api(
                    "GET", f"/panel/api/inbounds/getClientTraffics/{email}"
                )
                if data.get("success") and data.get("obj"):
                    obj = data["obj"]
                    total_up += obj.get("up", 0) or 0
                    total_down += obj.get("down", 0) or 0
                    if not expire:
                        expire = (obj.get("expiryTime", 0) or 0) // 1000
                    if not data_limit:
                        data_limit = obj.get("total", 0) or 0
                    found = True
                    break
            except Exception:
                pass

        if not found:
            return None
        return {
            "used_traffic": total_up + total_down,
            "expire": expire,
            "data_limit": data_limit,
        }
    except Exception as e:
        logger.error("get_marzban_user FAILED for user=%d: %s", user_id, e)
        return None


async def delete_marzban_user(user_id: int) -> bool:
    """Удалить клиента из всех инбаундов."""
    email = f"tg_{user_id}"
    client = await _get_client_by_email(email)
    if not client:
        logger.info("delete_marzban_user: %s not found (already deleted?)", email)
        return True

    client_uuid = client.get("id", "")
    if not client_uuid:
        logger.error("delete_marzban_user: empty uuid for %s", email)
        return False

    all_ok = True
    for inbound_id in XUI_INBOUND_IDS:
        try:
            exists = await _get_client_from_inbound(inbound_id, email)
            if not exists:
                continue
            result = await _api(
                "POST",
                f"/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}",
            )
            if result.get("success"):
                logger.info(
                    "3x-ui client deleted from inbound %d: email=%s", inbound_id, email
                )
            else:
                logger.error(
                    "Failed to delete from inbound %d: %s",
                    inbound_id,
                    result.get("msg"),
                )
                all_ok = False
        except Exception as e:
            logger.error(
                "Error deleting from inbound %d for %s: %s", inbound_id, email, e
            )
            all_ok = False

    return all_ok


async def update_marzban_user_expiry(user_id: int, days: int) -> bool:
    """Обновить срок действия во всех инбаундах."""
    email = f"tg_{user_id}"
    new_expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)

    logger.info("update_marzban_user_expiry: email=%s days=%d", email, days)

    client_base = await _get_client_by_email(email)
    if not client_base:
        logger.warning("update_marzban_user_expiry: %s not found", email)
        return False

    client_uuid = client_base.get("id", "")
    all_ok = True

    for inbound_id in XUI_INBOUND_IDS:
        try:
            client = await _get_client_from_inbound(inbound_id, email)
            if not client:
                continue
            updated = dict(client)
            updated["expiryTime"] = new_expiry_ms
            updated["enable"] = True

            payload = {
                "id": inbound_id,
                "settings": json.dumps({"clients": [updated]}),
            }
            result = await _api(
                "POST",
                f"/panel/api/inbounds/updateClient/{client_uuid}",
                json=payload,
            )
            if result.get("success"):
                logger.info("Expiry updated in inbound %d for %s", inbound_id, email)
            else:
                logger.error(
                    "Failed expiry update in inbound %d: %s",
                    inbound_id,
                    result.get("msg"),
                )
                all_ok = False
        except Exception as e:
            logger.error(
                "Error updating expiry in inbound %d for %s: %s", inbound_id, email, e
            )
            all_ok = False

    return all_ok


async def get_user_subscription_links(user_id: int) -> Optional[List[str]]:
    email = f"tg_{user_id}"
    try:
        client = await _get_client_by_email(email)
        if not client:
            return []
        sub_id = client.get("subId", "")
        if not sub_id:
            return []
        return [_build_sub_url(sub_id)]
    except Exception as e:
        logger.error("get_user_subscription_links FAILED for user=%d: %s", user_id, e)
        return None


def format_traffic(bytes_used: int) -> str:
    if bytes_used < 1024 * 1024:
        return f"{bytes_used / 1024:.2f} КБ"
    elif bytes_used < 1024 * 1024 * 1024:
        return f"{bytes_used / (1024 * 1024):.2f} МБ"
    else:
        return f"{bytes_used / (1024 * 1024 * 1024):.2f} ГБ"
