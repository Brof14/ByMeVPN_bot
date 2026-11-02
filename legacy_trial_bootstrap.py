"""Bounded first-device trial bootstrap for the bind-mounted legacy API.

The adapter keeps the Telegram trial claim as the single source of truth and
uses the installed 3x-ui API. A bootstrap token is account/device bound, lives
for at most ten minutes, and is replaced with a normal entitlement as soon as
the panel confirms the device fingerprint.
"""
import asyncio
import base64
import hashlib
import hmac
import re
import time
from typing import Any
from urllib.parse import quote, urlparse

import aiosqlite
import httpx
from fastapi import Depends, HTTPException, Request


BOOTSTRAP_TTL_SECONDS = 10 * 60
BOOTSTRAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_trial_bootstrap (
    user_id INTEGER PRIMARY KEY,
    device_hash TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS idx_mobile_trial_bootstrap_expiry
    ON mobile_trial_bootstrap(expires_at);
"""


def _db_path(database: Any) -> str:
    path = str(getattr(database, "DB_FILE", "") or "").strip()
    if not path:
        raise RuntimeError("DB_FILE is unavailable")
    return path


async def _ensure_schema(database: Any) -> None:
    async with aiosqlite.connect(_db_path(database), timeout=15) as db:
        await db.execute("PRAGMA busy_timeout=15000")
        await db.executescript(BOOTSTRAP_SCHEMA)
        await db.commit()


async def _create_grant(database: Any, user_id: int, device_hash: str, expires_at: int) -> bool:
    await _ensure_schema(database)
    async with aiosqlite.connect(_db_path(database), timeout=15) as db:
        await db.execute("PRAGMA busy_timeout=15000")
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "INSERT OR IGNORE INTO mobile_trial_bootstrap(user_id,device_hash,expires_at,created_at) VALUES(?,?,?,?)",
            (int(user_id), device_hash, int(expires_at), int(time.time())),
        )
        await db.commit()
        return cur.rowcount == 1


async def _grant_expiry(database: Any, user_id: int, device_hash: str) -> int | None:
    await _ensure_schema(database)
    async with aiosqlite.connect(_db_path(database), timeout=15) as db:
        await db.execute("PRAGMA busy_timeout=15000")
        cur = await db.execute(
            "SELECT expires_at FROM mobile_trial_bootstrap WHERE user_id=? AND device_hash=? AND expires_at>?",
            (int(user_id), device_hash, int(time.time())),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else None


async def _delete_grant(database: Any, user_id: int) -> None:
    await _ensure_schema(database)
    async with aiosqlite.connect(_db_path(database), timeout=15) as db:
        await db.execute("PRAGMA busy_timeout=15000")
        await db.execute("DELETE FROM mobile_trial_bootstrap WHERE user_id=?", (int(user_id),))
        await db.commit()


async def _trial_was_used(database: Any, user_id: int) -> bool:
    async with aiosqlite.connect(_db_path(database), timeout=15) as db:
        cur = await db.execute("SELECT trial_used FROM users WHERE user_id=?", (int(user_id),))
        row = await cur.fetchone()
        return bool(row and int(row[0] or 0))


def _fingerprint_matches(user_id: int, hwid: str, state: dict[str, Any]) -> bool:
    digest = hashlib.sha256(hwid.encode("utf-8")).hexdigest()
    for item in state.get("devices", []):
        if not isinstance(item, dict):
            continue
        fingerprint = str(item.get("fingerprint") or "").strip().lower()
        if (16 <= len(fingerprint) <= 64 and re.fullmatch(r"[0-9a-f]+", fingerprint)
                and hmac.compare_digest(digest[:len(fingerprint)], fingerprint)):
            return True
    return False


def _trial_key(keys: list[dict[str, Any]], now: int) -> dict[str, Any] | None:
    for key in keys:
        remark = str(key.get("remark") or "").casefold()
        if "пробн" in remark and int(key.get("expiry") or 0) > now:
            return key
    return None


def _valid_panel_device_state(state: Any) -> bool:
    """Accept the supported paid limits without weakening trial's two-device cap."""
    if not isinstance(state, dict) or not isinstance(state.get("devices"), list):
        return False
    try:
        return int(state.get("limit_hwid") or 0) in (2, 5, 10) and int(state.get("limit_ip") or 0) == 0
    except (TypeError, ValueError):
        return False


def _trial_panel_limits_match(state: Any) -> bool:
    if not _valid_panel_device_state(state):
        return False
    return int(state.get("limit_hwid") or 0) == 2 and int(state.get("limit_ip") or 0) == 0


def _normalized_headers(headers: dict[str, str]) -> dict[str, str]:
    return {str(key).casefold(): str(value) for key, value in headers.items() if value is not None}


async def _fetch_nodes(sub_url: str, headers: dict[str, str], parser: Any) -> list[dict[str, Any]]:
    parsed = urlparse(sub_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return []
    local_url = f"{parsed.scheme}://127.0.0.1:{parsed.port or 2096}{parsed.path}"
    if parsed.query:
        local_url += "?" + parsed.query
    allowed = ("x-hwid", "x-device-os", "x-ver-os", "x-device-model", "user-agent")
    upstream = {key.title(): value for key, value in headers.items() if key.casefold() in allowed and value}
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host += f":{parsed.port}"
    upstream["Host"] = host
    content = ""
    try:
        async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
            response = await client.get(local_url, headers=upstream)
            if response.status_code == 200:
                content = response.text
    except Exception:
        pass
    if not content:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(sub_url, headers=upstream)
                if response.status_code == 200:
                    content = response.text
        except Exception:
            return []
    if not content:
        return []
    clean = "".join(content.split())
    try:
        decoded = base64.b64decode(clean + "=" * ((4 - len(clean) % 4) % 4)).decode("utf-8", "ignore")
    except Exception:
        decoded = content
    rows = [line.strip() for line in decoded.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    nodes = [node for index, line in enumerate(rows) if (node := parser(line, index, None))]
    nodes.sort(key=lambda row: 0 if row.get("config", {}).get("security") == "reality" else 1)
    return nodes


def _install_fingerprint_readback(xui: Any) -> None:
    """Backport only the fingerprint field omitted by the active image adapter."""
    async def get_devices(user_id: int) -> dict[str, Any]:
        api = await xui._get_api()
        client_api = api.client
        email = quote(str(user_id), safe="")

        async def request_json(method: str, path: str, payload: dict | None = None) -> dict:
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
            xui._api_call_with_retry(request_json, "GET", f"panel/api/clients/get/{email}"),
            xui._api_call_with_retry(request_json, "POST", f"panel/api/clients/hwids/{email}", {}),
        )
        obj = client_data.get("obj") or {}
        client = obj.get("client") if isinstance(obj, dict) else None
        if not isinstance(client, dict):
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
                "id": device_id, "first_seen": first_seen, "last_seen": last_seen,
                "model": str(item.get("deviceModel") or "").strip()[:120],
                "os": str(item.get("deviceOs") or "").strip()[:80],
                "os_version": str(item.get("osVersion") or "").strip()[:80],
                "user_agent": str(item.get("userAgent") or "").strip()[:160],
                "fingerprint": str(item.get("fingerprint") or "").strip()[:256],
            })
        devices.sort(key=lambda item: item["last_seen"], reverse=True)
        return {"devices": devices,
                "limit_hwid": max(0, int(client.get("limitHwid") or 0)),
                "limit_ip": max(0, int(client.get("limitIp") or 0))}

    xui.get_xui_devices = get_devices


def install_legacy_trial_bootstrap(router: Any, namespace: dict[str, Any]) -> None:
    """Replace only trial/config routes and add bounded first-connect support."""
    database = namespace["database"]
    trial_request_model = namespace.get("TrialRequest")
    session_request_model = namespace.get("SessionRequest")
    if not isinstance(trial_request_model, type) or not isinstance(session_request_model, type):
        raise RuntimeError("The legacy API request models are unavailable")
    _install_fingerprint_readback(__import__("xui_client"))
    remove = {("/subscription/trial", "POST"), ("/servers", "GET"), ("/vpn/session", "POST")}
    router.routes[:] = [
        route for route in router.routes
        if not any(getattr(route, "path", None) == path and method in (getattr(route, "methods", set()) or set())
                   for path, method in remove)
    ]

    async def panel_device_state(user_id: int) -> dict[str, Any]:
        from xui_client import get_xui_devices
        state = await get_xui_devices(user_id)
        if not _valid_panel_device_state(state):
            raise HTTPException(status_code=503, detail="Не удалось подтвердить параметры устройств")
        return state

    def require_trial_panel_limits(state: dict[str, Any]) -> None:
        if not _trial_panel_limits_match(state):
            raise HTTPException(status_code=503, detail="Не удалось подтвердить параметры пробного периода")

    async def strict_panel_client(user_id: int) -> Any:
        import xui_client
        api = await xui_client._get_api()
        return await xui_client._api_call_with_retry(api.client.get_by_email, str(int(user_id)))

    async def valid_bootstrap(user_id: int, headers: dict[str, str], panel_state: dict[str, Any]) -> int | None:
        from entitlements import EntitlementSigningError, device_binding
        hwid = str(headers.get("x-hwid") or "").strip()
        try:
            digest = device_binding(user_id, hwid)
            expiry = await _grant_expiry(database, user_id, digest)
            if expiry is None or not await _trial_was_used(database, user_id):
                return None
            require_trial_panel_limits(panel_state)
            client = await strict_panel_client(user_id)
            if not client or not getattr(client, "uuid", None):
                return None
            panel_expiry = int(getattr(client, "expiry_time", 0) or 0) // 1000
            if panel_expiry <= int(time.time()):
                return None
            if _fingerprint_matches(user_id, hwid, panel_state):
                await _delete_grant(database, user_id)
                return None
            return expiry
        except EntitlementSigningError:
            return None

    async def attach_entitlement(user_id: int, headers: dict[str, str], subscription: dict[str, Any]) -> dict[str, Any]:
        headers = _normalized_headers(headers)
        from entitlements import EntitlementSigningError, create_entitlement, device_binding, load_private_key
        if not namespace["_entitlement_requested"](headers):
            return subscription
        if subscription.get("status") not in ("active", "trial", "grace"):
            subscription["entitlement"] = None
            return subscription
        keys = await database.get_user_active_keys(user_id)
        if _trial_key(keys, int(time.time())):
            subscription["status"] = "trial"
            subscription["plan_name"] = "Пробный период (3 дня)"
            subscription["max_devices"] = 2
        hwid = str(headers.get("x-hwid") or "").strip()
        try:
            load_private_key()
            device_binding(user_id, hwid)
            panel = await panel_device_state(user_id)
            limit = int(panel["limit_hwid"])
            if limit not in (2, 5, 10):
                raise EntitlementSigningError("Unsupported device limit")
            if _fingerprint_matches(user_id, hwid, panel):
                if subscription.get("status") == "trial":
                    try:
                        require_trial_panel_limits(panel)
                    except HTTPException as exc:
                        raise EntitlementSigningError("Trial panel limits mismatch") from exc
                subscription["max_devices"] = limit
                subscription["active_devices"] = len(panel["devices"])
                subscription["entitlement"] = create_entitlement(user_id, hwid, subscription)
                await _delete_grant(database, user_id)
                return subscription
            if subscription.get("status") == "trial":
                expiry = await valid_bootstrap(user_id, headers, panel)
                if expiry and expiry > int(time.time()):
                    remaining = min(int(subscription.get("seconds_remaining") or 0), expiry - int(time.time()))
                    if remaining <= 0:
                        raise HTTPException(status_code=403, detail="Первичное подключение истекло")
                    subscription["max_devices"] = 2
                    subscription["active_devices"] = len(panel["devices"])
                    bootstrap = dict(subscription, seconds_remaining=remaining)
                    subscription["entitlement"] = create_entitlement(user_id, hwid, bootstrap)
                    subscription["entitlement_bootstrap"] = True
                    subscription["entitlement_expires_at"] = expiry
                    return subscription
            raise HTTPException(status_code=403, detail="Устройство не подтверждено для этой подписки")
        except HTTPException:
            raise
        except EntitlementSigningError as exc:
            namespace["logger"].warning("Entitlement validation failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось подтвердить состояние подписки") from None
        except Exception as exc:
            namespace["logger"].warning("Entitlement panel check failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось проверить устройство подписки") from None

    namespace["_attach_signed_entitlement"] = attach_entitlement

    async def _require_device_access(user_id: int, request: Request) -> dict[str, str]:
        headers = _normalized_headers(namespace["_subscription_headers_from_request"](request))
        if not namespace["_entitlement_requested"](headers):
            return headers
        from entitlements import EntitlementSigningError, device_binding, load_private_key
        try:
            load_private_key()
            device_binding(user_id, str(headers.get("x-hwid") or ""))
            panel = await panel_device_state(user_id)
            is_trial = bool(_trial_key(await database.get_user_active_keys(user_id), int(time.time())))
            if is_trial:
                require_trial_panel_limits(panel)
            if _fingerprint_matches(user_id, headers.get("x-hwid", ""), panel):
                return headers
            if await valid_bootstrap(user_id, headers, panel):
                return headers
            raise HTTPException(status_code=403, detail="Устройство не подтверждено для этой подписки")
        except HTTPException:
            raise
        except EntitlementSigningError as exc:
            namespace["logger"].warning("Device binding rejected (%s)", type(exc).__name__)
            raise HTTPException(status_code=422, detail="Не удалось подтвердить это устройство") from None
        except Exception as exc:
            namespace["logger"].warning("Device access check unavailable (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось проверить устройство подписки") from None

    async def subscription_from_key(user_id: int) -> tuple[str, dict[str, Any]]:
        keys = await database.get_user_active_keys(user_id)
        key = _trial_key(keys, int(time.time()))
        if not key:
            raise HTTPException(status_code=409, detail="Пробный период уже использован")
        expiry = int(key.get("expiry") or 0)
        return str(key.get("key") or ""), {
            "status": "trial", "plan_name": "Пробный период (3 дня)",
            "expires_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry)),
            "seconds_remaining": max(0, expiry - int(time.time())),
            "subscription_url": key.get("key"), "max_devices": 2,
        }

    async def resume_trial(req: Any, request: Request, user_id: int, headers: dict[str, str]) -> dict[str, Any]:
        if not await _trial_was_used(database, user_id):
            raise HTTPException(status_code=409, detail="Пробный период уже использован")
        sub_url, status = await subscription_from_key(user_id)
        panel = await panel_device_state(user_id)
        require_trial_panel_limits(panel)
        status["active_devices"] = len(panel["devices"])
        servers = await _fetch_nodes(sub_url, headers, namespace["parse_single_node_line"])
        if not servers:
            raise HTTPException(status_code=503, detail="Не удалось загрузить серверы подписки")
        status["servers"] = servers
        return await attach_entitlement(user_id, headers, status)

    @router.post("/subscription/trial")
    async def activate_trial(req: trial_request_model, request: Request, user_id: int = Depends(namespace["get_current_user_id"])):
        trial_days, device_limit = 3, 2
        headers = _normalized_headers(namespace["_subscription_headers_from_request"](request))
        opted_in = namespace["_entitlement_requested"](headers)
        if opted_in:
            from entitlements import EntitlementSigningError, device_binding, load_private_key
            try:
                load_private_key()
                hwid = str(headers.get("x-hwid") or "").strip()
                device_binding(user_id, hwid)
                if req.install_id and not hmac.compare_digest(str(req.install_id).strip(), hwid):
                    raise HTTPException(status_code=422, detail="Данные устройства не совпадают")
            except EntitlementSigningError:
                raise HTTPException(status_code=422, detail="Не удалось подтвердить это устройство") from None
        now = int(time.time())
        keys = await database.get_user_active_keys(user_id)
        existing_trial = _trial_key(keys, now)
        if existing_trial:
            return await resume_trial(req, request, user_id, headers)
        if keys:
            raise HTTPException(status_code=409, detail="У вас уже есть действующая подписка")
        # Strict panel read: a swallowed panel error must not be mistaken for no client.
        try:
            existing_client = await strict_panel_client(user_id)
        except Exception as exc:
            namespace["logger"].warning("Trial eligibility panel lookup unavailable (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось проверить доступность пробного периода") from None
        if existing_client and int(getattr(existing_client, "expiry_time", 0) or 0) > now * 1000:
            raise HTTPException(status_code=409, detail="Для этого аккаунта уже создана подписка")
        if not await database.try_claim_trial(user_id):
            # The concurrent winner may already have committed the trial key.
            return await resume_trial(req, request, user_id, headers)
        try:
            from xui_client import create_xui_user
            # Legacy runtime's compatibility argument maps limit_ip=2 to
            # panel limitHwid=2 and limitIp=0; readback below is authoritative.
            result = await create_xui_user(user_id=user_id, days=trial_days, limit_ip=device_limit)
        except Exception as exc:
            try:
                if await strict_panel_client(user_id) is None:
                    await database.reset_trial_for_user(user_id)
            except Exception:
                pass  # Ambiguous panel result: reserve the shared trial claim.
            namespace["logger"].warning("Trial provisioning failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось подключить пробный период. Попробуйте позже") from None
        if not result or not result.get("subscription_url"):
            try:
                if await strict_panel_client(user_id) is None:
                    await database.reset_trial_for_user(user_id)
            except Exception:
                pass
            raise HTTPException(status_code=503, detail="Не удалось подключить пробный период. Попробуйте позже")
        sub_url = str(result["subscription_url"])
        result_expiry_ms = int(result.get("expiry_time") or 0)
        # Initial subscription request carries the installation identity. Only
        # the subsequent panel readback can prove that the panel registered it.
        servers = await _fetch_nodes(sub_url, headers, namespace["parse_single_node_line"])
        panel = await panel_device_state(user_id)
        require_trial_panel_limits(panel)
        if len(panel["devices"]) > device_limit:
            raise HTTPException(status_code=503, detail="Не удалось подтвердить лимит устройств пробного периода")
        try:
            verified_client = await strict_panel_client(user_id)
            created_uuid = str(result.get("uuid") or "").strip()
            panel_uuid = str(getattr(verified_client, "uuid", "") or "").strip()
            if not created_uuid or not panel_uuid or not hmac.compare_digest(created_uuid, panel_uuid):
                raise RuntimeError("Panel client identity did not match the provisioned account")
            panel_expiry = int(getattr(verified_client, "expiry_time", 0) or 0) // 1000
            if (panel_expiry <= now
                    or abs((panel_expiry - now) - trial_days * 86400) > 2
                    or (result_expiry_ms and abs(panel_expiry * 1000 - result_expiry_ms) > 2000)):
                raise RuntimeError("Panel trial expiry did not match the provisioned expiry")
            # The legacy create method does not return expiry_time for a new
            # client. The owner-scoped panel readback is the authoritative
            # source, and is required to be exactly three days from issuance.
            expiry_ts = panel_expiry
        except Exception as exc:
            namespace["logger"].warning("Trial panel ownership verification failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось подтвердить пробный период в панели") from None
        try:
            await database.add_key(
                user_id=user_id, key=sub_url, remark="Пробный период — 3 дня",
                uuid=result.get("uuid") or str(user_id), days=trial_days,
                limit_ip=device_limit, expiry=expiry_ts,
            )
        except Exception as exc:
            namespace["logger"].error("Trial key persistence failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось сохранить пробный период. Напишите в поддержку") from None
        if opted_in and not _fingerprint_matches(user_id, headers.get("x-hwid", ""), panel):
            from entitlements import device_binding
            try:
                ok = await _create_grant(database, user_id, device_binding(user_id, headers.get("x-hwid", "")),
                                         int(time.time()) + BOOTSTRAP_TTL_SECONDS)
            except Exception as exc:
                namespace["logger"].error("Trial bootstrap storage failed (%s)", type(exc).__name__)
                raise HTTPException(status_code=503, detail="Не удалось подготовить первое подключение") from None
            if not ok:
                raise HTTPException(status_code=409, detail="Пробный период уже использован")
        if not servers:
            # Keep the durable trial and fixed grant before returning a
            # temporary upstream failure. The same request can retry without
            # provisioning or extending the panel client again.
            raise HTTPException(status_code=503, detail="Не удалось загрузить серверы подписки")
        status = {
            "status": "trial", "plan_name": "Пробный период (3 дня)",
            "expires_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry_ts)),
            "seconds_remaining": max(0, expiry_ts - int(time.time())),
            "subscription_url": sub_url, "max_devices": device_limit,
            "active_devices": len(panel["devices"]), "servers": servers,
        }
        return await attach_entitlement(user_id, headers, status)

    @router.get("/servers")
    async def get_servers(request: Request, user_id: int = Depends(namespace["get_current_user_id"])):
        headers = await _require_device_access(user_id, request)
        sub_url, _, _ = await namespace["get_user_real_subscription"](user_id)
        servers = await _fetch_nodes(sub_url, headers, namespace["parse_single_node_line"]) if sub_url else []
        return {"servers": servers, "subscription_url": sub_url}

    @router.post("/vpn/session")
    async def create_vpn_session(req: session_request_model, request: Request, user_id: int = Depends(namespace["get_current_user_id"])):
        headers = await _require_device_access(user_id, request)
        sub_url, _, _ = await namespace["get_user_real_subscription"](user_id)
        servers = await _fetch_nodes(sub_url, headers, namespace["parse_single_node_line"]) if sub_url else []
        if not servers:
            raise HTTPException(status_code=403, detail="Нет активной подписки или серверов")
        matched = next((row for row in servers if row.get("node_code") == req.node_code), servers[0])
        config = matched.get("config")
        if not config or not config.get("uuid"):
            raise HTTPException(status_code=503, detail="Не удалось получить конфигурацию VPN")
        if config.get("security") == "reality" and not config.get("public_key"):
            raise HTTPException(status_code=503, detail="Не удалось получить конфигурацию VPN")
        return config
