"""
3x-ui API wrapper — управление VPN-пользователями через панель 3x-ui.
Использует py3xui AsyncApi (HTTP API).
"""

import logging
import uuid as uuid_lib
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, quote
from ipaddress import ip_address
import asyncio

from py3xui import AsyncApi, Client

from config import (
    XUI_API_URL,
    XUI_INBOUND_IDS,
    XUI_INBOUND_NAMES,
    XUI_PASSWORD,
    XUI_SUB_PATH,
    XUI_TLS_VERIFY,
    XUI_URL,
    XUI_USERNAME,
)

logger = logging.getLogger(__name__)


class CredentialRotationRollbackError(RuntimeError):
    """The panel changed VPN credentials but did not confirm rollback."""


def _inbound_name(inbound_id: int, inbound=None) -> str:
    """Prefer the live panel remark; use the verified map for safe fallback."""
    remark = getattr(inbound, "remark", None) if inbound is not None else None
    if isinstance(remark, str) and remark.strip():
        return remark.strip()
    return XUI_INBOUND_NAMES.get(inbound_id, f"ByMeVPN · {inbound_id}")


def _is_vless_inbound(inbound) -> bool:
    return str(getattr(inbound, "protocol", "")).strip().lower() == "vless"

# Connection pool for 3x-ui API
_api_pool: Optional[AsyncApi] = None
_api_lock = asyncio.Lock()
_API_LOGIN_ATTEMPTS = 3


def _is_authentication_error(exc: Exception) -> bool:
    """Return True for errors that make the cached panel session unusable."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    message = str(exc).lower()
    return (
        status in (401, 403)
        or "unauthorized" in message
        or "unauthenticated" in message
        or "session expired" in message
        or "csrf" in message
    )


def _invalidate_api_pool() -> None:
    """Force the next operation to authenticate with a fresh API instance."""
    global _api_pool
    if _api_pool is not None:
        logger.warning("3x-ui API session invalidated; next call will re-authenticate")
        _api_pool = None


async def _close_failed_api(api: AsyncApi) -> None:
    """Close a candidate session when authentication fails."""
    session = getattr(api, "session", None)
    if session is not None and not getattr(session, "closed", True):
        try:
            await session.close()
        except Exception as close_error:
            logger.debug("Could not close failed 3x-ui API session: %s", close_error)


async def _get_api() -> AsyncApi:
    """Get or create an authenticated pooled 3x-ui API connection."""
    global _api_pool

    async with _api_lock:
        if _api_pool is not None:
            return _api_pool

        api_url = XUI_API_URL or XUI_URL
        last_error: Optional[Exception] = None

        for attempt in range(_API_LOGIN_ATTEMPTS):
            candidate = AsyncApi(
                api_url,
                username=XUI_USERNAME,
                password=XUI_PASSWORD,
                use_tls_verify=XUI_TLS_VERIFY,
                logger=logger,
            )
            try:
                # Publish the candidate only after authentication succeeds. A failed
                # login must never poison the global pool for the process lifetime.
                await candidate.login()
            except Exception as exc:
                last_error = exc
                await _close_failed_api(candidate)
                if attempt < _API_LOGIN_ATTEMPTS - 1:
                    delay = 0.5 * (2 ** attempt)
                    logger.warning(
                        "3x-ui login failed (attempt %d/%d): %s; retrying in %.1fs",
                        attempt + 1,
                        _API_LOGIN_ATTEMPTS,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                continue

            _api_pool = candidate
            logger.info("3x-ui API connection pooled and authenticated")
            return _api_pool

        raise last_error or RuntimeError("3x-ui authentication failed")


async def _api_call_with_retry(func, *args, max_retries=3, base_delay=0.5, **kwargs):
    """Execute API call with exponential backoff retry logic."""
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if _is_authentication_error(e):
                _invalidate_api_pool()
                logger.error("3x-ui API authentication failed: %s", e)
                raise

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
        ids_str = ",".join(map(str, XUI_INBOUND_IDS))
        # Only probe the inbounds used for provisioning. Listing every panel
        # inbound can fail on unrelated malformed entries and adds retries to
        # startup, while the per-ID endpoint is the path key operations use.
        results = await asyncio.gather(
            *(_api_call_with_retry(api.inbound.get_by_id, iid) for iid in XUI_INBOUND_IDS),
            return_exceptions=True,
        )
        working = [
            iid for iid, result in zip(XUI_INBOUND_IDS, results)
            if not isinstance(result, Exception) and result is not None
        ]
        if not working:
            failures = [str(result)[:120] for result in results if isinstance(result, Exception)]
            raise RuntimeError(
                "No configured 3x-ui inbound responded"
                + (f": {'; '.join(failures)}" if failures else "")
            )
        if len(working) != len(XUI_INBOUND_IDS):
            logger.warning("Only configured 3x-ui inbounds %s respond; expected %s", working, ids_str)
        return True, f"Соединение с 3x-ui успешно. Рабочие настроенные инбаунды: {working}, настроенные: {ids_str}"
    except Exception as e:
        logger.exception("test_xui_connection failed: %s", e)
        return False, f"Ошибка подключения к 3x-ui API: {str(e)}"


async def _find_client_uuid(api: AsyncApi, email: str) -> Optional[str]:
    """
    Ищет UUID клиента по email через все возможные методы (parallel).
    Возвращает UUID или None.
    """
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
    
    # Method 2: search only configured inbounds. A hard-coded inbound can
    # silently miss an existing client after a panel migration.
    async def method2():
        for inbound_id in XUI_INBOUND_IDS:
            try:
                inbound = await _api_call_with_retry(api.inbound.get_by_id, inbound_id)
                for c in (inbound.client_stats or []) if inbound else []:
                    if c.email == email and c.uuid:
                        logger.info("Found client %s in configured inbound %d", email, inbound_id)
                        return c.uuid
            except Exception as e:
                logger.debug("Failed to search configured inbound %d for %s: %s", inbound_id, email, e)
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
        for inbound_id in XUI_INBOUND_IDS:
            try:
                inbound = await _api_call_with_retry(api.inbound.get_by_id, inbound_id)
                for c in (inbound.client_stats or []) if inbound else []:
                    if c.email == email and c.sub_id:
                        logger.info("Found client %s sub_id in configured inbound %d", email, inbound_id)
                        return c.sub_id
            except Exception as e:
                logger.debug("Failed to search configured inbound %d for %s: %s", inbound_id, email, e)
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
    new_expiry_ms: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Создать клиента в инбаундах через py3xui HTTP API.
    
    Простой алгоритм:
    1. Принудительно удалить существующего клиента (если есть)
    2. Создать нового клиента в инбаундах из конфигурации
    3. Получить конкретную конфигурацию VLESS
    """
    email = str(user_id)
    # Clamp days to prevent "date value out of range" (max ~10 years)
    days = max(1, min(days, 3650))
    expiry_ms = (
        int(new_expiry_ms)
        if new_expiry_ms is not None
        else int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
    )
    total_gb_limit = data_limit_gb  # 0 = безлимит
    inbound_ids = XUI_INBOUND_IDS  # Use configured inbound IDs

    logger.info(
        "create_xui_user: email=%s days=%d limit_ip=%d inbounds=%s",
        email, days, limit_ip, inbound_ids,
    )

    try:
        api = await _get_api()

        # Шаг 1: Проверяем, существует ли уже клиент в 3x-ui
        existing_uuid = await _find_client_uuid(api, email)
        
        if existing_uuid:
            logger.info("Client %s already exists in 3x-ui (uuid=%s) - PRESERVING UUID and updating", email, existing_uuid)
            existing_sub_id = await _find_client_sub_id(api, email) or existing_uuid.replace("-", "")[:16]
            update_res = await update_xui_user(
                user_id,
                days=days if new_expiry_ms is None else 0,
                new_expiry_ms=new_expiry_ms,
                limit_ip=limit_ip,
            )
            if update_res:
                update_res["created_new_client"] = False
                return update_res
            # Never fall through to client.add after finding a live account.
            # A partial add can leave inbounds with mixed expiry/device limits,
            # while falsely returning a successful subscription to the caller.
            logger.error(
                "Existing 3x-ui client %s (uuid=%s, sub_id=%s) could not be updated; refusing to create or mutate inbounds",
                email, existing_uuid, existing_sub_id,
            )
            return None
        else:
            logger.info("No existing client found for %s, creating new", email)
            client_uuid = str(uuid_lib.uuid4())
            sub_id = client_uuid.replace("-", "")[:16]

        # Шаг 2: Создаем клиента в инбаундах (parallel)
        logger.info("Adding client %s in inbounds %s", email, inbound_ids)

        success_count = 0
        create_tasks = []
        
        for inbound_id in inbound_ids:
            async def create_in_inbound(iid):
                nonlocal success_count, sub_id, client_uuid
                logger.info("=== Starting creation for inbound %d ===", iid)
                try:
                    # Устанавливаем remark для каждого инбаунда отдельно
                    inbound_name = _inbound_name(iid)
                    logger.info("Inbound %d: name=%s, email=%s", iid, inbound_name, email)
                    
                    # Create client dict directly to avoid py3xui Client model issues
                    client_dict = {
                        "email": email,
                        "id": client_uuid,
                        "enable": True,
                        "expiry_time": expiry_ms,
                        "limit_ip": limit_ip,
                        "total_gb": total_gb_limit,
                        "sub_id": sub_id,
                        "remark": inbound_name,
                    }
                    logger.info("Inbound %d: client_dict prepared, calling API add...", iid)
                    
                    # Use API directly with dict
                    await _api_call_with_retry(api.client.add, iid, [Client(**client_dict)])
                    logger.info("✅ Successfully added client %s to inbound %d with remark %s", email, iid, inbound_name)
                    return True
                except Exception as e:
                    err_str = str(e)
                    logger.error("❌ Inbound %d failed: %s", iid, err_str)
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
        
        # Log detailed results
        logger.info("=== Creation results for %s ===", email)
        for idx, (iid, result) in enumerate(zip(inbound_ids, results)):
            if isinstance(result, Exception):
                logger.error("Inbound %d: Exception - %s", iid, str(result))
            elif result is True:
                logger.info("Inbound %d: ✅ SUCCESS", iid)
            else:
                logger.error("Inbound %d: ❌ FAILED (returned False)", iid)
        logger.info("Total successful: %d/%d", success_count, len(inbound_ids))
        
        # Remote nodes (7,8,10,11) need sync verification - check if client actually exists on remote node
        remote_inbounds = [iid for iid in inbound_ids if iid in (7, 8, 10, 11)]
        if remote_inbounds:
            logger.info("Verifying remote node sync for inbounds %s...", remote_inbounds)
            await asyncio.sleep(2)  # Small initial delay for sync to start
            
            # Track client state to detect disappear/reappear pattern
            client_state = {iid: None for iid in remote_inbounds}
            
            for attempt in range(15):  # Check up to 15 times (more attempts for longer monitoring)
                all_remote_ok = True
                state_changed = False
                
                for iid in remote_inbounds:
                    try:
                        inbound = await _api_call_with_retry(api.inbound.get_by_id, iid)
                        if inbound and inbound.client_stats:
                            client_exists = any(c.email == email for c in inbound.client_stats)
                            
                            if client_exists != client_state[iid]:
                                state_changed = True
                                if client_exists:
                                    logger.info("🔄 Remote inbound %d: client APPEARED (attempt %d)", iid, attempt + 1)
                                else:
                                    logger.warning("🔄 Remote inbound %d: client DISAPPEARED (attempt %d)", iid, attempt + 1)
                                client_state[iid] = client_exists
                            
                            if client_exists:
                                logger.debug("Remote inbound %d: client exists (attempt %d)", iid, attempt + 1)
                            else:
                                logger.warning("Remote inbound %d: client not found (attempt %d)", iid, attempt + 1)
                                all_remote_ok = False
                        else:
                            logger.warning("Remote inbound %d: no client_stats (attempt %d)", iid, attempt + 1)
                            all_remote_ok = False
                    except Exception as e:
                        logger.warning("Remote inbound %d: check failed (attempt %d): %s", iid, attempt + 1, e)
                        all_remote_ok = False
                
                if state_changed:
                    logger.info("Client state changed on attempt %d: %s", attempt + 1, client_state)
                
                if all_remote_ok and all(client_state.values()):
                    # Client exists on all remote inbounds - wait a bit more to ensure stability
                    logger.info("All remote inbounds have client, waiting 10s to verify stability...")
                    await asyncio.sleep(10)
                    
                    # Final check
                    final_ok = True
                    for iid in remote_inbounds:
                        try:
                            inbound = await _api_call_with_retry(api.inbound.get_by_id, iid)
                            if inbound and inbound.client_stats:
                                if not any(c.email == email for c in inbound.client_stats):
                                    logger.warning("Remote inbound %d: client disappeared during stability check!", iid)
                                    final_ok = False
                            else:
                                final_ok = False
                        except Exception as e:
                            logger.warning("Final check failed for inbound %d: %s", iid, e)
                            final_ok = False
                    
                    if final_ok:
                        logger.info("✅ All remote inbounds stable - sync verified")
                        break
                    else:
                        logger.warning("Client became unstable, continuing monitoring...")
                
                if attempt < 14:  # Don't sleep on last attempt
                    delay = 5  # Fixed 5s delay for consistent monitoring
                    logger.info("Waiting %ds before next sync check (attempt %d/15)...", delay, attempt + 1)
                    await asyncio.sleep(delay)
            
            # Final state report
            logger.info("Final remote node state: %s", client_state)
            if not all(client_state.values()):
                logger.warning("⚠️ Some remote inbounds still missing client after monitoring period")

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
                    if not _is_vless_inbound(inbound):
                        logger.info(
                            "Skipping manual VLESS fallback for inbound %d (protocol=%s)",
                            iid, getattr(inbound, "protocol", "unknown"),
                        )
                        return None
                    
                    # Генерируем линк для клиента на основе данных inbound
                    try:
                        # Получаем адрес и порт из inbound
                        address = getattr(inbound, 'address', 'bymevpn.duckdns.org')
                        port = getattr(inbound, 'port', 443)
                        
                        # Генерируем VLESS линк с названием для конкретного инбаунда
                        inbound_name = _inbound_name(iid, inbound)
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
                        return None
            except Exception as e:
                logger.warning("Failed to get inbound %d: %s", iid, e)
                return None
        
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
            "uuid": client_uuid,
            "sub_id": sub_id,
            "created_new_client": True,
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
            logger.debug("get_xui_user: client %s not found", email)
            return None

        used_traffic = (client.up or 0) + (client.down or 0)
        expiry_sec = _ms_to_ts(client.expiry_time or 0)
        data_limit_bytes = (client.total_gb or 0) * (1024 ** 3)

        return {
            "used_traffic": used_traffic,
            "expire": expiry_sec,
            "data_limit": data_limit_bytes,
        }

    except ValueError as e:
        # Expected case: user not found in 3x-ui panel
        if "record not found" in str(e).lower():
            logger.debug("get_xui_user: user %d not found in panel", user_id)
        else:
            logger.warning("get_xui_user: ValueError for user=%d: %s", user_id, e)
        return None
    except Exception as e:
        logger.error("get_xui_user FAILED for user=%d: %s", user_id, e)
        return None


async def get_xui_connection_info(user_id: int) -> Dict[str, Any]:
    """Return the profile-level online state and remembered source IPs.

    3x-ui exposes online state per VPN profile, not per IP/device. Its IP API
    returns source addresses with last-seen timestamps, so callers must not
    present individual IPs as currently online devices.
    """
    api = await _get_api()
    client_api = api.client
    email = str(user_id)

    async def post_json(path: str) -> dict:
        url = client_api._url(path)
        response = await client_api._post(url, {"Accept": "application/json"}, {})
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            raise RuntimeError("3x-ui returned an unsuccessful response")
        return payload

    encoded_email = quote(email, safe="")
    ip_payload, online_payload = await asyncio.gather(
        _api_call_with_retry(post_json, f"panel/api/clients/ips/{encoded_email}"),
        _api_call_with_retry(post_json, "panel/api/clients/onlines"),
    )

    raw_ips = ip_payload.get("obj")
    if isinstance(raw_ips, str):
        import json
        try:
            raw_ips = json.loads(raw_ips)
        except (TypeError, ValueError):
            raw_ips = [raw_ips]
    if isinstance(raw_ips, dict):
        raw_ips = [raw_ips]
    if not isinstance(raw_ips, list):
        raw_ips = []

    ips = []
    for item in raw_ips:
        raw_ip = ""
        timestamp = 0
        if isinstance(item, dict):
            raw_ip = str(item.get("ip") or item.get("address") or "").strip()
            try:
                timestamp = int(item.get("timestamp") or item.get("lastSeen") or 0)
            except (TypeError, ValueError):
                timestamp = 0
        elif isinstance(item, str):
            # Current 3x-ui API format: "address (unix timestamp)".
            value = item.strip()
            if value.endswith(")") and " (" in value:
                raw_ip, raw_time = value.rsplit(" (", 1)
                try:
                    timestamp = int(raw_time[:-1])
                except ValueError:
                    timestamp = 0
            else:
                raw_ip = value
        try:
            normalized_ip = str(ip_address(raw_ip))
        except ValueError:
            continue
        ips.append({"ip": normalized_ip, "last_seen": timestamp})

    raw_online = online_payload.get("obj")
    is_online = isinstance(raw_online, list) and email in {str(value) for value in raw_online}
    ips.sort(key=lambda row: row["last_seen"], reverse=True)
    return {"is_online": is_online, "ips": ips}


async def clear_xui_client_ips(user_id: int) -> bool:
    """Clear all remembered source IPs for this user's shared VPN profile."""
    api = await _get_api()
    client_api = api.client
    email = quote(str(user_id), safe="")
    url = client_api._url(f"panel/api/clients/clearIps/{email}")

    async def clear() -> bool:
        response = await client_api._post(url, {"Accept": "application/json"}, {})
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            raise RuntimeError("3x-ui could not clear the IP list")
        return True

    return await _api_call_with_retry(clear)


async def get_xui_devices(user_id: int) -> Dict[str, Any]:
    """Read the registered Happ HWID devices and actual device limits."""
    api = await _get_api()
    client_api = api.client
    email = quote(str(user_id), safe="")

    async def request_json(method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = client_api._url(path)
        if method == "POST":
            response = await client_api._post(url, {"Accept": "application/json"}, payload or {})
        else:
            response = await client_api._get(url, {"Accept": "application/json"})
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError("3x-ui device API returned an unsuccessful response")
        return data

    client_data, hwid_data = await asyncio.gather(
        _api_call_with_retry(request_json, "GET", f"panel/api/clients/get/{email}"),
        _api_call_with_retry(request_json, "POST", f"panel/api/clients/hwids/{email}", {}),
    )
    client_obj = client_data.get("obj") or {}
    client_obj = client_obj.get("client") if isinstance(client_obj, dict) else None
    if not isinstance(client_obj, dict):
        raise RuntimeError("3x-ui client details are unavailable")

    raw_devices = hwid_data.get("obj")
    if not isinstance(raw_devices, list):
        raw_devices = []
    devices = []
    for item in raw_devices:
        if not isinstance(item, dict):
            continue
        try:
            device_id = int(item.get("id"))
            first_seen = max(0, int(item.get("firstSeen") or 0) // 1000)
            last_seen = max(0, int(item.get("lastSeen") or 0) // 1000)
        except (TypeError, ValueError):
            continue
        devices.append({
            "id": device_id,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "model": str(item.get("deviceModel") or "").strip()[:120],
            "os": str(item.get("deviceOs") or "").strip()[:80],
            "os_version": str(item.get("osVersion") or "").strip()[:80],
            "user_agent": str(item.get("userAgent") or "").strip()[:160],
        })
    devices.sort(key=lambda item: item["last_seen"], reverse=True)
    return {
        "devices": devices,
        "limit_hwid": max(0, int(client_obj.get("limitHwid") or 0)),
        "limit_ip": max(0, int(client_obj.get("limitIp") or 0)),
    }


async def get_xui_online_user_ids() -> set[int]:
    """Return numeric Telegram IDs with an active VPN connection."""
    api = await _get_api()
    client_api = api.client

    async def request() -> set[int]:
        response = await client_api._post(
            client_api._url("panel/api/clients/onlines"), {"Accept": "application/json"}, {},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            raise RuntimeError("3x-ui online-clients API returned an unsuccessful response")
        raw = payload.get("obj")
        result = set()
        if isinstance(raw, list):
            for value in raw:
                try:
                    result.add(int(value))
                except (TypeError, ValueError):
                    continue
        return result

    return await _api_call_with_retry(request)


async def delete_xui_hwid_device(user_id: int, device_id: int) -> bool:
    """Remove one registered Happ device; this does not revoke its existing config."""
    api = await _get_api()
    client_api = api.client
    email = quote(str(user_id), safe="")
    device_id = int(device_id)

    async def remove() -> bool:
        url = client_api._url(f"panel/api/clients/hwids/{email}/{device_id}")
        response = await client_api._request_with_retry(
            "DELETE", url, {"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            raise RuntimeError("3x-ui could not remove the device")
        return True

    return await _api_call_with_retry(remove)


async def rotate_xui_client_credentials(user_id: int) -> Optional[Dict[str, str]]:
    """Rotate subscription id and UUID while preserving all other panel settings."""
    api = await _get_api()
    client_api = api.client
    email = str(user_id)
    encoded_email = quote(email, safe="")
    old_response = await _api_call_with_retry(
        client_api._get, client_api._url(f"panel/api/clients/get/{encoded_email}"),
        {"Accept": "application/json"},
    )
    old_response.raise_for_status()
    old_data = old_response.json()
    current = ((old_data.get("obj") or {}).get("client") or {}) if isinstance(old_data, dict) else {}
    if not isinstance(current, dict) or not current.get("uuid") or not current.get("subId"):
        raise RuntimeError("Current VPN credentials are unavailable")

    old_uuid = str(current["uuid"])
    old_sub_id = str(current["subId"])
    new_uuid = str(uuid_lib.uuid4())
    new_sub_id = uuid_lib.uuid4().hex[:16]
    updated = dict(current)
    updated["uuid"] = new_uuid
    updated["subId"] = new_sub_id
    url = client_api._url(f"panel/api/clients/update/{encoded_email}")

    async def update(payload: dict) -> None:
        response = await client_api._post(url, {"Accept": "application/json"}, payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError("3x-ui rejected credential rotation")

    await _api_call_with_retry(update, updated)

    # Verify the panel committed both credentials before changing the bot DB.
    try:
        verify = await _api_call_with_retry(
            client_api._get, client_api._url(f"panel/api/clients/get/{encoded_email}"),
            {"Accept": "application/json"},
        )
        verify.raise_for_status()
        verify_obj = ((verify.json().get("obj") or {}).get("client") or {})
    except Exception:
        verify_obj = {}
    if verify_obj.get("uuid") != new_uuid or verify_obj.get("subId") != new_sub_id:
        # Best-effort rollback, preserving the same full client payload.
        rollback = dict(updated)
        rollback["uuid"] = old_uuid
        rollback["subId"] = old_sub_id
        try:
            await _api_call_with_retry(update, rollback)
        except Exception:
            logger.critical("Credential rotation rollback failed for user %s", email)
            raise CredentialRotationRollbackError("credential rotation rollback failed")
        raise RuntimeError("3x-ui did not confirm credential rotation")

    return {
        "uuid": new_uuid,
        "subscription_url": _build_sub_url(new_sub_id),
        "previous_uuid": old_uuid,
        "previous_sub_id": old_sub_id,
    }


async def restore_xui_client_credentials(user_id: int, previous_uuid: str, previous_sub_id: str) -> bool:
    """Compensating rollback used if the bot DB cannot save a successful rotation."""
    api = await _get_api()
    client_api = api.client
    email = quote(str(user_id), safe="")
    response = await _api_call_with_retry(
        client_api._get, client_api._url(f"panel/api/clients/get/{email}"),
        {"Accept": "application/json"},
    )
    response.raise_for_status()
    obj = response.json().get("obj") or {}
    current = obj.get("client") if isinstance(obj, dict) else None
    if not isinstance(current, dict):
        return False
    current = dict(current)
    current["uuid"] = previous_uuid
    current["subId"] = previous_sub_id
    url = client_api._url(f"panel/api/clients/update/{email}")

    async def update() -> None:
        result = await client_api._post(url, {"Accept": "application/json"}, current)
        result.raise_for_status()
        data = result.json()
        if not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError("3x-ui rejected credential rollback")

    await _api_call_with_retry(update)
    verify = await _api_call_with_retry(
        client_api._get, client_api._url(f"panel/api/clients/get/{email}"),
        {"Accept": "application/json"},
    )
    verify.raise_for_status()
    restored = ((verify.json().get("obj") or {}).get("client") or {})
    return restored.get("uuid") == previous_uuid and restored.get("subId") == previous_sub_id


async def delete_xui_user(user_id: int) -> bool:
    email = str(user_id)
    inbound_ids = XUI_INBOUND_IDS  # Use configured inbound IDs
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


async def update_xui_user(
    user_id: int,
    days: int = 0,
    limit_ip: Optional[int] = None,
    new_expiry_ms: Optional[int] = None,
    enable: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """
    Safely update client in 3x-ui WITHOUT deleting or changing UUID/sub_id.
    Updates expiry_time, limit_ip (device limit), and enable state.
    """
    email = str(user_id)
    logger.info("update_xui_user: email=%s days=%d limit_ip=%s new_expiry_ms=%s enable=%s", email, days, limit_ip, new_expiry_ms, enable)

    try:
        api = await _get_api()
        client_uuid = await _find_client_uuid(api, email)

        if not client_uuid:
            logger.info("update_xui_user: client %s not found in 3x-ui", email)
            return None

        # Получаем текущие настройки клиента
        client = None
        try:
            client = await _api_call_with_retry(api.client.get_by_email, email)
        except Exception as e:
            logger.warning("update_xui_user: failed to get client settings for %s: %s", email, e)

        # UUID alone is not enough to safely update an existing client: without
        # the current object we cannot preserve its expiry, device limit, traffic
        # cap, or subscription ID. Fail closed instead of resetting fields.
        if not client or not getattr(client, "uuid", None):
            logger.error("update_xui_user: refusing partial update for %s; full client details unavailable", email)
            return None

        current_limit_ip = client.limit_ip if (client and client.limit_ip) else 2
        current_total_gb = client.total_gb if (client and client.total_gb) else 0
        current_sub_id = client.sub_id if (client and client.sub_id) else ""
        sub_id = current_sub_id or client_uuid.replace("-", "")[:16]

        final_limit_ip = limit_ip if (limit_ip is not None and limit_ip > 0) else current_limit_ip

        import time
        now_ms = int(time.time() * 1000)
        current_expiry_ms = (client.expiry_time or 0) if client else 0

        if new_expiry_ms is not None:
            final_expiry_ms = new_expiry_ms
        else:
            base_ms = max(current_expiry_ms, now_ms)
            final_expiry_ms = base_ms + days * 86400 * 1000

        is_enabled = enable if enable is not None else True

        updated = Client(
            email=email,
            id=client_uuid,
            uuid=client_uuid,
            enable=is_enabled,
            expiry_time=final_expiry_ms,
            limit_ip=final_limit_ip,
            total_gb=current_total_gb,
            sub_id=sub_id,
        )
        await _api_call_with_retry(api.client.update, client_uuid, updated)
        logger.info(
            "update_xui_user: %s updated (uuid=%s, limit_ip=%d, expiry=%s)",
            email,
            client_uuid,
            final_limit_ip,
            datetime.fromtimestamp(final_expiry_ms / 1000).strftime("%d.%m.%Y"),
        )

        subscription_url = _build_sub_url(sub_id)
        return {
            "subscription_url": subscription_url,
            "username": email,
            "uuid": client_uuid,
            "sub_id": sub_id,
            "expiry_time": final_expiry_ms,
            "limit_ip": final_limit_ip,
        }

    except Exception as e:
        logger.exception("update_xui_user FAILED for user=%d: %s", user_id, e)
        return None


async def update_xui_user_expiry(user_id: int, days: int, limit_ip: Optional[int] = None) -> bool:
    """Backward-compatible wrapper to update client expiry and optional limit_ip."""
    res = await update_xui_user(user_id, days=days, limit_ip=limit_ip)
    return res is not None


async def get_xui_client_details(user_id: int) -> Optional[Dict[str, Any]]:
    """Get full details of client in 3x-ui including UUID and sub_id."""
    email = str(user_id)
    try:
        api = await _get_api()
        client = await _api_call_with_retry(api.client.get_by_email, email)
        if not client or not client.uuid:
            return None
        sub_id = client.sub_id or client.uuid.replace("-", "")[:16]
        return {
            "uuid": client.uuid,
            "sub_id": sub_id,
            "email": email,
            "limit_ip": client.limit_ip or 2,
            "expiry_time": client.expiry_time or 0,
            "subscription_url": _build_sub_url(sub_id),
            "enable": client.enable,
        }
    except Exception as e:
        logger.debug("get_xui_client_details failed for user %d: %s", user_id, e)
        return None


async def get_user_subscription_links(user_id: int) -> Optional[List[str]]:
    email = str(user_id)
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
            # Fallback across exactly the inbounds used to build subscriptions.
            for inbound_id in XUI_INBOUND_IDS:
                try:
                    inbound = await _api_call_with_retry(api.inbound.get_by_id, inbound_id)
                    for c in (inbound.client_stats or []) if inbound else []:
                        if c.email == email and c.sub_id:
                            sub_id = c.sub_id
                            break
                except Exception as e:
                    logger.debug("get_user_subscription_links: configured inbound %d failed for %s: %s", inbound_id, email, e)
                if sub_id:
                    break

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


def get_api_client():
    """Stub для совместимости с main.py (3x-ui не требует pre-init)"""
    return None
