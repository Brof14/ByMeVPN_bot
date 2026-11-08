"""Verified, owner-scoped rotation of a user's shared 3x-ui subscription."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from urllib.parse import quote

from database import update_user_key_credentials
from xui_client import _api_call_with_retry, _get_api, _build_sub_url

logger = logging.getLogger(__name__)
_locks: dict[int, asyncio.Lock] = {}


class ReissueOutcomeUnknown(RuntimeError):
    """Panel changed but its compensating rollback could not be verified."""


def _client_from_response(response) -> dict:
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError("panel did not confirm client operation")
    obj = data.get("obj")
    client = obj.get("client") if isinstance(obj, dict) else None
    if not isinstance(client, dict):
        raise RuntimeError("panel client data unavailable")
    return client


async def _get_panel_client(client_api, email: str) -> dict:
    response = await client_api._get(client_api._url(f"panel/api/clients/get/{email}"), {"Accept": "application/json"})
    return _client_from_response(response)


async def _set_panel_client(client_api, email: str, payload: dict) -> None:
    response = await client_api._post(
        client_api._url(f"panel/api/clients/update/{email}"),
        {"Accept": "application/json"}, payload,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError("panel did not confirm client update")


def _update_payload(panel_client: dict, protocol_uuid: str, sub_id: str) -> dict:
    """Convert the GET response to the same Client shape used by xui_client."""
    payload = dict(panel_client)
    payload.update({"id": protocol_uuid, "uuid": protocol_uuid, "subId": sub_id})
    allowed_ips = payload.get("allowedIPs")
    if isinstance(allowed_ips, str):
        payload["allowedIPs"] = [part.strip() for part in allowed_ips.split(",") if part.strip()]
    elif allowed_ips is not None and (
        not isinstance(allowed_ips, list)
        or not all(isinstance(item, str) for item in allowed_ips)
    ):
        raise RuntimeError("panel client allowed IPs have an unsupported format")
    return payload


def _same_profile(actual: dict, expected: dict, uuid_value: str, sub_id: str) -> bool:
    """Compare identity and access controls, ignoring counters changed by traffic."""
    return (
        str(actual.get("email") or "") == str(expected.get("email") or "")
        and str(actual.get("uuid") or "") == uuid_value
        and str(actual.get("subId") or "") == sub_id
        and bool(actual.get("enable")) == bool(expected.get("enable"))
        and int(actual.get("expiryTime") or 0) == int(expected.get("expiryTime") or 0)
        and int(actual.get("limitIp") or 0) == int(expected.get("limitIp") or 0)
        and int(actual.get("limitHwid") or 0) == int(expected.get("limitHwid") or 0)
        and int(actual.get("totalGB") or 0) == int(expected.get("totalGB") or 0)
    )


async def _restore_panel(client_api, email: str, old_payload: dict, old_uuid: str, old_sub_id: str) -> bool:
    try:
        restore_payload = _update_payload(old_payload, old_uuid, old_sub_id)
        await _set_panel_client(client_api, email, restore_payload)
        actual = await _get_panel_client(client_api, email)
        return _same_profile(actual, old_payload, old_uuid, old_sub_id)
    except Exception as exc:
        logger.error("Panel rollback after subscription reissue failed (%s)", type(exc).__name__)
        return False


async def _reissue_locked(user_id: int, expected_uuid: str) -> dict | None:
    api = await _get_api()
    client_api = api.client
    email = quote(str(int(user_id)), safe="")
    old_payload = await _api_call_with_retry(_get_panel_client, client_api, email)
    old_uuid = str(old_payload.get("uuid") or "").strip()
    old_sub_id = str(old_payload.get("subId") or "").strip()
    try:
        uuid.UUID(old_uuid)
    except (ValueError, AttributeError):
        raise RuntimeError("panel client identity is invalid") from None
    if not expected_uuid or old_uuid != expected_uuid:
        raise RuntimeError("subscription changed after confirmation was opened")
    if str(old_payload.get("email") or "") != str(int(user_id)):
        raise RuntimeError("panel client ownership could not be verified")
    if not bool(old_payload.get("enable")) or int(old_payload.get("expiryTime") or 0) <= int(time.time() * 1000):
        raise RuntimeError("panel subscription is not active")
    if not old_sub_id:
        raise RuntimeError("panel subscription ID is unavailable")

    new_uuid = str(uuid.uuid4())
    new_sub_id = new_uuid.replace("-", "")[:16]
    new_payload = _update_payload(old_payload, new_uuid, new_sub_id)

    try:
        await _api_call_with_retry(_set_panel_client, client_api, email, new_payload)
        current = await _api_call_with_retry(_get_panel_client, client_api, email)
        if not _same_profile(current, new_payload, new_uuid, new_sub_id):
            raise RuntimeError("panel verification did not match the requested rotation")
        updated = await update_user_key_credentials(user_id, new_uuid, _build_sub_url(new_sub_id))
        if not updated:
            raise RuntimeError("stored subscription links could not be updated")
        return {"updated": True}
    except Exception as exc:
        if not await _restore_panel(client_api, email, old_payload, old_uuid, old_sub_id):
            logger.critical("Subscription reissue rollback could not be verified for account %s", email)
            raise ReissueOutcomeUnknown("panel rollback is unconfirmed") from exc
        raise


async def reissue_user_subscription(user_id: int, *, expected_uuid: str) -> dict | None:
    """Rotate panel UUID/subId only after validation and persist all user links.

    A local lock prevents duplicate callback deliveries from rotating twice in
    one bot process. The operation fails closed if the panel or DB confirmation
    is unavailable; panel state is restored when persistence cannot complete.
    """
    user_id = int(user_id)
    lock = _locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        try:
            return await _reissue_locked(user_id, expected_uuid)
        except ReissueOutcomeUnknown:
            logger.critical("Subscription reissue left panel state unconfirmed for account %s", user_id)
            return {"updated": False, "state_unknown": True}
        except Exception as exc:
            logger.warning("Subscription reissue did not complete (%s)", type(exc).__name__)
            return None
