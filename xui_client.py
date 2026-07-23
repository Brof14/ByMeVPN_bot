"""
3x-ui API wrapper — управление VPN-пользователями через панель 3x-ui.
Использует py3xui AsyncApi (HTTP API).
"""

import logging
import uuid as uuid_lib
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import asyncio

from py3xui import AsyncApi, Client

from config import (
    XUI_API_URL,
    XUI_INBOUND_IDS,
    XUI_PASSWORD,
    XUI_SUB_PATH,
    XUI_URL,
    XUI_USERNAME,
)

logger = logging.getLogger(__name__)

# Connection pool for 3x-ui API
_api_pool: Optional[AsyncApi] = None
_api_lock = asyncio.Lock()


async def _get_api() -> AsyncApi:
    """Get or create pooled 3x-ui API connection with retry logic."""
    global _api_pool
    
    async with _api_lock:
        if _api_pool is not None:
            return _api_pool
        
        # py3xui требует полный URL с путем к панели
        # Важно: для локальных адресов принудительно используем HTTP
        api_url = XUI_API_URL
        
        # Если URL начинается с https:// но указан 127.0.0.1 или localhost, меняем на http
        if api_url.startswith("https://") and ("127.0.0.1" in api_url or "localhost" in api_url):
            api_url = api_url.replace("https://", "http://")
            logger.info("Changed HTTPS to HTTP for local address: %s", api_url)
        
        _api_pool = AsyncApi(
            api_url,
            username=XUI_USERNAME,
            password=XUI_PASSWORD,
            use_tls_verify=False,
            logger=logger,
        )
        
        # Pre-login to keep connection alive
        await _api_pool.login()
        logger.info("3x-ui API connection pooled and authenticated")
        
        return _api_pool


async def _api_call_with_retry(func, *args, max_retries=3, base_delay=0.5, **kwargs):
    """Execute API call with exponential backoff retry logic."""
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error("API call failed after %d retries: %s", max_retries, e)
                raise
            
            delay = base_delay * (2 ** attempt)  # Exponential backoff
            logger.warning("API call failed (attempt %d/%d): %s, retrying in %.1fs", 
                          attempt + 1, max_retries, e, delay)
            await asyncio.sleep(delay)


def _build_sub_url(sub_id: str) -> str:
    parsed = urlparse(XUI_URL)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base_url}/{XUI_SUB_PATH.strip('/')}/{sub_id}"


def _ms_to_ts(ms: int) -> int:
    return ms // 1000 if ms else 0


def validate_xui_config() -> tuple[bool, str]:
    errors: list[str] = []
    if not XUI_API_URL:
        errors.append("XUI_API_URL не указан")
    if not XUI_USERNAME:
        errors.append("XUI_USERNAME не указан")
    if not XUI_PASSWORD:
        errors.append("XUI_PASSWORD не указан")
    if not XUI_INBOUND_IDS:
        errors.append("XUI_INBOUND_IDS не указан")
    return (False, "; ".join(errors)) if errors else (True, "")


async def test_xui_connection() -> tuple[bool, str]:
    is_valid, err = validate_xui_config()
    if not is_valid:
        return False, f"Ошибка конфигурации: {err}"
    try:
        api = await _get_api()
        inbounds = await _api_call_with_retry(api.inbound.get_list)
        ids_str = ",".join(map(str, XUI_INBOUND_IDS))
        return True, (
            f"Соединение с 3x-ui успешно. "
            f"Инбаундов в панели: {len(inbounds)}, настроенные: {ids_str}"
        )
    except Exception as e:
        logger.exception("test_xui_connection failed: %s", e)
        return False, f"Ошибка подключения к 3x-ui API: {str(e)}"


async def _find_client_uuid(api: AsyncApi, email: str) -> Optional[str]:
    """
    Ищет UUID клиента по email через все возможные методы (parallel).
    Возвращает UUID или None.
    """
    inbound_id = 2  # Только VLESS
    
    # Run both methods in parallel for speed
    tasks = []
    
    # Метод 1: get_by_email
    async def method1():
        try:
            client = await _api_call_with_retry(api.client.get_by_email, email)
            if client and client.uuid:
                logger.info("Found client %s via get_by_email: uuid=%s", email, client.uuid)
                return client.uuid
        except Exception as e:
            logger.debug("get_by_email failed for %s: %s", email, e)
        return None
    
    # Метод 2: Поиск в client_stats инбаунда 2
    async def method2():
        try:
            inbound = await _api_call_with_retry(api.inbound.get_by_id, inbound_id)
            if inbound and inbound.client_stats:
                for c in inbound.client_stats:
                    if c.email == email and c.uuid:
                        logger.info("Found client %s in inbound %d client_stats: uuid=%s", email, inbound_id, c.uuid)
                        return c.uuid
        except Exception as e:
            logger.debug("Failed to search inbound %d for %s: %s", inbound_id, email, e)
        return None
    
    tasks = [method1(), method2()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Return first non-None result
    for result in results:
        if isinstance(result, str) and result:
            return result
    
    logger.warning("Client %s not found anywhere", email)
    return None


async def _find_client_sub_id(api: AsyncApi, email: str) -> Optional[str]:
    """
    Ищет sub_id клиента по email через все возможные методы (parallel).
    Возвращает sub_id или None.
    """
    inbound_id = 2  # Только VLESS
    
    # Run methods in parallel for speed
    async def method1():
        try:
            client = await _api_call_with_retry(api.client.get_by_email, email)
            if client and client.sub_id:
                logger.info("Found client %s sub_id via get_by_email: sub_id=%s", email, client.sub_id)
                return client.sub_id
        except Exception as e:
            logger.debug("get_by_email failed for %s: %s", email, e)
        return None
    
    async def method2():
        try:
            inbound = await _api_call_with_retry(api.inbound.get_by_id, inbound_id)
            if inbound and inbound.client_stats:
                for c in inbound.client_stats:
                    if c.email == email and c.sub_id:
                        logger.info("Found client %s sub_id in inbound %d client_stats: sub_id=%s", email, inbound_id, c.sub_id)
                        return c.sub_id
        except Exception as e:
            logger.debug("Failed to search inbound %d for %s: %s", inbound_id, email, e)
        return None
    
    tasks = [method1(), method2()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Return first non-None result
    for result in results:
        if isinstance(result, str) and result:
            return result
    
    # Метод 3: Если не нашли sub_id, но есть uuid, используем его для генерации sub_id
    try:
        client_uuid = await _find_client_uuid(api, email)
        if client_uuid:
            # Генерируем sub_id из uuid (убираем дефисы и берем первые 16 символов)
            generated_sub_id = client_uuid.replace("-", "")[:16]
            logger.info("Generated sub_id for %s from uuid: %s", email, generated_sub_id)
            return generated_sub_id
    except Exception as e:
        logger.debug("Failed to generate sub_id from uuid for %s: %s", email, e)
    
    logger.warning("Client %s sub_id not found anywhere", email)
    return None


async def create_xui_user(
    user_id: int,
    days: int,
    data_limit_gb: int = 0,
    limit_ip: int = 0,
) -> Optional[Dict[str, Any]]:
    """
    Создать клиента в инбаундах ID: 2 и 3 (VLESS) через py3xui HTTP API.
    
    Простой алгоритм:
    1. Принудительно удалить существующего клиента (если есть)
    2. Создать нового клиента в инбаундах 2 и 3
    3. Получить конкретную конфигурацию VLESS
    """
    email = str(user_id)
    expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
    total_gb_limit = data_limit_gb  # 0 = безлимит
    inbound_ids = [2, 3]  # VLESS инбаунды

    logger.info(
        "create_xui_user: email=%s days=%d limit_ip=%d inbounds=%s",
        email, days, limit_ip, inbound_ids,
    )

    try:
        api = await _get_api()

        # Шаг 1: Принудительно удалить существующего клиента из инбаундов 2 и 3 (parallel)
        existing_uuid = await _find_client_uuid(api, email)
        
        if existing_uuid:
            # Delete from all inbounds in parallel
            delete_tasks = []
            for inbound_id in inbound_ids:
                async def delete_from_inbound(iid):
                    try:
                        logger.info("Force deleting existing client %s (uuid=%s) from inbound %d", email, existing_uuid, iid)
                        await _api_call_with_retry(api.client.delete, iid, existing_uuid)
                        logger.info("Deleted client %s from inbound %d", email, iid)
                    except Exception as del_err:
                        logger.debug("Failed to delete client %s from inbound %d: %s", email, iid, del_err)
                
                delete_tasks.append(delete_from_inbound(inbound_id))
            
            await asyncio.gather(*delete_tasks, return_exceptions=True)
        else:
            logger.info("No existing client found for %s", email)
        
        # Шаг 2: Создаем нового клиента в обоих инбаундах (parallel)
        logger.info("Creating new client %s in inbounds %s", email, inbound_ids)
        
        client_uuid = str(uuid_lib.uuid4())
        sub_id = client_uuid.replace("-", "")[:16]

        # Названия для инбаундов (используем при создании клиентов и генерации ссылок)
        inbound_names = {2: "🇳🇱Netherlands", 3: "🇳🇱Netherlands-2"}

        success_count = 0
        create_tasks = []
        
        for inbound_id in inbound_ids:
            async def create_in_inbound(iid):
                nonlocal success_count, sub_id, client_uuid
                try:
                    # Устанавливаем remark для каждого инбаунда отдельно
                    inbound_name = inbound_names.get(iid, "🇳🇱Netherlands")
                    client_with_remark = Client(
                        email=email,
                        uuid=client_uuid,
                        enable=True,
                        expiry_time=expiry_ms,
                        limit_ip=limit_ip,
                        total_gb=total_gb_limit,
                        sub_id=sub_id,
                        remark=inbound_name,
                        tg_id=user_id,  # Set tg_id as integer to avoid type error
                    )
                    await _api_call_with_retry(api.client.add, iid, [client_with_remark])
                    logger.info("Successfully added client %s to inbound %d with remark %s", email, iid, inbound_name)
                    return True
                except Exception as e:
                    err_str = str(e)
                    if "Duplicate" in err_str or "duplicate" in err_str.lower() or "already exists" in err_str.lower():
                        # Клиент уже существует - пробуем получить реальные данные из client_stats
                        logger.info("Client %s already exists in inbound %d, searching in client_stats", email, iid)
                        try:
                            inbound = await _api_call_with_retry(api.inbound.get_by_id, iid)
                            if inbound and inbound.client_stats:
                                for c in inbound.client_stats:
                                    if c.email == email:
                                        if c.sub_id:
                                            sub_id = c.sub_id
                                        if c.uuid:
                                            client_uuid = c.uuid
                                        logger.info("Found client in client_stats: uuid=%s, sub_id=%s", client_uuid, sub_id)
                                        return True
                        except Exception as get_err:
                            logger.warning("Failed to search client_stats: %s", get_err)
                    else:
                        logger.error("Failed to add client %s to inbound %d: %s", email, iid, e)
                return False
            
            create_tasks.append(create_in_inbound(inbound_id))
        
        results = await asyncio.gather(*create_tasks, return_exceptions=True)
        success_count = sum(1 for r in results if r is True)

        if success_count == 0:
            logger.error("Failed to add client %s to any inbound", email)
            return None

        # Проверяем что клиент реально создан и получаем его uuid (no sleep needed)
        logger.info("Verifying client creation and getting uuid for %s", email)
        try:
            existing_client = await _api_call_with_retry(api.client.get_by_email, email)
            if existing_client and existing_client.uuid:
                client_uuid = existing_client.uuid
                logger.info("Using verified uuid from panel: %s", client_uuid)
            else:
                logger.warning("Client created but no uuid found, using generated: %s", client_uuid)
        except Exception as e:
            logger.warning("Failed to verify client: %s, using generated uuid", e)

        # Генерируем VLESS ссылки для обоих инбаундов с разными названиями (parallel)
        logger.info("Generating VLESS links for client %s", email)
        vless_links = []
        
        async def generate_link_for_inbound(iid):
            try:
                # Получаем inbound конфигурацию
                inbound = await _api_call_with_retry(api.inbound.get_by_id, iid)
                if inbound:
                    logger.info("Inbound %d data: %s", iid, inbound)
                    
                    # Генерируем линк для клиента на основе данных inbound
                    try:
                        # Получаем адрес и порт из inbound
                        address = getattr(inbound, 'address', 'bymevpn.duckdns.org')
                        port = getattr(inbound, 'port', 443)
                        
                        # Генерируем VLESS линк с названием для конкретного инбаунда
                        inbound_name = inbound_names.get(iid, "🇳🇱Netherlands")
                        vless_link = (
                            f"vless://{client_uuid}@{address}:{port}"
                            f"?encryption=none"
                            f"&security=tls"
                            f"&type=tcp"
                            f"&flow=xtls-rprx-vision"
                            f"&sni={address}"
                            f"&alpn=h3,h2,http/1.1"
                            f"&fp=chrome"
                            f"&mux=4"  # Multiplexing for better speed
                            f"&allowInsecure=0"
                            f"#{inbound_name}"
                        )
                        
                        logger.info("Generated VLESS link for inbound %d: %s", iid, vless_link[:80] + "...")
                        return vless_link
                    except Exception as gen_err:
                        logger.warning("Failed to generate VLESS link for inbound %d: %s", iid, gen_err)
                        
                        # Простой fallback
                        inbound_name = inbound_names.get(iid, "🇳🇱Netherlands")
                        vless_link = (
                            f"vless://{client_uuid}@bymevpn.duckdns.org:443"
                            f"?encryption=none&security=tls&type=tcp&flow=xtls-rprx-vision"
                            f"&sni=bymevpn.duckdns.org&alpn=h3,h2,http/1.1&fp=chrome"
                            f"&mux=4&allowInsecure=0#{inbound_name}"
                        )
                        logger.info("Generated simple fallback VLESS link for inbound %d", iid)
                        return vless_link
            except Exception as e:
                logger.warning("Failed to get inbound %d: %s", iid, e)
                # Ultimate fallback
                inbound_name = inbound_names.get(iid, "🇳🇱Netherlands")
                vless_link = (
                    f"vless://{client_uuid}@bymevpn.duckdns.org:443"
                    f"?encryption=none&security=tls&type=tcp&flow=xtls-rprx-vision"
                    f"&sni=bymevpn.duckdns.org&alpn=h3,h2,http/1.1&fp=chrome"
                    f"&mux=4&allowInsecure=0#{inbound_name}"
                )
                logger.info("Generated ultimate fallback VLESS link for inbound %d", iid)
                return vless_link
        
        # Generate all links in parallel
        link_tasks = [generate_link_for_inbound(iid) for iid in inbound_ids]
        link_results = await asyncio.gather(*link_tasks, return_exceptions=True)
        
        for result in link_results:
            if isinstance(result, str) and result:
                vless_links.append(result)

        logger.info("Generated %d VLESS links for %s", len(vless_links), email)

        # Генерируем ссылку на подписку
        subscription_url = _build_sub_url(sub_id)
        logger.info("Subscription URL for %s: %s", email, subscription_url)

        return {
            "subscription_url": subscription_url,
            "username": email,
            "vless_links": vless_links,
        }

    except Exception as e:
        logger.exception("create_xui_user FAILED for user=%d: %s", user_id, e)
        return None


async def get_xui_user(user_id: int) -> Optional[Dict[str, Any]]:
    email = str(user_id)
    try:
        api = await _get_api()

        client: Optional[Client] = await _api_call_with_retry(api.client.get_by_email, email)
        if client is None:
            logger.warning("get_xui_user: client %s not found", email)
            return None

        used_traffic = (client.up or 0) + (client.down or 0)
        expiry_sec = _ms_to_ts(client.expiry_time or 0)
        data_limit_bytes = (client.total_gb or 0) * (1024 ** 3)

        return {
            "used_traffic": used_traffic,
            "expire": expiry_sec,
            "data_limit": data_limit_bytes,
        }

    except Exception as e:
        logger.exception("get_xui_user FAILED for user=%d: %s", user_id, e)
        return None


async def delete_xui_user(user_id: int) -> bool:
    email = str(user_id)
    inbound_ids = [2, 3]  # VLESS инбаунды (те же, что и при создании)
    logger.info("delete_xui_user: email=%s inbounds=%s", email, inbound_ids)

    try:
        api = await _get_api()

        client_uuid = await _find_client_uuid(api, email)

        if not client_uuid:
            logger.info("delete_xui_user: client %s not found — already deleted?", email)
            return True

        # Delete from all inbounds in parallel (same as creation)
        delete_tasks = []
        for inbound_id in inbound_ids:
            async def delete_from_inbound(iid):
                try:
                    logger.info("Deleting client %s (uuid=%s) from inbound %d", email, client_uuid, iid)
                    await _api_call_with_retry(api.client.delete, iid, client_uuid)
                    logger.info("Deleted client %s from inbound %d", email, iid)
                    return True
                except Exception as del_err:
                    logger.debug("Failed to delete client %s from inbound %d: %s", email, iid, del_err)
                    return False
            
            delete_tasks.append(delete_from_inbound(inbound_id))
        
        results = await asyncio.gather(*delete_tasks, return_exceptions=True)
        success_count = sum(1 for r in results if r is True)
        
        if success_count == 0:
            logger.warning("Failed to delete client %s from any inbound", email)
            return False
        
        logger.info("Deleted client %s from %d/%d inbounds", email, success_count, len(inbound_ids))
        return True

    except Exception as e:
        logger.exception("delete_xui_user FAILED for user=%d: %s", user_id, e)
        return False


async def update_xui_user_expiry(user_id: int, days: int) -> bool:
    email = str(user_id)
    logger.info("update_xui_user_expiry: email=%s days=%d", email, days)

    try:
        api = await _get_api()

        client_uuid = await _find_client_uuid(api, email)

        if not client_uuid:
            logger.warning("update_xui_user_expiry: client %s not found — recreating", email)
            result = await create_xui_user(user_id, days)
            return result is not None

        # Получаем текущие настройки клиента
        try:
            client = await _api_call_with_retry(api.client.get_by_email, email)
            if client:
                current_limit_ip = client.limit_ip or 0
                current_total_gb = client.total_gb or 0
                current_sub_id = client.sub_id or ""
            else:
                current_limit_ip = 0
                current_total_gb = 0
                current_sub_id = ""
        except Exception as e:
            logger.debug("update_xui_user_expiry: failed to get client settings: %s", e)
            current_limit_ip = 0
            current_total_gb = 0
            current_sub_id = ""

        import time
        now_ms = int(time.time() * 1000)
        current_expiry_ms = 0
        
        # Пробуем получить текущий expiry из client
        if client and client.expiry_time:
            current_expiry_ms = client.expiry_time
        
        if current_expiry_ms > now_ms:
            new_expiry_ms = current_expiry_ms + days * 86400 * 1000
        else:
            new_expiry_ms = now_ms + days * 86400 * 1000

        sub_id = current_sub_id or client_uuid.replace("-", "")[:16]
        updated = Client(
            email=email,
            uuid=client_uuid,
            enable=True,
            expiry_time=new_expiry_ms,
            limit_ip=current_limit_ip,
            total_gb=current_total_gb,
            sub_id=sub_id,
        )
        await _api_call_with_retry(api.client.update, client_uuid, updated)
        logger.info(
            "update_xui_user_expiry: %s → new expiry %s (+%d days)",
            email,
            datetime.fromtimestamp(new_expiry_ms / 1000).strftime("%d.%m.%Y"),
            days,
        )
        return True

    except Exception as e:
        logger.exception("update_xui_user_expiry FAILED for user=%d: %s", user_id, e)
        return False


async def get_user_subscription_links(user_id: int) -> Optional[List[str]]:
    email = str(user_id)
    inbound_id = 2  # Только VLESS
    try:
        api = await _get_api()

        sub_id: Optional[str] = None
        try:
            client = await _api_call_with_retry(api.client.get_by_email, email)
            if client:
                sub_id = client.sub_id
        except Exception as e:
            logger.debug("get_user_subscription_links: get_by_email failed for %s: %s", email, e)

        if not sub_id:
            # Пробуем найти через client_stats инбаунда 2
            try:
                inbound = await _api_call_with_retry(api.inbound.get_by_id, inbound_id)
                if inbound and inbound.client_stats:
                    for c in inbound.client_stats:
                        if c.email == email and c.sub_id:
                            sub_id = c.sub_id
                            break
            except Exception as e:
                logger.debug("get_user_subscription_links: failed to search inbound %d for %s: %s", inbound_id, email, e)

        if not sub_id:
            logger.warning("get_user_subscription_links: client %s has no sub_id", email)
            return []

        url = _build_sub_url(sub_id)
        logger.info("get_user_subscription_links: returning URL for %s: %s", email, url)
        return [url]

    except Exception as e:
        logger.exception("get_user_subscription_links FAILED for user=%d: %s", user_id, e)
        return None


def format_traffic(bytes_used: int) -> str:
    if bytes_used < 1024 * 1024:
        return f"{bytes_used / 1024:.2f} КБ"
    elif bytes_used < 1024 * 1024 * 1024:
        return f"{bytes_used / (1024 * 1024):.2f} МБ"
    else:
        return f"{bytes_used / (1024 * 1024 * 1024):.2f} ГБ"


# Совместимость со старым кодом (алиасы)
create_marzban_user = create_xui_user
get_marzban_user = get_xui_user
delete_marzban_user = delete_xui_user
update_marzban_user_expiry = update_xui_user_expiry
get_user_subscription_links = get_user_subscription_links
validate_marzban_config = validate_xui_config
test_marzban_connection = test_xui_connection


def get_api_client():
    """Stub для совместимости с main.py (3x-ui не требует pre-init)"""
    return None
