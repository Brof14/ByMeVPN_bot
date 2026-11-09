"""Safe recovery of a missing local key row from a matching 3x-ui client."""
from __future__ import annotations

import time
from urllib.parse import urlsplit


async def sync_missing_local_key_from_panel(user_id: int) -> str:
    """Import a DB key only when 3x-ui has a client named by the exact Telegram ID.

    Existing local key rows and all panel/device records are left untouched.
    """
    from database import get_user_keys, import_panel_key_if_missing
    from xui_client import get_xui_client_details

    user_id = int(user_id)
    if user_id <= 0:
        return "not_found"
    if await get_user_keys(user_id):
        return "already_present"

    details = await get_xui_client_details(user_id, strict=True)
    if not isinstance(details, dict) or not details.get("uuid"):
        return "not_found"
    if details.get("enable") is not True:
        return "panel_disabled"

    subscription_url = str(details.get("subscription_url") or "").strip()
    parsed = urlsplit(subscription_url)
    if (len(subscription_url) > 2048 or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        return "invalid_panel_link"
    expiry = int(details.get("expiry_time") or 0) // 1000
    if expiry <= 0:
        return "not_found"
    device_limit = int(details.get("limit_hwid") or 0)
    # Older panel clients may have no HWID limit (0) or a legacy tier. Preserve
    # the panel's actual setting so admins can see the key and choose 2/5/10.
    if device_limit < 0 or device_limit > 1000:
        return "unsupported_device_limit"

    days = max(1, (expiry - int(time.time()) + 86_399) // 86_400)
    imported = await import_panel_key_if_missing(
        user_id=user_id,
        key=subscription_url,
        remark=f"ByMeVPN_{user_id}",
        uuid=str(details["uuid"]),
        days=days,
        limit_ip=device_limit,
        expiry=expiry,
    )
    return "imported" if imported else "already_present"
