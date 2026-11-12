"""Read-only subscription reconciliation; payment rights != a working tunnel.

A local paid term can exist while panel provisioning is unfinished. Never
invent an active tunnel from either SQLite dates or HTTP /healthz.
"""
from __future__ import annotations

import time
import uuid

import database


def _panel_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"true", "1", "yes"}


async def inspect_subscription(user_id: int, *, now: int | None = None) -> dict:
    from xui_client import get_xui_client_details

    current_time = int(time.time()) if now is None else int(now)
    keys = await database.get_user_keys(int(user_id))
    active_keys = [key for key in keys if int(key.get("expiry") or 0) > current_time]
    latest = max(active_keys, key=lambda key: int(key.get("expiry") or 0)) if active_keys else None
    local_expiry = int(latest.get("expiry") or 0) if latest else 0
    local_status = "active" if latest else ("expired" if keys else "none")
    outcome = {
        "subscription_status": local_status,
        "paid_until": local_expiry or None,
        "panel_status": "unknown",
        "panel_consistent": None,
        "configuration_status": "unknown",
        "connection_test": "not_performed",
        "repair_required": False,
    }
    try:
        panel = await get_xui_client_details(int(user_id), strict=True)
    except Exception:
        outcome["panel_status"] = "unavailable"
        outcome["configuration_status"] = "unavailable"
        return outcome

    if not panel:
        outcome["panel_status"] = "missing"
        outcome["panel_consistent"] = not bool(latest)
        outcome["configuration_status"] = "unavailable"
        outcome["repair_required"] = bool(latest)
        return outcome

    if not _panel_enabled(panel.get("enable")):
        panel_status = "disabled"
    elif int(panel.get("expiry_time") or 0) <= current_time * 1000:
        panel_status = "expired"
    else:
        panel_status = "active"
    outcome["panel_status"] = panel_status
    outcome["configuration_status"] = "available" if panel_status == "active" and panel.get("subscription_url") else "unavailable"

    if latest is None:
        # Panel-only access needs a controlled recovery rather than a second key.
        if panel_status == "active":
            outcome["repair_required"] = True
            outcome["panel_consistent"] = False
        else:
            outcome["panel_consistent"] = True
        return outcome

    try:
        actual_uuid = str(uuid.UUID(str(panel.get("uuid") or "")))
        stored_uuid = str(uuid.UUID(str(latest.get("uuid") or "")))
    except (ValueError, TypeError, AttributeError):
        outcome["panel_consistent"] = False
        outcome["repair_required"] = True
        return outcome

    expiry_diff = abs(int(panel.get("expiry_time") or 0) // 1000 - local_expiry)
    outcome["panel_consistent"] = (
        panel_status == "active" and actual_uuid == stored_uuid and expiry_diff <= 120
    )
    outcome["repair_required"] = not outcome["panel_consistent"]
    return outcome
