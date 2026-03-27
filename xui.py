"""
3x-UI API wrapper — direct httpx calls, SSL verification disabled.

Uses the 3x-UI REST API directly instead of py3xui to give full control
over timeouts, retries and TLS settings.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional
import urllib.parse

import httpx

from config import XUI_HOST, XUI_USERNAME, XUI_PASSWORD, INBOUND_ID

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0   # seconds between retries
_TIMEOUT = 30.0       # seconds per request


def _client() -> httpx.AsyncClient:
    """Create httpx client with SSL verification disabled."""
    return httpx.AsyncClient(
        verify=False,
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


async def _login(client: httpx.AsyncClient) -> None:
    """Authenticate with 3x-UI panel and store session cookie."""
    url = f"{XUI_HOST}/login"
    payload = {"username": XUI_USERNAME, "password": XUI_PASSWORD}

    for content_type, body in [("json", payload), ("data", payload)]:
        try:
            resp = await client.post(url, **{content_type: body})
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if data.get("success"):
                        logger.debug("3x-UI login OK (method=%s)", content_type)
                        return
                    logger.warning("3x-UI login response: %s", data)
                except Exception:
                    if resp.cookies:
                        logger.debug("3x-UI login OK (cookie, method=%s)", content_type)
                        return
        except Exception as e:
            logger.debug("Login attempt failed (method=%s): %s", content_type, e)

    raise RuntimeError(
        f"3x-UI login failed: host={XUI_HOST} user={XUI_USERNAME}"
    )


async def _with_retry(coro_factory, retries: int = _MAX_RETRIES):
    """Call an async coroutine factory up to `retries` times."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return await coro_factory()
        except Exception as e:
            last_exc = e
            if attempt < retries:
                logger.warning(
                    "Attempt %d/%d failed: %s — retrying in %.1fs",
                    attempt, retries, e, _RETRY_DELAY,
                )
                await asyncio.sleep(_RETRY_DELAY)
            else:
                logger.error("All %d attempts failed. Last error: %s", retries, e)
    raise last_exc


async def create_client(email: str, days: int, limit_ip: int = 1) -> Optional[str]:
    """
    Create a new VLESS client in 3x-UI inbound.
    limit_ip — max simultaneous device connections (1, 2 or 5).
    Returns the client UUID on success, None on failure.
    """

    async def _attempt():
        client_id = str(uuid.uuid4())
        expiry_ms = int(
            (datetime.now() + timedelta(days=days)).timestamp() * 1000
        )
        client_obj = {
            "id": client_id,
            "flow": "xtls-rprx-vision",
            "email": email,
            "limitIp": max(1, int(limit_ip)),  # enforce minimum 1, use plan value
            "totalGB": 0,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": "",
            "subId": "",
        }
        payload = {
            "id": INBOUND_ID,
            "settings": json.dumps({"clients": [client_obj]}),
        }
        async with _client() as http:
            await _login(http)
            url = f"{XUI_HOST}/panel/api/inbounds/addClient"
            resp = await http.post(url, json=payload)
            logger.info(
                "addClient → status=%d  body=%s",
                resp.status_code, resp.text[:300],
            )
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception:
                raise RuntimeError(f"Non-JSON response: {resp.text[:200]}")
            if not data.get("success"):
                raise RuntimeError(f"3x-UI addClient failed: {data.get('msg', data)}")
        logger.info("Client created: email=%s uuid=%s days=%d limit_ip=%d", email, client_id, days, limit_ip)
        return client_id

    try:
        return await _with_retry(_attempt)
    except Exception as e:
        logger.error(
            "create_client permanently failed for '%s': %s | "
            "XUI_HOST=%s XUI_USERNAME=%s INBOUND_ID=%d",
            email, e, XUI_HOST, XUI_USERNAME, INBOUND_ID,
        )
        return None


async def delete_client(client_uuid: str) -> bool:
    """Delete a client from 3x-UI by UUID. Returns True on success."""

    async def _attempt():
        async with _client() as http:
            await _login(http)
            url = f"{XUI_HOST}/panel/api/inbounds/{INBOUND_ID}/delClient/{client_uuid}"
            resp = await http.post(url)
            logger.info(
                "delClient(%s) → status=%d  body=%s",
                client_uuid, resp.status_code, resp.text[:200],
            )
            resp.raise_for_status()
            try:
                data = resp.json()
                if not data.get("success"):
                    raise RuntimeError(f"delClient failed: {data.get('msg', data)}")
            except Exception:
                pass  # empty body = success on some versions
        return True

    try:
        return await _with_retry(_attempt)
    except Exception as e:
        logger.error("delete_client permanently failed for %s: %s", client_uuid, e)
        return False


def build_vless_link(client_uuid: str, remark: str = "ByMeVPN") -> str:
    """Build a VLESS connection link using config values from .env."""
    from config import (
        REALITY_HOST, REALITY_PORT,
        REALITY_SNI, REALITY_FP,
        REALITY_PBK, REALITY_SID,
    )
    params = {
        "type": "tcp",
        "security": "reality",
        "pbk": REALITY_PBK,
        "fp": REALITY_FP,
        "sni": REALITY_SNI,
        "sid": REALITY_SID,
        "flow": "xtls-rprx-vision",
        "encryption": "none",
    }
    qs = "&".join(
        f"{k}={urllib.parse.quote(str(v), safe='')}"
        for k, v in params.items()
        if v
    )
    tag = urllib.parse.quote(remark, safe="")
    return f"vless://{client_uuid}@{REALITY_HOST}:{REALITY_PORT}?{qs}#{tag}"
