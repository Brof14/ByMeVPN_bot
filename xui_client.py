"""
3x-ui API wrapper — управление VPN-пользователями через панель 3x-ui.
Использует py3xui AsyncApi (HTTP API).
"""

import logging
import uuid as uuid_lib
import secrets
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
    XUI_TLS_VERIFY,
    XUI_URL,
    XUI_USERNAME,
)

logger = logging.getLogger(__name__)

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
    if not XUI_URL:
        errors.append("XUI_URL не указан: без публичного URL нельзя выдать рабочую подписку")
    else:
        public_url = urlparse(XUI_URL)
        if public_url.scheme not in ("http", "https") or not public_url.netloc:
            errors.append("XUI_URL должен быть полным URL с http:// или https://")
    if not XUI_USERNAME:
        errors.append("XUI_USERNAME не указан")
    if not XUI_PASSWORD:
        errors.append("XUI_PASSWORD не указан")
    if not XUI_INBOUND_IDS:
        errors.append("XUI_INBOUND_IDS не указан")
    elif any(inbound_id <= 0 for inbound_id in XUI_INBOUND_IDS):
        errors.append("Все значения XUI_INBOUND_IDS должны быть положительными ID")
    elif len(set(XUI_INBOUND_IDS)) != len(XUI_INBOUND_IDS):
        errors.append("XUI_INBOUND_IDS содержит повторяющиеся ID")
    if not XUI_SUB_PATH.strip("/"):
        errors.append("XUI_SUB_PATH не должен быть пустым")
    return (False, "; ".join(errors)) if errors else (True, "")


async def test_xui_connection() -> tuple[bool, str]:
    """Verify panel authentication and every configured inbound, not just the panel root."""
    is_valid, err = validate_xui_config()
    if not is_valid:
        return False, f"Ошибка конфигурации: {err}"

    try:
        api = await _get_api()
        working: list[int] = []
        missing: list[int] = []

        # A reachable panel is not enough: if an inbound ID is stale or inaccessible,
        # accepting payments could produce subscriptions missing one or more servers.
        for inbound_id in XUI_INBOUND_IDS:
            try:
                inbound = await _api_call_with_retry(api.inbound.get_by_id, inbound_id)
                if inbound is not None:
                    working.append(inbound_id)
                else:
                    missing.append(inbound_id)
            except Exception as inbound_error:
                logger.error(
                    "Configured 3x-ui inbound %d is not reachable: %s",
                    inbound_id,
                    str(inbound_error)[:160],
                )
                missing.append(inbound_id)

        if missing:
            return False, (
                "Не все настроенные инбаунды доступны. "
                f"Работают: {working or 'нет'}; недоступны: {missing}. "
                "Проверьте XUI_INBOUND_IDS и доступность панели."
            )

        return True, (
            "Соединение с 3x-ui успешно. "
            f"Проверены все настроенные инбаунды: {working}"
        )
    except Exception as e:
        logger.exception("test_xui_connection failed: %s", e)
        return False, f"Ошибка подключения к 3x-ui API: {str(e)}"


async def _find_client_uuid(api: AsyncApi, email: str) -> Optional[str]:
    """Find a client UUID via the panel-wide API or any configured inbound."""
    try:
        client = await _api_call_with_retry(api.client.get_by_email, email)
        if client and client.uuid:
            return client.uuid
    except Exception as e:
        logger.debug("Panel-wide client lookup failed for %s (%s)", email, type(e).__name__)

    async def search_inbound(inbound_id: int) -> Optional[str]:
        try:
            inbound = await _api_call_with_retry(api.inbound.get_by_id, inbound_id)
            for client in getattr(inbound, "client_stats", None) or []:
                if client.email == email and client.uuid:
                    return client.uuid
        except Exception as e:
            logger.debug("Client lookup in inbound %d failed (%s)", inbound_id, type(e).__name__)
        return None

    results = await asyncio.gather(
        *(search_inbound(inbound_id) for inbound_id in XUI_INBOUND_IDS),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, str) and result:
            return result
    logger.warning("Client %s not found in configured inbounds", email)
    return None


async def _find_client_sub_id(api: AsyncApi, email: str) -> Optional[str]:
    """Find an existing subscription ID without changing legacy client identities."""
    try:
        client = await _api_call_with_retry(api.client.get_by_email, email)
        if client and client.sub_id:
            return client.sub_id
    except Exception as e:
        logger.debug("Panel-wide sub_id lookup failed for %s (%s)", email, type(e).__name__)

    async def search_inbound(inbound_id: int) -> Optional[str]:
        try:
            inbound = await _api_call_with_retry(api.inbound.get_by_id, inbound_id)
            for client in getattr(inbound, "client_stats", None) or []:
                if client.email == email and client.sub_id:
                    return client.sub_id
        except Exception as e:
            logger.debug("sub_id lookup in inbound %d failed (%s)", inbound_id, type(e).__name__)
        return None

    results = await asyncio.gather(
        *(search_inbound(inbound_id) for inbound_id in XUI_INBOUND_IDS),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, str) and result:
            return result

    # Legacy clients without a panel sub_id may already use this historical URL.
    # Preserve that identity; all newly-created clients get an independent random ID.
    client_uuid = await _find_client_uuid(api, email)
    return client_uuid.replace("-", "")[:16] if client_uuid else None


async def create_xui_user(
    user_id: int,
    days: int,
    data_limit_gb: int = 0,
    limit_ip: int = 0,
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
    expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
    total_gb_limit = data_limit_gb  # 0 = безлимит
    inbound_ids = XUI_INBOUND_IDS  # Use configured inbound IDs

    logger.info(
        "create_xui_user: email=%s days=%d limit_ip=%d inbounds=%s",
        email, days, limit_ip, len(inbound_ids),
    )

    try:
        api = await _get_api()

        # Шаг 1: Проверяем, существует ли уже клиент в 3x-ui
        existing_uuid = await _find_client_uuid(api, email)
        
        if existing_uuid:
            logger.info("Client %s already exists in 3x-ui; preserving existing identity and updating", email)
            update_res = await update_xui_user(user_id, days=days, limit_ip=limit_ip)
            if update_res:
                return update_res
            logger.error("Existing client %s could not be updated; refusing to create duplicate inbounds", email)
            return None
        else:
            logger.info("No existing client found for %s, creating new", email)
            client_uuid = str(uuid_lib.uuid4())
            sub_id = secrets.token_hex(8)

        # Шаг 2: Создаем клиента в инбаундах (parallel)
        logger.info("Adding client %s in inbounds %s", email, inbound_ids)

        # Названия для инбаундов (используем при создании клиентов и генерации ссылок)
        inbound_names = {iid: "ByMeVPN-" + str(iid) for iid in inbound_ids}

        success_count = 0
        create_tasks = []
        
        for inbound_id in inbound_ids:
            async def create_in_inbound(iid):
                nonlocal success_count, sub_id, client_uuid
                logger.info("=== Starting creation for inbound %d ===", iid)
                try:
                    # Устанавливаем remark для каждого инбаунда отдельно
                    inbound_name = inbound_names.get(iid, "🇳🇱Netherlands")
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
                                        logger.debug("Found existing client identity in inbound %d client_stats", iid)
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
        
        # Do not block the payment path on remote-node replication polling.
        # Inbound IDs are taken exclusively from validated configuration.
        remote_inbounds = []
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

        if success_count != len(inbound_ids):
            failed_ids = [
                iid for iid, result in zip(inbound_ids, results)
                if result is not True
            ]
            logger.critical(
                "Provisioning incomplete for user %s: successful=%d/%d failed_inbounds=%s; cleaning partial client",
                email, success_count, len(inbound_ids), failed_ids,
            )
            cleanup = await asyncio.gather(
                *(_api_call_with_retry(api.client.delete, iid, client_uuid)
                  for iid, result in zip(inbound_ids, results) if result is True),
                return_exceptions=True,
            )
            if any(isinstance(item, Exception) for item in cleanup):
                logger.critical("Partial provisioning cleanup was incomplete for user %s", email)
            return None

        # Verify the client identity after all configured inbound writes succeeded.
        logger.info("Verifying client creation and getting uuid for %s", email)
        try:
            existing_client = await _api_call_with_retry(api.client.get_by_email, email)
            if existing_client and existing_client.uuid:
                client_uuid = existing_client.uuid
                logger.debug("Using UUID verified by panel for %s", email)
            else:
                logger.warning("Client created but panel did not return a UUID; retaining generated identity")
        except Exception as e:
            logger.warning("Failed to verify client: %s, using generated uuid", e)

        # The canonical subscription URL is generated by 3x-ui. Do not synthesize
        # direct VLESS links: transports may be Reality, TLS, Hysteria, or another mode.
        vless_links: list[str] = []

        # Генерируем ссылку на подписку
        subscription_url = _build_sub_url(sub_id)
        logger.debug("Subscription URL prepared for %s", email)

        return {
            "subscription_url": subscription_url,
            "username": email,
            "vless_links": vless_links,
            "uuid": client_uuid,
            "sub_id": sub_id,
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
                    logger.info("Deleting client %s from inbound %d", email, iid)
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
            logger.debug("update_xui_user: failed to get client settings: %s", e)

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
            "update_xui_user: %s updated (limit_ip=%d, expiry=%s)",
            email,
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
    inbound_id = XUI_INBOUND_IDS[0] if XUI_INBOUND_IDS else 0  # configured primary inbound
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
        logger.debug("get_user_subscription_links: returning subscription URL for user %s (URL redacted)", email)
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
