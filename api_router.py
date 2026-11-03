"""
ByMeVPN Mobile App Backend API Router
FastAPI router mounted at /api/v1 in webhook.py

Security & Architecture:
1. Strict Password Authentication using PBKDF2-HMAC-SHA256 with cryptographically secure random salt.
   Prevents unauthorized logins and password bypass vulnerabilities.
2. 1-Tap Telegram Login (session-based token exchange, zero manual link copying).
3. Real 3x-ui Subscriptions: Retrieves real subscription URLs (/yay/<sub_id>) and parses
   actual VLESS Reality & Hysteria server nodes directly from 3x-ui.
   Zero dummy / fake placeholder servers.
4. Real VPN session delivery matching actual Xray server Reality inbounds.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

import database
from entitlements import EntitlementSigningError, create_entitlement, device_binding, load_private_key
from config import XUI_SUB_PATH, XUI_URL
from telegram_post_compat import TelegramSessionTokenBody, register_telegram_check_post
from telegram_auth_storage_compat import install_telegram_auth_storage_compat
from google_signin import GoogleIdTokenError, verify_google_id_token

logger = logging.getLogger(__name__)

# Older production deployments use a narrow bind-mounted database.py shim.
# Fill only missing Telegram session helpers; native DB implementations stay
# authoritative when present.
install_telegram_auth_storage_compat(database)

api_router = APIRouter()

# ---------------------------------------------------------------------------
# Telegram Session Exchange & JWT Signing
# ---------------------------------------------------------------------------
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET or len(JWT_SECRET.encode("utf-8")) < 32:
    raise RuntimeError("JWT_SECRET must be configured with at least 32 bytes")

JWT_ISSUER = "bymevpn-api"
JWT_AUDIENCE = "bymevpn-mobile"
MOBILE_TRIAL_BOOTSTRAP_TTL_SECONDS = 10 * 60
_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def _request_correlation_id(request: Request) -> str:
    """Return a safe caller ID or generate one without logging request data."""
    provided = request.headers.get("X-Correlation-ID", "")
    if _SAFE_CORRELATION_ID.fullmatch(provided):
        return provided
    return secrets.token_hex(8)


def _correlated_http_error(status_code: int, detail: str, correlation_id: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"X-Correlation-ID": correlation_id},
    )


async def _ensure_auth_db():
    """Ensure app_users table exists for secure mobile email authentication."""
    db = await database.get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS app_users (
            user_id INTEGER PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            name TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    await db.commit()


def hash_password(password: str, salt: str) -> str:
    """Generate salted PBKDF2-HMAC-SHA256 hash."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    ).hex()


async def confirm_telegram_session(token: str, user_id: int, username: str = "", full_name: str = "") -> bool:
    """Confirm a pending single-use login state created by the mobile API."""
    state = token.removeprefix("auth_")
    if not state or len(state) > 256:
        return False
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    correlation_id = state_hash[:16]
    try:
        confirmed = await database.confirm_mobile_auth_session(
            state_hash,
            user_id,
            (username or f"user_{user_id}")[:80],
            (full_name or username or f"User {user_id}")[:160],
        )
    except Exception as exc:
        logger.error(
            "Telegram session confirmation stage=bot_confirm code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise
    logger.info(
        "Telegram session confirmation stage=bot_confirm code=%s correlation_id=%s",
        "confirmed" if confirmed else "not_pending_or_expired", correlation_id,
    )
    return confirmed


def _jwt_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _jwt_b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def create_simple_jwt(
    user_id: int,
    email: str = "",
    *,
    issued_at: Optional[int] = None,
    expires_at: Optional[int] = None,
    issuer: str = JWT_ISSUER,
    audience: str = JWT_AUDIENCE,
    jwt_id: Optional[str] = None,
) -> str:
    """Create an HS256 JWT with fixed service issuer/audience and bounded lifetime."""
    issued = int(time.time()) if issued_at is None else int(issued_at)
    expiry = issued + 30 * 86400 if expires_at is None else int(expires_at)
    session_id = jwt_id or secrets.token_urlsafe(18)
    if (
        int(user_id) <= 0
        or expiry <= issued
        or expiry - issued > 30 * 86400
        or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", session_id)
    ):
        raise ValueError("Invalid JWT subject, lifetime, or session ID")

    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "sub": str(int(user_id)),
        "jti": session_id,
        "email": str(email or ""),
        "iss": issuer,
        "aud": audience,
        "iat": issued,
        "exp": expiry,
    }
    header_part = _jwt_b64encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    claims_part = _jwt_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header_part}.{claims_part}"
    signature = hmac.new(JWT_SECRET.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_jwt_b64encode(signature)}"


def _verified_token_claims(token: str) -> Optional[dict]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header = json.loads(_jwt_b64decode(parts[0]).decode("utf-8"))
        claims = json.loads(_jwt_b64decode(parts[1]).decode("utf-8"))
        if not isinstance(header, dict) or header.get("alg") != "HS256" or header.get("typ") != "JWT":
            return None
        if not isinstance(claims, dict):
            return None

        signing_input = f"{parts[0]}.{parts[1]}"
        expected = hmac.new(JWT_SECRET.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
        supplied = _jwt_b64decode(parts[2])
        if not hmac.compare_digest(supplied, expected):
            return None

        # Require exact service claims. In particular, do not accept a list
        # audience containing this app alongside an unexpected audience.
        if claims.get("iss") != JWT_ISSUER or claims.get("aud") != JWT_AUDIENCE:
            return None
        subject = claims.get("sub")
        session_id = claims.get("jti")
        issued = claims.get("iat")
        expiry = claims.get("exp")
        if (
            not isinstance(subject, str)
            or not subject.isdecimal()
            or not isinstance(session_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", session_id)
            or type(issued) is not int
            or type(expiry) is not int
        ):
            return None
        user_id = int(subject)
        now = int(time.time())
        if (
            user_id <= 0
            or issued > now + 60
            or expiry <= now
            or expiry <= issued
            or expiry - issued > 30 * 86400
        ):
            return None
        email = claims.get("email", "")
        if not isinstance(email, str):
            return None
        return {
            "user_id": user_id,
            "jti": session_id,
            "email": email,
            "issued_at": issued,
            "expires_at": expiry,
            "issuer": JWT_ISSUER,
            "audience": JWT_AUDIENCE,
        }
    except (ValueError, TypeError, UnicodeDecodeError, OverflowError):
        return None


def verify_simple_jwt(token: str) -> Optional[int]:
    """Verify signature, issuer, exact audience and lifetime; return subject ID."""
    claims = _verified_token_claims(token)
    return int(claims["user_id"]) if claims else None


async def get_current_user_id(authorization: Optional[str] = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    claims = _verified_token_claims(token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")
    try:
        if await database.is_mobile_jwt_revoked(claims["jti"]):
            raise HTTPException(status_code=401, detail="Session has been logged out")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("JWT revocation check unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Session verification temporarily unavailable") from None
    return claims["user_id"]


# Real 3x-ui Server Nodes & Subscription Resolution
# ---------------------------------------------------------------------------

def _detect_geo(remark: str) -> Tuple[str, str, str, str]:
    """Detect country, code, city, and flag from inbound remark (Примечание)."""
    flag_map = [
        ("🇩🇪", ("Германия", "de", "Франкфурт")),
        ("🇳🇱", ("Нидерланды", "nl", "Амстердам")),
        ("🇺🇸", ("США", "us", "Нью-Йорк")),
        ("🇫🇮", ("Финляндия", "fi", "Хельсинки")),
        ("🇸🇪", ("Швеция", "se", "Стокгольм")),
        ("🇬🇧", ("Великобритания", "gb", "Лондон")),
        ("🇹🇷", ("Турция", "tr", "Стамбул")),
        ("🇫🇷", ("Франция", "fr", "Париж")),
        ("🇵🇱", ("Польша", "pl", "Варшава")),
        ("🇷🇺", ("Россия", "ru", "Москва")),
        ("🇨🇦", ("Канада", "ca", "Торонто")),
        ("🇯🇵", ("Япония", "jp", "Токио")),
        ("🇸🇬", ("Сингапур", "sg", "Сингапур")),
        ("🇨🇭", ("Швейцария", "ch", "Цюрих")),
        ("🇦🇹", ("Австрия", "at", "Вена")),
        ("🇪🇸", ("Испания", "es", "Мадрид")),
        ("🇮🇹", ("Италия", "it", "Милан")),
    ]
    for flag_emoji, (cname, ccode, city) in flag_map:
        if flag_emoji in remark:
            return cname, ccode, city, flag_emoji

    lower = remark.lower()
    if any(k in lower for k in ["nl", "amsterdam", "netherland", "нидерланд"]):
        return "Нидерланды", "nl", "Амстердам", "🇳🇱"
    if any(k in lower for k in ["de", "frankfurt", "german", "герман"]):
        return "Германия", "de", "Франкфурт", "🇩🇪"
    if any(k in lower for k in ["fi", "helsinki", "finland", "финлянд"]):
        return "Финляндия", "fi", "Хельсинки", "🇫🇮"
    if any(k in lower for k in ["se", "stockholm", "sweden", "швеци"]):
        return "Швеция", "se", "Стокгольм", "🇸🇪"
    if any(k in lower for k in ["us", "usa", "york", "сша", "miami", "america"]):
        return "США", "us", "Нью-Йорк", "🇺🇸"
    if any(k in lower for k in ["gb", "uk", "london", "британи", "англия"]):
        return "Великобритания", "gb", "Лондон", "🇬🇧"
    if any(k in lower for k in ["tr", "turkey", "турци", "istanbul"]):
        return "Турция", "tr", "Стамбул", "🇹🇷"
    if any(k in lower for k in ["fr", "france", "франци", "paris"]):
        return "Франция", "fr", "Париж", "🇫🇷"
    if any(k in lower for k in ["pl", "poland", "польш", "warsaw"]):
        return "Польша", "pl", "Варшава", "🇵🇱"
    if any(k in lower for k in ["ru", "russia", "росси", "moscow"]):
        return "Россия", "ru", "Москва", "🇷🇺"

    return "Сервер", "eu", "VPN", "🌐"


def parse_single_node_line(line: str, index: int, fallback_uuid: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Parse real vless:// or hysteria2:// line into ServerNode with VpnSessionConfig."""
    try:
        if line.startswith("vless://") or line.startswith("VLESS://"):
            clean = line[8:]
            hash_parts = clean.split("#", 1)
            main_part = hash_parts[0]
            remark = unquote(hash_parts[1]) if len(hash_parts) > 1 else f"ByMeVPN Node {index + 1}"

            at_parts = main_part.split("@", 1)
            uuid_str = at_parts[0] or (fallback_uuid or "")
            host_and_rest = at_parts[1]

            q_parts = host_and_rest.split("?", 1)
            host_port = q_parts[0]
            query_str = q_parts[1] if len(q_parts) > 1 else ""

            hp_parts = host_port.split(":")
            raw_host = hp_parts[0]
            port = int(hp_parts[1]) if len(hp_parts) > 1 else 443
            server_ip = raw_host

            params = parse_qs(query_str)
            pbk = params.get("pbk", [""])[0]
            sid = params.get("sid", [""])[0]
            sni = params.get("sni", [raw_host or "www.cloudflare.com"])[0]
            fp = params.get("fp", ["chrome"])[0]
            flow = params.get("flow", ["xtls-rprx-vision"])[0]
            security = params.get("security", ["reality"])[0]
            net_type = params.get("type", ["tcp"])[0]

            country, country_code, base_city, flag = _detect_geo(remark)
            sec_lower = security.lower()
            proto_badge = "VLESS Reality" if sec_lower == "reality" else ("VLESS TLS" if sec_lower == "tls" else "VLESS")
            
            # Prioritize exact Remark (Примечание) entered by admin in 3x-ui
            clean_remark = remark.strip() if (remark and remark.strip()) else f"{country} #{index + 1}"
            if proto_badge.lower() in clean_remark.lower() or "reality" in clean_remark.lower() or "vless" in clean_remark.lower():
                disp_name = clean_remark
            else:
                disp_name = f"{clean_remark} • {proto_badge}"

            node_code = f"node_{index}_{server_ip.replace('.', '_')}_{port}"

            return {
                "node_code": node_code,
                "country": country,
                "country_code": country_code,
                "city": clean_remark,
                "flag": flag,
                "protocol": proto_badge,
                "remark": clean_remark,
                "display_name": disp_name,
                "port": port,
                "ping_ms": 25 + index * 6,
                "load_percent": 18 + index * 4,
                "is_premium": False,
                "country_en": country,
                "city_en": clean_remark,
                "config": {
                    "protocol": "vless",
                    "server": server_ip,
                    "port": port,
                    "uuid": uuid_str,
                    "encryption": "none",
                    "flow": flow,
                    "security": security,
                    "server_name": sni,
                    "public_key": pbk,
                    "short_id": sid,
                    "fingerprint": fp,
                    "network": net_type,
                },
            }
        elif line.startswith("hysteria2://") or line.startswith("hy2://"):
            clean = line.split("://", 1)[1]
            hash_parts = clean.split("#", 1)
            main_part = hash_parts[0]
            remark = unquote(hash_parts[1]) if len(hash_parts) > 1 else f"ByMeVPN Fast {index + 1}"

            at_parts = main_part.split("@", 1)
            password = at_parts[0]
            host_and_rest = at_parts[1]

            q_parts = host_and_rest.split("?", 1)
            hp_parts = q_parts[0].split(":")
            raw_host = hp_parts[0]
            port = int(hp_parts[1]) if len(hp_parts) > 1 else 443
            server_ip = raw_host

            query_str = q_parts[1] if len(q_parts) > 1 else ""
            params = parse_qs(query_str)
            sni = params.get("sni", [raw_host])[0]

            country, country_code, base_city, flag = _detect_geo(remark)
            proto_badge = "Hysteria 2"
            
            # Prioritize exact Remark (Примечание) entered by admin in 3x-ui
            clean_remark = remark.strip() if (remark and remark.strip()) else f"{country} #{index + 1}"
            if proto_badge.lower() in clean_remark.lower() or "hysteria" in clean_remark.lower() or "hy2" in clean_remark.lower():
                disp_name = clean_remark
            else:
                disp_name = f"{clean_remark} • {proto_badge}"

            node_code = f"node_{index}_hy2_{server_ip.replace('.', '_')}_{port}"

            return {
                "node_code": node_code,
                "country": country,
                "country_code": country_code,
                "city": clean_remark,
                "flag": flag,
                "protocol": proto_badge,
                "remark": clean_remark,
                "display_name": disp_name,
                "port": port,
                "ping_ms": 22 + index * 5,
                "load_percent": 15,
                "is_premium": False,
                "country_en": country,
                "city_en": clean_remark,
                "config": {
                    "protocol": "hysteria2",
                    "server": server_ip,
                    "port": port,
                    "uuid": password,
                    "encryption": "",
                    "flow": "",
                    "security": "tls",
                    "server_name": sni,
                    "public_key": "",
                    "short_id": "",
                    "fingerprint": "firefox",
                    "network": "udp",
                },
            }
    except Exception as e:
        logger.warning(f"Error parsing node line {line[:50]}: {e}")
    return None


async def fetch_and_parse_real_nodes(
    sub_url: str,
    fallback_uuid: Optional[str] = None,
    request_headers: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Fetch raw base64 subscription from 3x-ui and parse into actual real nodes."""
    raw_content = ""
    parsed = urlparse(sub_url)
    # Preserve HTTPS and the subscription host: the listener uses them for
    # routing and its HWID gate. Never downgrade a TLS subscription to HTTP.
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
    local_url = f"{scheme}://127.0.0.1:{parsed.port or 2096}{parsed.path}"
    if parsed.query:
        local_url = f"{local_url}?{parsed.query}"
    allowed_headers = ("X-HWID", "X-Device-OS", "X-Ver-OS", "X-Device-Model", "User-Agent")
    upstream_headers = {
        name: value for name in allowed_headers
        if (value := (request_headers or {}).get(name))
    }
    if parsed.hostname:
        host_header = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        if parsed.port:
            host_header = f"{host_header}:{parsed.port}"
        upstream_headers["Host"] = host_header

    # The local 3x-ui listener can have a hostname-specific certificate.
    # Limit its TLS exception strictly to loopback; verify public TLS normally.
    async with httpx.AsyncClient(verify=False, timeout=5.0) as local_client:
        try:
            resp = await local_client.get(local_url, headers=upstream_headers)
            if resp.status_code == 200:
                raw_content = resp.text
        except Exception:
            pass

    if not raw_content:
        try:
            async with httpx.AsyncClient(timeout=5.0) as public_client:
                resp = await public_client.get(sub_url, headers=upstream_headers)
                if resp.status_code == 200:
                    raw_content = resp.text
        except Exception as e:
            logger.error(
                "Failed to fetch subscription from host %s (%s)",
                parsed.hostname or "unknown",
                type(e).__name__,
            )

    if not raw_content:
        return []

    decoded = ""
    try:
        clean = "".join(raw_content.split())
        padding = "=" * ((4 - len(clean) % 4) % 4)
        decoded = base64.b64decode((clean + padding).encode()).decode("utf-8", "ignore")
    except Exception:
        decoded = raw_content

    lines = [l.strip() for l in decoded.splitlines() if l.strip() and not l.strip().startswith("#")]
    servers = []

    for index, line in enumerate(lines):
        node = parse_single_node_line(line, index, fallback_uuid)
        if node:
            servers.append(node)

    servers.sort(key=lambda s: 0 if s.get("config", {}).get("security") == "reality" else 1)
    return servers


async def get_user_real_subscription(
    user_id: int,
    request_headers: Optional[Dict[str, str]] = None,
    include_device_count: bool = False,
) -> Tuple[Optional[str], List[Dict[str, Any]], Dict[str, Any]]:
    """
    Looks up user's active keys from DB or 3x-ui, fetches the real subscription,
    and parses all actual VLESS Reality & Hysteria nodes.
    Returns: (sub_url, servers_list, subscription_dict)
    """
    now = int(time.time())
    sub_url: Optional[str] = None
    sub_uuid: Optional[str] = None
    expiry_ts = 0
    days_count = 30
    device_limit = 2
    best_key = None

    # 1. Check local keys table
    user_keys = await database.get_user_keys(user_id)
    active_keys = [k for k in user_keys if int(k.get("expiry", 0)) > now]
    if active_keys:
        best_key = max(active_keys, key=lambda k: int(k.get("expiry", 0)))
        expiry_ts = int(best_key.get("expiry", 0))
        days_count = int(best_key.get("days", 30))
        from constants import validate_device_limit
        device_limit = validate_device_limit(best_key.get("limit_ip"))
        sub_uuid = best_key.get("uuid")
        key_content = best_key.get("key", "").strip()
        if key_content.startswith("http://") or key_content.startswith("https://"):
            sub_url = key_content

    # 2. Check 3x-ui client if not in DB
    if not sub_url:
        try:
            from xui_client import get_xui_client_details, get_user_subscription_links
            client_details = await get_xui_client_details(user_id)
            if client_details:
                from constants import validate_device_limit
                sub_uuid = client_details.get("uuid") or sub_uuid
                sub_url = client_details.get("subscription_url")
                device_limit = validate_device_limit(client_details.get("limit_hwid", 0))
                if not expiry_ts:
                    expiry_ts = int(client_details.get("expiry_time", 0)) // 1000
            if not sub_url:
                links = await get_user_subscription_links(user_id)
                if links:
                    sub_url = links[0]
        except Exception as e:
            logger.warning(f"Error checking 3x-ui for user {user_id}: {e}")

    # If no active subscription or expired: return none and empty servers (NO FAKE SERVERS)
    if not sub_url or (expiry_ts and expiry_ts <= now):
        trial_available = False
        trial_reason = "trial_already_used"
        if not await database.has_trial_used(user_id) and not active_keys:
            try:
                from xui_client import get_xui_client_details
                panel_client = await get_xui_client_details(user_id, strict=True)
                panel_expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
                if panel_client and (panel_expiry <= 0 or panel_expiry > now):
                    trial_reason = "existing_panel_subscription"
                elif not _entitlement_requested(request_headers or {}):
                    trial_available = True
                    trial_reason = None
                else:
                    # First-time clients cannot have panel HWID evidence until
                    # after their first tunnel connection. Eligibility only
                    # validates the signed-client identity and signer here;
                    # the bounded bootstrap path verifies the panel client and
                    # its 2/0 limits before allowing that first connection.
                    headers = request_headers or {}
                    hwid = str(headers.get("X-HWID") or "").strip()
                    device_binding(user_id, hwid)
                    load_private_key()
                    trial_available = True
                    trial_reason = None
            except Exception as exc:
                logger.warning("Trial eligibility lookup unavailable (%s)", type(exc).__name__)
                trial_reason = (
                    "device_confirmation_required"
                    if _entitlement_requested(request_headers or {})
                    else "eligibility_check_unavailable"
                )
        return None, [], {
            "status": "none",
            "plan_code": None,
            "plan_name": "Нет подписки",
            "trial_available": trial_available,
            "trial_unavailable_reason": trial_reason,
            "max_devices": device_limit,
            "active_devices": 0,
            "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "subscription_url": None,
        }

    # Fetch and parse real nodes from 3x-ui
    servers = await fetch_and_parse_real_nodes(sub_url, sub_uuid, request_headers)

    seconds_remaining = max(0, expiry_ts - now) if expiry_ts else 30 * 86400
    expires_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry_ts)) if expiry_ts else None

    period_code = {30: "1m", 120: "3m", 240: "6m", 450: "12m"}.get(days_count)
    plan_code = f"{period_code}_d{device_limit}" if period_code else None
    active_devices = None
    if include_device_count:
        try:
            from xui_client import get_xui_devices
            device_state = await get_xui_devices(user_id)
            panel_limit = int(device_state.get("limit_hwid") or 0)
            if panel_limit in (2, 5, 10):
                device_limit = panel_limit
                period_code = {30: "1m", 120: "3m", 240: "6m", 450: "12m"}.get(days_count)
                plan_code = f"{period_code}_d{device_limit}" if period_code else None
            active_devices = len(device_state.get("devices") or [])
        except Exception as exc:
            logger.warning("Device count unavailable for subscription status (%s)", type(exc).__name__)
    is_trial = bool(best_key and "пробн" in str(best_key.get("remark") or "").casefold())
    sub_dict = {
        "status": "trial" if is_trial else "active",
        "plan_code": plan_code,
        "plan_name": f"Подписка ByMeVPN ({days_count} дн.)",
        "expires_at": expires_at,
        "seconds_remaining": seconds_remaining,
        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "max_devices": device_limit,
        "trial_available": False,
        "subscription_url": sub_url,
    }
    if active_devices is not None:
        sub_dict["active_devices"] = active_devices

    return sub_url, servers, sub_dict


async def _attach_signed_entitlement(
    user_id: int,
    headers: Dict[str, str],
    subscription: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach an ES256 snapshot only to clients that explicitly opt in."""
    if not _entitlement_requested(headers):
        # Preserve the legacy response shape so old clients never receive a
        # token they do not know how to validate.
        return subscription
    if subscription.get("status") not in ("active", "trial", "grace"):
        subscription["entitlement"] = None
        return subscription

    hwid = str(headers.get("X-HWID") or "").strip()
    try:
        device_state = await _require_registered_entitlement_device(user_id, headers)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        bootstrap_expiry = await _validated_trial_bootstrap_expiry(user_id, headers)
        if bootstrap_expiry is None:
            # The panel may have learned the HWID between the first read and
            # bootstrap validation. Re-read once so that race upgrades directly
            # to the ordinary entitlement instead of returning a false denial.
            device_state = await _require_registered_entitlement_device(user_id, headers)
        else:
            remaining_bootstrap = bootstrap_expiry - int(time.time())
            if remaining_bootstrap <= 0:
                raise HTTPException(status_code=403, detail="Первичное подключение истекло") from None
            subscription["max_devices"] = 2
            subscription["active_devices"] = 0
            bootstrap_status = dict(subscription)
            bootstrap_status["seconds_remaining"] = min(
                int(subscription.get("seconds_remaining") or 0), remaining_bootstrap,
            )
            subscription["entitlement"] = create_entitlement(user_id, hwid, bootstrap_status)
            subscription["entitlement_bootstrap"] = True
            subscription["entitlement_expires_at"] = bootstrap_expiry
            return subscription
    devices = device_state["devices"]

    try:
        device_limit = int(device_state.get("limit_hwid") or 0)
    except (TypeError, ValueError) as exc:
        raise EntitlementSigningError("Subscription device limit is unavailable") from exc
    if device_limit not in (2, 5, 10):
        raise EntitlementSigningError("Subscription device limit is unavailable")
    if subscription.get("status") == "trial":
        try:
            ip_limit = int(device_state.get("limit_ip") or 0)
        except (TypeError, ValueError) as exc:
            raise EntitlementSigningError("Trial panel limits are unavailable") from exc
        if device_limit != 2 or ip_limit != 0:
            raise EntitlementSigningError("Trial panel limits do not match the server catalog")
    subscription["max_devices"] = device_limit
    subscription["active_devices"] = len(devices)
    subscription["entitlement"] = create_entitlement(user_id, hwid, subscription)
    try:
        await database.delete_mobile_trial_bootstrap(user_id)
    except Exception as exc:
        logger.warning("Confirmed trial bootstrap cleanup failed (%s)", type(exc).__name__)
    return subscription


SUPPORTED_ENTITLEMENT_VERSION = "es256-v1"


def _entitlement_requested(headers: Dict[str, str]) -> bool:
    value = next((v for k, v in headers.items() if k.casefold() == "x-entitlement-version"), None)
    if value is None:
        return False
    if value.strip() != SUPPORTED_ENTITLEMENT_VERSION:
        raise HTTPException(status_code=400, detail="Версия проверки подписки не поддерживается")
    return True


async def _require_registered_entitlement_device(
    user_id: int, headers: Dict[str, str],
) -> Dict[str, Any]:
    """Require an authenticated account's HWID to exist in its 3x-ui record."""
    hwid = str(next((v for k, v in headers.items() if k.casefold() == "x-hwid"), "") or "").strip()
    try:
        device_binding(user_id, hwid)
    except EntitlementSigningError:
        raise HTTPException(status_code=422, detail="Не удалось подтвердить это устройство") from None

    try:
        from xui_client import get_xui_devices, has_xui_client
        if not await has_xui_client(user_id):
            raise HTTPException(status_code=403, detail="Устройство не подтверждено для этой подписки")
        device_state = await get_xui_devices(user_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Entitlement device check unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Не удалось проверить устройство подписки") from None

    if not isinstance(device_state, dict) or not isinstance(device_state.get("devices"), list):
        raise HTTPException(status_code=503, detail="Не удалось проверить устройство подписки")
    registered = _panel_state_contains_hwid(user_id, hwid, device_state)
    if not registered:
        raise HTTPException(status_code=403, detail="Устройство не подтверждено для этой подписки")
    return device_state


def _panel_state_contains_hwid(user_id: int, hwid: str, device_state: Dict[str, Any]) -> bool:
    current_fingerprint = hashlib.sha256(hwid.encode("utf-8")).hexdigest()
    return any(
        isinstance(item, dict)
        and (fingerprint := str(item.get("fingerprint") or "").strip().lower())
        and 16 <= len(fingerprint) <= 64
        and re.fullmatch(r"[0-9a-f]+", fingerprint)
        and hmac.compare_digest(current_fingerprint[:len(fingerprint)], fingerprint)
        for item in device_state.get("devices", [])
    )


async def _validated_trial_bootstrap_expiry(
    user_id: int, headers: Dict[str, str],
) -> Optional[int]:
    """Check a fixed, short first-connect grant against the account and live panel state."""
    hwid = str(headers.get("X-HWID") or "").strip()
    try:
        fingerprint = device_binding(user_id, hwid)
    except EntitlementSigningError:
        return None
    try:
        expiry = await database.get_mobile_trial_bootstrap(user_id, fingerprint)
        if expiry is None or expiry <= int(time.time()):
            return None
        if not await database.has_trial_used(user_id):
            return None
        from xui_client import get_xui_client_details, get_xui_devices, has_xui_client
        if not await has_xui_client(user_id):
            return None
        panel_client, device_state = await asyncio.gather(
            get_xui_client_details(user_id, strict=True),
            get_xui_devices(user_id),
        )
        panel_expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
        if (panel_expiry <= int(time.time()) or not isinstance(device_state, dict)
                or int(device_state.get("limit_hwid") or 0) != 2
                or int(device_state.get("limit_ip") or 0) != 0
                or not isinstance(device_state.get("devices"), list)):
            return None
        # If the panel has already learned this device, stop using bootstrap.
        if _panel_state_contains_hwid(user_id, hwid, device_state):
            await database.delete_mobile_trial_bootstrap(user_id)
            return None
        return int(expiry)
    except Exception as exc:
        logger.warning("Trial bootstrap panel validation failed (%s)", type(exc).__name__)
        return None


async def _require_opted_in_device_access(user_id: int, request: Request) -> Dict[str, str]:
    """Gate signed-client subscription/config routes on signer and panel HWID proof."""
    headers = _subscription_headers_from_request(request)
    if not _entitlement_requested(headers):
        return headers
    try:
        load_private_key()
    except EntitlementSigningError as exc:
        logger.error("Entitlement signer unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Не удалось подтвердить состояние подписки") from None
    try:
        await _require_registered_entitlement_device(user_id, headers)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        if await _validated_trial_bootstrap_expiry(user_id, headers) is None:
            # Retry once because panel state may change between reads.
            await _require_registered_entitlement_device(user_id, headers)
    return headers


async def _auth_subscription_snapshot(user_id: int, headers: Dict[str, str]):
    """Keep login independent from 3x-ui subscription availability."""
    _entitlement_requested(headers)
    try:
        sub_url, servers, sub_dict = await get_user_real_subscription(user_id, headers)
        sub_dict = await _attach_signed_entitlement(user_id, headers, sub_dict)
        return sub_url, servers, sub_dict
    except HTTPException as exc:
        logger.warning("Subscription snapshot unavailable during login (HTTP %s)", exc.status_code)
        return None, [], {
            "status": "unknown", "plan_name": "Проверяем подписку",
            "trial_available": False, "trial_unavailable_reason": "subscription_check_unavailable",
            "max_devices": 0, "active_devices": 0,
        }
    except Exception as exc:
        logger.warning("Subscription lookup unavailable during login (%s)", type(exc).__name__)
        return None, [], {
            "status": "unknown", "plan_name": "Проверяем подписку",
            "trial_available": False, "trial_unavailable_reason": "subscription_check_unavailable",
            "max_devices": 0, "active_devices": 0,
        }


def _subscription_headers_from_request(request: Request) -> Dict[str, str]:
    """Forward only device metadata required by the subscription HWID gate."""
    names = (
        "X-HWID", "X-Device-OS", "X-Ver-OS", "X-Device-Model", "User-Agent",
        "X-Entitlement-Version",
    )
    return {name: value for name in names if (value := request.headers.get(name))}


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class CreateTelegramSessionResponse(BaseModel):
    session_token: str
    bot_url: str


class TelegramCheckResponse(BaseModel):
    status: str
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    user: Optional[Dict[str, Any]] = None
    subscription: Optional[Dict[str, Any]] = None
    servers: Optional[List[Dict[str, Any]]] = None


# Keep the model name available to existing unit callers and adapters.
TelegramSessionCheckRequest = TelegramSessionTokenBody


class TelegramSessionCancelRequest(BaseModel):
    token: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = "User"
    install_id: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str
    install_id: Optional[str] = None


class GoogleLoginRequest(BaseModel):
    id_token: str
    nonce: str
    install_id: Optional[str] = None


class RefreshRequest(BaseModel):
    refresh: str


class TrialRequest(BaseModel):
    install_id: Optional[str] = None
    device_model: Optional[str] = None


class PaymentCreateRequest(BaseModel):
    plan_code: str
    request_id: Optional[str] = None
    amount_rub: Optional[int] = None
    days: Optional[int] = None
    devices: Optional[int] = None
    provider: Optional[str] = "yookassa"


class MobileV2PaymentCreateRequest(BaseModel):
    """Mobile v2 accepts only a server-defined plan and an idempotency key."""
    plan_code: str
    request_id: str
    provider: str = "yookassa"

    class Config:
        extra = "forbid"


class SessionRequest(BaseModel):
    node_code: str
    install_id: Optional[str] = None


# ---------------------------------------------------------------------------
# 1. TELEGRAM 1-TAP AUTH (Zero Link Copying)
# ---------------------------------------------------------------------------

@api_router.post("/auth/telegram/create-session", response_model=CreateTelegramSessionResponse)
async def create_telegram_session(response: Response):
    token = f"auth_{secrets.token_urlsafe(32)}"
    state_hash = hashlib.sha256(token.removeprefix("auth_").encode("utf-8")).hexdigest()
    correlation_id = state_hash[:16]
    response.headers["X-Correlation-ID"] = correlation_id
    try:
        await database.create_mobile_auth_session(state_hash, int(time.time()) + 600)
    except Exception as exc:
        logger.error(
            "Telegram session creation stage=storage code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось начать вход через Telegram", correlation_id,
        ) from None
    logger.info(
        "Telegram session creation stage=storage code=created correlation_id=%s",
        correlation_id,
    )

    return CreateTelegramSessionResponse(
        session_token=token,
        bot_url=f"https://t.me/ByMeVPNbot?start={token}",
    )


@api_router.get("/auth/telegram/check-session", response_model=TelegramCheckResponse)
async def check_telegram_session(
    token: str, request: Request, response: Response = None,
):
    state = token.removeprefix("auth_")
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest() if state else ""
    correlation_id = state_hash[:16] if state_hash else secrets.token_hex(8)
    request.state.correlation_id = correlation_id
    if response is not None:
        response.headers["X-Correlation-ID"] = correlation_id
    if not state or len(state) > 256:
        return TelegramCheckResponse(status="expired")
    # Do not burn the single-use Telegram state before JWT and subscription
    # preparation finishes. A transient panel/signer failure must be retryable.
    try:
        session = await database.get_mobile_auth_session(state_hash)
    except Exception as exc:
        logger.error(
            "Telegram session exchange stage=state_read code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось проверить подтверждение Telegram", correlation_id,
        ) from None
    if not session or session.get("status") == "expired":
        return TelegramCheckResponse(status="expired")
    if session.get("status") == "pending":
        return TelegramCheckResponse(status="pending")

    try:
        user_id = int(session["user_id"])
        username = session.get("username", "")
        full_name = session.get("full_name", "Telegram User")
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(
            "Telegram session exchange stage=confirmed_state_decode code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось завершить вход через Telegram", correlation_id,
        ) from None

    try:
        jwt_token = create_simple_jwt(user_id, f"{user_id}@telegram.bymevpn")
    except Exception as exc:
        logger.error(
            "Telegram session exchange stage=jwt_issue code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось завершить вход через Telegram", correlation_id,
        ) from None
    try:
        sub_url, servers, sub_dict = await _auth_subscription_snapshot(
            user_id, _subscription_headers_from_request(request)
        )
    except Exception as exc:
        logger.error(
            "Telegram session exchange stage=profile_snapshot code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось загрузить профиль для входа", correlation_id,
        ) from None

    try:
        consumed = await database.consume_mobile_auth_session(state_hash)
    except Exception as exc:
        logger.error(
            "Telegram session exchange stage=state_consume code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось завершить вход через Telegram", correlation_id,
        ) from None
    if not consumed:
        return TelegramCheckResponse(status="expired")

    logger.info(
        "Telegram session exchange stage=complete code=authenticated correlation_id=%s",
        correlation_id,
    )

    return TelegramCheckResponse(
        status="authenticated",
        access_token=jwt_token,
        refresh_token=f"ref_{jwt_token}",
        user={
            "id": str(user_id),
            "email": f"{username}@telegram",
            "name": full_name,
            "is_telegram": True,
        },
        subscription=sub_dict,
        servers=servers,
    )


check_telegram_session_body = register_telegram_check_post(
    api_router, check_telegram_session, TelegramCheckResponse
)


@api_router.post("/auth/telegram/cancel-session")
async def cancel_telegram_session(req: TelegramSessionCancelRequest):
    """Cancel a short-lived login state without exposing it in a URL."""
    state = req.token.removeprefix("auth_") if req.token else ""
    if not state or len(state) > 256:
        return {"status": "expired"}
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    canceled = await database.cancel_mobile_auth_session(state_hash)
    return {"status": "cancelled" if canceled else "expired"}


# ---------------------------------------------------------------------------
# 2. SECURE EMAIL REGISTRATION & LOGIN (Strict Password Verification)
# ---------------------------------------------------------------------------

@api_router.post("/auth/register")
async def register(req: RegisterRequest, request: Request):
    await _ensure_auth_db()
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Некорректный формат email")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Пароль должен содержать не менее 6 символов")

    db = await database.get_db()
    cur = await db.execute("SELECT user_id FROM app_users WHERE email = ?", (email,))
    if await cur.fetchone():
        raise HTTPException(status_code=400, detail="Пользователь с таким email уже зарегистрирован")

    salt = secrets.token_hex(16)
    pw_hash = hash_password(req.password, salt)
    user_id = int(hashlib.md5(email.encode()).hexdigest()[:8], 16)
    now = int(time.time())

    await db.execute(
        "INSERT INTO app_users (user_id, email, password_hash, salt, name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, email, pw_hash, salt, req.name or email.split("@")[0], now),
    )
    await db.commit()
    await database.ensure_user(user_id, email.split("@")[0])

    jwt_token = create_simple_jwt(user_id, email)
    sub_url, servers, sub_dict = await _auth_subscription_snapshot(
        user_id, _subscription_headers_from_request(request)
    )

    return {
        "access": jwt_token,
        "refresh": f"ref_{jwt_token}",
        "user": {
            "id": str(user_id),
            "email": email,
            "name": req.name or email.split("@")[0],
            "is_google": False,
        },
        "subscription": sub_dict,
        "servers": servers,
    }


@api_router.post("/auth/login")
async def login(req: LoginRequest, request: Request):
    await _ensure_auth_db()
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Введите корректный email")

    db = await database.get_db()
    cur = await db.execute(
        "SELECT user_id, password_hash, salt, name FROM app_users WHERE email = ?",
        (email,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    user_id, stored_hash, salt, name = row
    computed_hash = hash_password(req.password, salt)
    if not hmac.compare_digest(computed_hash, stored_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    jwt_token = create_simple_jwt(user_id, email)
    sub_url, servers, sub_dict = await _auth_subscription_snapshot(
        user_id, _subscription_headers_from_request(request)
    )

    return {
        "access": jwt_token,
        "refresh": f"ref_{jwt_token}",
        "user": {
            "id": str(user_id),
            "email": email,
            "name": name or email.split("@")[0],
            "is_google": False,
        },
        "subscription": sub_dict,
        "servers": servers,
    }


def _google_signin_configured() -> bool:
    return (
        os.getenv("GOOGLE_SIGNIN_ENABLED") == "1"
        and bool(os.getenv("GOOGLE_WEB_CLIENT_ID", "").strip())
    )


@api_router.post("/auth/google/nonce")
async def create_google_nonce():
    """Issue a short-lived server nonce only when Google login is configured."""
    if not _google_signin_configured():
        raise HTTPException(status_code=503, detail="Вход через Google временно недоступен")
    nonce = secrets.token_urlsafe(32)
    nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    try:
        await database.create_mobile_google_nonce(nonce_hash, int(time.time()) + 300)
    except Exception as exc:
        logger.error("Google sign-in challenge storage unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Не удалось начать вход через Google") from None
    return {"nonce": nonce}


@api_router.post("/auth/google")
async def login_google(req: GoogleLoginRequest, request: Request):
    correlation_id = secrets.token_hex(8)

    def reject(stage: str, code: str, status_code: int, detail: str) -> None:
        # Only fixed, non-sensitive stage/code values and a random correlation
        # ID enter logs. Never log provider exception text or token claims.
        logger.warning(
            "Google sign-in rejected stage=%s code=%s correlation_id=%s",
            stage, code, correlation_id,
        )
        raise HTTPException(
            status_code=status_code, detail=detail,
            headers={"X-Correlation-ID": correlation_id},
        )

    if not _google_signin_configured():
        reject("configuration", "google_signin_unavailable", 503,
               "Вход через Google временно недоступен")

    expected_audience = os.getenv("GOOGLE_WEB_CLIENT_ID", "").strip()
    try:
        claims = await asyncio.to_thread(verify_google_id_token, req.id_token, expected_audience)
    except GoogleIdTokenError as exc:
        reject("id_token", exc.code, 401, "Не удалось подтвердить аккаунт Google")

    if not hmac.compare_digest(claims["nonce"], req.nonce):
        reject("nonce", "nonce_mismatch", 401, "Не удалось подтвердить запрос входа")
    nonce_hash = hashlib.sha256(req.nonce.encode("utf-8")).hexdigest()
    try:
        consumed = await database.consume_mobile_google_nonce(nonce_hash)
    except Exception as exc:
        reject("nonce", "nonce_store_unavailable", 503,
               "Не удалось подтвердить запрос входа")
    if not consumed:
        reject("nonce", "nonce_expired_or_replayed", 401,
               "Запрос входа истёк или уже использован")

    try:
        await _ensure_auth_db()
        identity = await database.get_or_create_google_account(
            claims["sub"], claims["email"], claims["name"],
        )
    except Exception as exc:
        reject("account", "account_store_unavailable", 503,
               "Не удалось открыть аккаунт")

    user_id = int(identity["user_id"])
    jwt_token = create_simple_jwt(user_id, identity["email"])
    _, servers, sub_dict = await _auth_subscription_snapshot(
        user_id, _subscription_headers_from_request(request)
    )
    return {
        "access": jwt_token,
        "refresh": f"ref_{jwt_token}",
        "user": {
            "id": str(user_id),
            "email": identity["email"],
            "name": identity["name"],
            "is_google": True,
        },
        "subscription": sub_dict,
        "servers": servers,
    }


@api_router.post("/auth/refresh")
async def refresh_token(req: RefreshRequest):
    token = req.refresh.removeprefix("ref_")
    claims = _verified_token_claims(token)
    if not claims:
        raise HTTPException(status_code=401, detail="Сессия истекла")
    try:
        if await database.is_mobile_jwt_revoked(claims["jti"]):
            raise HTTPException(status_code=401, detail="Сессия завершена")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("JWT revocation check unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Сессия временно недоступна") from None
    # Refresh preserves the same revocable session and its absolute expiry.
    new_access = create_simple_jwt(
        claims["user_id"], claims["email"],
        issued_at=claims["issued_at"], expires_at=claims["expires_at"],
        jwt_id=claims["jti"],
    )
    return {
        "access": new_access,
        "refresh": f"ref_{new_access}",
    }


@api_router.post("/auth/logout")
async def logout(
    authorization: Optional[str] = Header(None),
    user_id: int = Depends(get_current_user_id),
):
    token = (authorization or "").removeprefix("Bearer ").strip()
    claims = _verified_token_claims(token)
    if not claims or claims["user_id"] != user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")
    try:
        await database.revoke_mobile_jwt(claims["jti"], claims["expires_at"])
    except Exception as exc:
        logger.error("JWT revocation failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Не удалось завершить сессию. Попробуйте позже") from None
    return {"status": "ok"}


@api_router.get("/me")
async def get_me(user_id: int = Depends(get_current_user_id)):
    user = await database.find_user_by_id(user_id)
    username = user.get("username", "") if user else ""
    # Restore an actual registered email/name instead of manufacturing a false
    # @bymevpn.app address for signed-in desktop accounts.
    db = await database.get_db()
    try:
        cursor = await db.execute("SELECT email,name FROM google_accounts WHERE user_id=?", (user_id,))
        row = await cursor.fetchone()
    except Exception:
        row = None  # Telegram identities do not necessarily have app_users rows.
    is_google = bool(row)
    if not row:
        try:
            cursor = await db.execute("SELECT email,name FROM app_users WHERE user_id=?", (user_id,))
            row = await cursor.fetchone()
        except Exception:
            row = None
    email = str(row[0]) if row else ""
    display_name = str(row[1] or "") if row else ""
    return {
        "user": {
            "id": str(user_id),
            "email": email,
            "name": display_name or username or f"User {user_id}",
            "is_google": is_google,
        }
    }


@api_router.delete("/me")
async def delete_me(user_id: int = Depends(get_current_user_id)):
    # The mobile account-deletion flow is not implemented yet. Never report
    # success while leaving the account and subscription untouched.
    raise HTTPException(
        status_code=501,
        detail="Удаление аккаунта пока недоступно. Обратитесь в поддержку",
    )


# ---------------------------------------------------------------------------
# 3. SUBSCRIPTION & TRIAL (Calls 3x-ui to Create Real Clients)
# ---------------------------------------------------------------------------

@api_router.get("/subscription/status")
async def get_subscription_status(request: Request, user_id: int = Depends(get_current_user_id)):
    headers = _subscription_headers_from_request(request)
    sub_url, servers, sub_dict = await get_user_real_subscription(
        user_id, headers, include_device_count=False
    )
    try:
        sub_dict = await _attach_signed_entitlement(user_id, headers, sub_dict)
    except EntitlementSigningError as exc:
        logger.error("Entitlement signing unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Не удалось подтвердить состояние подписки") from None
    return sub_dict


@api_router.post("/subscription/trial")
async def activate_trial(
    req: TrialRequest, request: Request, response: Response = None,
    user_id: int = Depends(get_current_user_id),
):
    """Atomically grant one free 3-day trial to a user without a subscription."""
    from constants import DEFAULT_DEVICE_LIMIT, TRIAL_DAYS

    correlation_id = _request_correlation_id(request)
    if response is not None:
        response.headers["X-Correlation-ID"] = correlation_id
    trial_days = TRIAL_DAYS
    trial_device_limit = DEFAULT_DEVICE_LIMIT
    headers = _subscription_headers_from_request(request)
    wants_entitlement = _entitlement_requested(headers)
    if wants_entitlement:
        try:
            load_private_key()
        except EntitlementSigningError as exc:
            logger.error(
                "Trial failure stage=signer code=%s correlation_id=%s",
                type(exc).__name__, correlation_id,
            )
            raise _correlated_http_error(
                503, "Не удалось подтвердить состояние подписки", correlation_id,
            ) from None
        # Validate identity and the client/server binding, but do not require
        # panel registration yet: 3x-ui learns an installation HWID only after
        # its first VPN connection. A fixed ten-minute bootstrap below enables
        # that first connection; it is never treated as panel confirmation.
        hwid = str(headers.get("X-HWID") or "").strip()
        try:
            device_binding(user_id, hwid)
        except EntitlementSigningError:
            raise _correlated_http_error(
                422, "Не удалось подтвердить это устройство", correlation_id,
            ) from None
        if req.install_id and not hmac.compare_digest(req.install_id.strip(), hwid):
            raise _correlated_http_error(422, "Данные устройства не совпадают", correlation_id)
    try:
        if await database.has_trial_used(user_id):
            raise _correlated_http_error(
                409, "Пробный период уже использован", correlation_id,
            )
        current_keys = await database.get_user_keys(user_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Trial failure stage=user_eligibility code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось проверить доступность пробного периода", correlation_id,
        ) from None
    now = int(time.time())
    try:
        has_active_subscription = any(
            int(key.get("expiry") or 0) > now for key in current_keys
        )
    except (TypeError, ValueError):
        logger.warning(
            "Trial failure stage=subscription_expiry_parse code=invalid_database_expiry correlation_id=%s",
            correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось проверить доступность пробного периода", correlation_id,
        ) from None
    if has_active_subscription:
        raise _correlated_http_error(
            409, "У вас уже есть действующая подписка", correlation_id,
        )
    try:
        from xui_client import get_xui_client_details
        panel_client = await get_xui_client_details(user_id, strict=True)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Trial failure stage=eligibility_panel_lookup code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось проверить доступность пробного периода", correlation_id,
        ) from None
    try:
        panel_expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
    except (TypeError, ValueError):
        logger.warning(
            "Trial failure stage=eligibility_expiry_parse code=invalid_panel_expiry correlation_id=%s",
            correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось проверить доступность пробного периода", correlation_id,
        ) from None
    if panel_client and (panel_expiry <= 0 or panel_expiry > now):
        raise _correlated_http_error(
            409, "Для этого аккаунта уже создана подписка", correlation_id,
        )

    try:
        claimed = await database.try_claim_trial(user_id)
    except Exception as exc:
        logger.error(
            "Trial failure stage=trial_claim code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось активировать пробный период", correlation_id,
        ) from None
    if not claimed:
        raise _correlated_http_error(
            409, "Пробный период уже использован", correlation_id,
        )

    async def release_trial_only_when_panel_confirms_no_active_access() -> None:
        """Undo a claim only when panel readback proves no active client exists.

        A timeout or unavailable readback is ambiguous: keep the claim reserved
        so a retry cannot extend a client that may have been created remotely.
        """
        try:
            from xui_client import get_xui_client_details
            panel_client = await get_xui_client_details(user_id, strict=True)
            panel_expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
        except Exception as exc:
            logger.warning(
                "Trial failure stage=claim_release_readback code=%s correlation_id=%s",
                type(exc).__name__, correlation_id,
            )
            return
        if not panel_client or panel_expiry <= int(time.time()):
            try:
                await database.reset_trial_for_user(user_id)
            except Exception as exc:
                logger.warning(
                    "Trial failure stage=claim_release code=%s correlation_id=%s",
                    type(exc).__name__, correlation_id,
                )

    try:
        from xui_client import create_xui_user
        res = await create_xui_user(
            user_id=user_id,
            days=trial_days,
            limit_ip=0,
            limit_hwid=trial_device_limit,
        )
    except Exception as exc:
        logger.error(
            "Trial failure stage=panel_provision code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        await release_trial_only_when_panel_confirms_no_active_access()
        raise _correlated_http_error(
            503, "Не удалось подключить пробный период. Напишите в поддержку", correlation_id,
        ) from None
    if not res or not res.get("subscription_url"):
        await release_trial_only_when_panel_confirms_no_active_access()
        raise _correlated_http_error(
            503, "Не удалось подключить пробный период. Напишите в поддержку", correlation_id,
        )

    # Do not persist or report a trial until the panel confirms the actual
    # HWID/IP limits. The legacy DB column is named `limit_ip`, but the bot
    # stores the device allowance there; the panel itself must keep IPs open.
    try:
        from xui_client import get_xui_devices
        panel_devices = await get_xui_devices(user_id)
        if (
            not isinstance(panel_devices, dict)
            or int(panel_devices.get("limit_hwid") or 0) != trial_device_limit
            or int(panel_devices.get("limit_ip") or 0) != 0
            or not isinstance(panel_devices.get("devices"), list)
        ):
            raise RuntimeError("Panel trial limits did not match the server catalog")
    except Exception as exc:
        # The panel may already have created a client. Keep the atomic claim
        # reserved on an uncertain readback; retrying must not extend access.
        logger.warning(
            "Trial failure stage=panel_readback code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось подтвердить параметры пробного периода", correlation_id,
        ) from None

    sub_url = res["subscription_url"]
    expiry_ts = int(time.time()) + trial_days * 86400
    try:
        await database.add_key(
            user_id=user_id,
            key=sub_url,
            remark="Пробный период — 3 дня",
            uuid=str(res.get("uuid") or ""),
            days=trial_days,
            limit_ip=trial_device_limit,
            expiry=expiry_ts,
        )
    except Exception as exc:
        logger.error(
            "Trial failure stage=database_save code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Пробный период создан, но его статус не сохранился. Напишите в поддержку", correlation_id,
        ) from None

    if wants_entitlement:
        hwid = str(headers.get("X-HWID") or "").strip()
        if _panel_state_contains_hwid(user_id, hwid, panel_devices):
            await database.delete_mobile_trial_bootstrap(user_id)
        else:
            # The trial claim is already atomically reserved and the panel has
            # confirmed the 2-HWID / unlimited-IP client. Bind a fixed window
            # to this account + installation digest. It cannot be renewed by
            # retrying the trial request or by changing the request header.
            bootstrap_expiry = int(time.time()) + MOBILE_TRIAL_BOOTSTRAP_TTL_SECONDS
            try:
                reserved = await database.create_mobile_trial_bootstrap(
                    user_id, device_binding(user_id, hwid), bootstrap_expiry,
                )
            except Exception as exc:
                logger.error(
                    "Trial failure stage=bootstrap_persist code=%s correlation_id=%s",
                    type(exc).__name__, correlation_id,
                )
                raise _correlated_http_error(
                    503, "Не удалось подтвердить первое подключение", correlation_id,
                ) from None
            if not reserved:
                raise _correlated_http_error(
                    409, "Пробный период уже использован", correlation_id,
                )

    try:
        servers = await fetch_and_parse_real_nodes(
            sub_url, request_headers=_subscription_headers_from_request(request)
        )
    except Exception as exc:
        logger.error(
            "Trial failure stage=subscription_fetch code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Пробный период создан, но список серверов пока недоступен", correlation_id,
        ) from None

    trial_status = {
        "status": "trial",
        "plan_name": "Пробный период (3 дня)",
        "expires_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry_ts)),
        "seconds_remaining": trial_days * 86400,
        "subscription_url": sub_url,
        "max_devices": trial_device_limit,
        "active_devices": len(panel_devices["devices"]),
        "servers": servers,
    }
    try:
        result = await _attach_signed_entitlement(
            user_id, _subscription_headers_from_request(request), trial_status
        )
    except EntitlementSigningError as exc:
        logger.error(
            "Trial failure stage=entitlement_sign code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось подтвердить состояние подписки", correlation_id,
        ) from None
    logger.info(
        "Trial activation stage=complete code=activated correlation_id=%s",
        correlation_id,
    )
    return result


@api_router.get("/plans")
async def get_plans(devices: int = 2):
    """Server-owned tariff catalog with legacy and desktop plan identifiers."""
    from constants import get_price_for_months, VALID_DEVICE_LIMITS
    if devices not in VALID_DEVICE_LIMITS:
        raise HTTPException(status_code=400, detail="Выберите 2, 5 или 10 устройств")

    plan_catalog = [
        ("1m", "month", 1, "1 месяц", 0, False),
        ("3m", "quarter", 3, "4 месяца (3+1)", 11, True),
        ("6m", "half_year", 6, "8 месяцев (6+2)", 22, False),
        ("12m", "year", 12, "15 месяцев (12+3)", 34, False),
    ]
    plans = []
    for code, desktop_code, months, label, discount, popular in plan_catalog:
        options = [
            {"devices": limit, "price_rub": get_price_for_months(months, limit)[0],
             "days": get_price_for_months(months, limit)[1]}
            for limit in VALID_DEVICE_LIMITS
        ]
        selected = next(option for option in options if option["devices"] == devices)
        plans.append({
            "code": code, "desktop_code": desktop_code, "name": label,
            "price": f"{selected['price_rub']} ₽", "price_rub": selected["price_rub"],
            "days": selected["days"], "devices": devices, "device_options": options,
            "discount_percent": discount, "is_popular": popular,
        })
    return {
        "trial": {"code": "trial", "name": "3 дня бесплатно",
                  "price": "0 ₽", "price_rub": 0, "days": 3, "devices": 2},
        "plans": plans,
    }

# ---------------------------------------------------------------------------
@api_router.post("/payment/create")
async def api_create_payment(req: PaymentCreateRequest, user_id: int = Depends(get_current_user_id)):
    """Create an owned, idempotent checkout from the server-owned tariff catalog."""
    plan_map = {
        "1m": 1, "month": 1,
        "3m": 3, "quarter": 3,
        "6m": 6, "half_year": 6,
        "12m": 12, "year": 12,
    }
    tier_match = re.fullmatch(r"(1m|3m|6m|12m)_d(2|5|10)", req.plan_code)
    plan_code = tier_match.group(1) if tier_match else req.plan_code
    mobile_v2_plan_code = req.plan_code if tier_match else None
    months = plan_map.get(plan_code)
    if months is None:
        raise HTTPException(status_code=400, detail="Неизвестный тариф")
    provider = req.provider or "yookassa"
    if provider not in ("yookassa", "crypto"):
        raise HTTPException(status_code=400, detail="Этот способ оплаты недоступен в приложении")
    if not req.request_id or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", req.request_id):
        raise HTTPException(status_code=400, detail="Создайте новый запрос на оплату")

    from constants import get_price_for_months, VALID_DEVICE_LIMITS
    devices = int(tier_match.group(2)) if tier_match else (2 if req.devices is None else req.devices)
    if tier_match and req.devices is not None and int(req.devices) != devices:
        raise HTTPException(status_code=422, detail="Тариф и лимит устройств не совпадают")
    if devices not in VALID_DEVICE_LIMITS:
        raise HTTPException(status_code=422, detail="Недопустимое количество устройств")
    # Price and term come from the backend catalog. Reject stale or altered
    # client values before reserving an invoice.
    amount_rub, days = get_price_for_months(months, devices)
    if req.amount_rub is not None and req.amount_rub != amount_rub:
        raise HTTPException(status_code=422, detail="Стоимость тарифа изменилась. Обновите тарифы")
    if req.days is not None and req.days != days:
        raise HTTPException(status_code=422, detail="Срок тарифа изменился. Обновите тарифы")
    reserved = await database.ensure_mobile_payment_request(
        user_id, req.request_id, plan_code, provider, devices,
    )
    if (
        reserved["plan_code"] != plan_code
        or reserved["provider"] != provider
        or int(reserved.get("devices", 2)) != devices
    ):
        raise HTTPException(status_code=409, detail="Этот запрос уже использован для другого тарифа")

    if provider == "crypto":
        from payments import create_crypto_payment, find_crypto_invoice_by_payload, get_crypto_invoice

        def _crypto_payload(generation: int) -> str:
            material = f"mobile:{user_id}:{req.request_id}:{mobile_v2_plan_code or plan_code}:{devices}:{generation}"
            return "bm1_" + hashlib.sha256(material.encode()).hexdigest()

        async def _bind_crypto_invoice(invoice_id: str, payment_url: str, payload: str) -> None:
            saved = await database.save_mobile_crypto_checkout(
                user_id, req.request_id, plan_code, devices, invoice_id,
                payment_url, days, amount_rub, payload,
            )
            if not saved:
                raise HTTPException(
                    status_code=503,
                    detail="Счёт создан, но его безопасное сохранение не подтверждено. Повторите проверку позже",
                )

        async def _rotate_expired_crypto(invoice_id: str, generation: int) -> dict:
            rotated = await database.advance_mobile_payment_request_generation(
                user_id, req.request_id, plan_code, provider, invoice_id, generation,
            )
            if rotated:
                await database.delete_crypto_pending(invoice_id)
            latest = await database.ensure_mobile_payment_request(
                user_id, req.request_id, plan_code, provider, devices,
            )
            if latest.get("payment_id") == invoice_id:
                raise HTTPException(status_code=503, detail="Не удалось обновить просроченный счёт. Попробуйте позже")
            return latest

        if reserved.get("payment_id") and reserved.get("confirmation_url"):
            previous_invoice = str(reserved["payment_id"])
            invoice = await get_crypto_invoice(previous_invoice)
            provider_status = invoice.get("status") if invoice else None
            if provider_status == "expired":
                reserved = await _rotate_expired_crypto(previous_invoice, reserved["generation"])
            else:
                # Reinsert the durable fulfillment intent if a previous process
                # stopped between binding the invoice and queueing its delivery.
                payload = _crypto_payload(reserved["generation"])
                await _bind_crypto_invoice(previous_invoice, reserved["confirmation_url"], payload)
                return {
                    "payment_url": reserved["confirmation_url"],
                    "invoice_id": previous_invoice,
                    "provider": "crypto",
                }

        payload = _crypto_payload(reserved["generation"])
        if reserved.get("creation_state") == "creating":
            # Crypto Pay has no documented create idempotency key. Recover an
            # invoice by its opaque payload after an ambiguous timeout; never
            # issue a second invoice when absence cannot be proven.
            scan_complete, invoice = await find_crypto_invoice_by_payload(payload)
            if not scan_complete:
                raise HTTPException(
                    status_code=503,
                    detail="Проверяем создание счёта. Не запускайте оплату повторно; попробуйте позже",
                )
            if invoice:
                invoice_id = str(invoice.get("invoice_id") or "")
                payment_url = invoice.get("bot_invoice_url") or invoice.get("web_app_invoice_url")
                if not invoice_id or not payment_url:
                    raise HTTPException(status_code=503, detail="Найденный счёт пока недоступен для оплаты")
                await _bind_crypto_invoice(invoice_id, payment_url, payload)
                if invoice.get("status") == "expired":
                    reserved = await _rotate_expired_crypto(invoice_id, reserved["generation"])
                    payload = _crypto_payload(reserved["generation"])
                else:
                    return {"payment_url": payment_url, "invoice_id": invoice_id, "provider": "crypto"}
            else:
                raise HTTPException(
                    status_code=503,
                    detail="Не удалось однозначно сверить создание счёта. Обратитесь в поддержку",
                )

        claimed = await database.claim_mobile_payment_creation(
            user_id, req.request_id, plan_code, provider,
        )
        if not claimed:
            raise HTTPException(status_code=409, detail="Создание счёта уже выполняется. Повторите проверку позже")
        from payments import CryptoPayCreateError
        try:
            result = await create_crypto_payment(
                amount_rub=amount_rub,
                description=f"ByMeVPN — {days} дней, {devices} устройства",
                user_id=user_id,
                days=days,
                devices=devices,
                external_payload=payload,
                raise_errors=True,
            )
        except CryptoPayCreateError as exc:
            if not exc.outcome_unknown:
                # A definite provider rejection created no invoice, so allow
                # this same idempotent request to retry safely.
                await database.reset_mobile_payment_creation(
                    user_id, req.request_id, plan_code, provider,
                )
                raise HTTPException(
                    status_code=502,
                    detail="Провайдер отклонил создание счёта. Попробуйте ещё раз позже",
                ) from exc
            # A timeout or incomplete response may hide an accepted invoice.
            # Keep 'creating'; the next retry must reconcile by opaque payload.
            raise HTTPException(
                status_code=503,
                detail="Проверяем создание счёта. Не запускайте оплату повторно; попробуйте позже",
            ) from exc
        if not result:
            # Legacy helpers can still return None. Treat the outcome as unknown
            # and reconcile on retry rather than risking a duplicate invoice.
            raise HTTPException(status_code=503, detail="Не удалось подтвердить создание счёта. Повторите проверку позже")
        payment_url, invoice_id = result
        await _bind_crypto_invoice(str(invoice_id), payment_url, payload)
        return {"payment_url": payment_url, "invoice_id": str(invoice_id), "provider": "crypto"}

    if reserved.get("payment_id") and reserved.get("confirmation_url"):
        from payments import get_yookassa_payment_status

        previous_invoice = reserved["payment_id"]
        provider_status = await get_yookassa_payment_status(previous_invoice)
        if provider_status == "canceled":
            await database.advance_mobile_payment_request_generation(
                user_id, req.request_id, plan_code, provider,
                previous_invoice, reserved["generation"],
            )
            # A simultaneous retry may have rotated the request already.
            reserved = await database.ensure_mobile_payment_request(
                user_id, req.request_id, plan_code, provider, devices,
            )
            if reserved["payment_id"] == previous_invoice:
                raise HTTPException(status_code=503, detail="Не удалось повторить оплату. Попробуйте позже")
            if reserved.get("payment_id") and reserved.get("confirmation_url"):
                return {
                    "payment_url": reserved["confirmation_url"],
                    "invoice_id": reserved["payment_id"],
                    "provider": "yookassa",
                }
        else:
            # Unknown status is not proof of cancellation: preserve the invoice.
            return {
                "payment_url": reserved["confirmation_url"],
                "invoice_id": reserved["payment_id"],
                "provider": "yookassa",
            }

    from payments import create_yookassa_payment
    stable_key = hashlib.sha256(
        f"bymevpn-mobile:{user_id}:{req.request_id}:{reserved['generation']}".encode()
    ).hexdigest()
    result = await create_yookassa_payment(
        amount_rub=amount_rub,
        description=f"ByMeVPN — {days} дней, {devices} устройства",
        user_id=user_id,
        days=days,
        devices=devices,
        months=months,
        extra_metadata={
            "app_payment": "1",
            "plan_code": f"{plan_code}_d{devices}",
            "mobile_request_hash": hashlib.sha256(req.request_id.encode()).hexdigest()[:20],
            # Every invoice created through this JWT-authenticated mobile API
            # has a durable mobile_payment_requests row, including clients
            # still using the pre-V2 route. Keep the V2 marker for existing
            # diagnostics, but bind all new API invoices in fulfillment.
            "mobile_api_checkout": "1",
            **({"mobile_v2": "1"} if mobile_v2_plan_code else {}),
        },
        idempotency_key=stable_key,
        return_payment_details=True,
        save_payment_method=False,
    )
    if not isinstance(result, dict) or not result.get("payment_url") or not result.get("payment_id"):
        raise HTTPException(status_code=502, detail="Не удалось создать платёж. Попробуйте позже")
    if result.get("status") not in (None, "pending", "waiting_for_capture"):
        raise HTTPException(status_code=502, detail="Платёжная страница недоступна. Попробуйте позже")
    saved = await database.save_mobile_payment_request(
        user_id, req.request_id, plan_code, provider,
        result["payment_id"], result["payment_url"],
    )
    if not saved:
        raise HTTPException(status_code=503, detail="Платёж создан, но статус запроса не сохранён. Повторите проверку")
    return {
        "payment_url": result["payment_url"],
        "invoice_id": result["payment_id"],
        "provider": "yookassa",
    }


@api_router.get("/payment/check")
async def api_check_payment(
    request: Request,
    invoice_id: Optional[str] = None,
    provider: Optional[str] = None,
    user_id: int = Depends(get_current_user_id),
):
    """Check one owned invoice; provider success alone never grants access."""
    headers = _subscription_headers_from_request(request)
    _entitlement_requested(headers)  # Reject explicitly unsupported versions on every path.
    if not invoice_id:
        recent = await database.list_recent_mobile_invoice_refs(user_id)
        if len(recent) != 1:
            raise HTTPException(status_code=400, detail="Укажите номер счёта для проверки оплаты")
        invoice_id = recent[0]["payment_id"]
        provider = recent[0]["provider"]
    provider = provider or "yookassa"
    if provider not in ("yookassa", "crypto") or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", invoice_id):
        raise HTTPException(status_code=400, detail="Некорректные данные счёта")
    record = await database.get_mobile_payment_record(provider, invoice_id)
    if not record or record["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Счёт не найден")

    if record["status"] != "success":
        if provider == "yookassa":
            from payments import get_yookassa_payment_status
            provider_status = await get_yookassa_payment_status(invoice_id)
        else:
            from payments import get_crypto_invoice
            provider_payment = await get_crypto_invoice(invoice_id)
            provider_status = provider_payment.get("status") if provider_payment else None
        if provider_status in ("canceled", "expired"):
            return {"status": "failed", "is_active": False, "subscription": None}
        return {"status": "processing" if provider_status == "succeeded" or provider_status == "paid" else "pending",
                "is_active": False, "subscription": None}

    sub_url, servers, sub_dict = await get_user_real_subscription(
        user_id, headers
    )
    active = sub_dict.get("status") in ("active", "trial", "grace")
    if active:
        try:
            sub_dict = await _attach_signed_entitlement(user_id, headers, sub_dict)
        except EntitlementSigningError as exc:
            logger.error("Payment-check entitlement signing unavailable (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось подтвердить состояние подписки") from None
    return {"status": "succeeded" if active else "processing", "is_active": active,
            "subscription": sub_dict if active else None}


# Mobile v2 is deliberately namespaced so a compatibility patch can be added
# beside the legacy production routes without replacing the router or changing
# Telegram bot checkout callbacks. Its plan_code binds period and HWID tier.
mobile_v2_router = APIRouter()


@mobile_v2_router.get("/plans")
async def get_mobile_v2_plans(devices: int = 2):
    from constants import get_price_for_months, VALID_DEVICE_LIMITS

    if devices not in VALID_DEVICE_LIMITS:
        raise HTTPException(status_code=400, detail="Выберите 2, 5 или 10 устройств")
    payment_enabled = os.getenv("MOBILE_V2_WEBHOOK_GUARD_ENABLED") == "1"
    catalog = [
        ("1m", "month", 1, "1 месяц", 0, False),
        ("3m", "quarter", 3, "4 месяца (3+1)", 11, True),
        ("6m", "half_year", 6, "8 месяцев (6+2)", 22, False),
        ("12m", "year", 12, "15 месяцев (12+3)", 34, False),
    ]
    plans = []
    for period_code, desktop_code, months, label, discount, popular in catalog:
        options = []
        for device_count in VALID_DEVICE_LIMITS:
            amount, days = get_price_for_months(months, device_count)
            options.append({
                "plan_code": f"{period_code}_d{device_count}",
                "devices": device_count,
                "price_rub": amount,
                "days": days,
            })
        selected = next(option for option in options if option["devices"] == devices)
        plans.append({
            "code": selected["plan_code"],
            "desktop_code": desktop_code,
            "name": label,
            "price": f"{selected['price_rub']} ₽",
            "price_rub": selected["price_rub"],
            "days": selected["days"],
            "devices": devices,
            "device_options": options,
            "discount_percent": discount,
            "is_popular": popular,
            "purchase_enabled": payment_enabled,
        })
    return {
        "trial": {
            "code": "trial", "name": "3 дня бесплатно", "price": "0 ₽",
            "price_rub": 0, "days": 3, "devices": 2,
        },
        "payment_enabled": payment_enabled,
        "purchase_enabled": payment_enabled,
        "plans": plans,
    }


@mobile_v2_router.post("/payment/create")
async def create_mobile_v2_payment(
    req: MobileV2PaymentCreateRequest,
    user_id: int = Depends(get_current_user_id),
):
    if os.getenv("MOBILE_V2_WEBHOOK_GUARD_ENABLED") != "1":
        raise HTTPException(status_code=503, detail="Мобильная оплата временно недоступна")
    match = re.fullmatch(r"(1m|3m|6m|12m)_d(2|5|10)", req.plan_code)
    if not match:
        raise HTTPException(status_code=400, detail="Неизвестный тариф")
    if req.provider not in ("yookassa", "crypto"):
        raise HTTPException(status_code=400, detail="Этот способ оплаты недоступен в приложении")

    # Device count is parsed from the server-issued plan code, never from a
    # client-supplied `devices`, price, or duration field.
    legacy_request = PaymentCreateRequest(
        plan_code=req.plan_code,
        request_id=req.request_id,
        provider=req.provider,
    )
    return await api_create_payment(legacy_request, user_id=user_id)


@mobile_v2_router.get("/payment/check")
async def check_mobile_v2_payment(
    request: Request,
    invoice_id: str,
    provider: str,
    user_id: int = Depends(get_current_user_id),
):
    return await api_check_payment(
        request=request,
        invoice_id=invoice_id,
        provider=provider,
        user_id=user_id,
    )


api_router.include_router(mobile_v2_router, prefix="/mobile/v2")


# 4. SERVERS & VPN SESSIONS (Real Nodes Only)
# ---------------------------------------------------------------------------

@api_router.get("/servers")
async def get_servers(request: Request, user_id: int = Depends(get_current_user_id)):
    headers = await _require_opted_in_device_access(user_id, request)
    sub_url, servers, sub_dict = await get_user_real_subscription(
        user_id, headers
    )
    return {
        "servers": servers,
        "subscription_url": sub_url,
    }


@api_router.post("/vpn/session")
async def create_vpn_session(req: SessionRequest, request: Request, user_id: int = Depends(get_current_user_id)):
    headers = await _require_opted_in_device_access(user_id, request)
    sub_url, servers, sub_dict = await get_user_real_subscription(
        user_id, headers
    )
    if not servers:
        raise HTTPException(
            status_code=403,
            detail="Нет активной подписки или серверов. Оформите подписку или активируйте пробный период.",
        )

    def compatible_with_desktop(server: dict) -> bool:
        conf = server.get("config") or {}
        return (conf.get("protocol") == "vless"
                and conf.get("security") == "reality"
                and conf.get("network", "tcp") in ("tcp", "raw")
                and bool(conf.get("public_key")))

    is_windows = request.headers.get("x-device-os", "").casefold() == "windows"
    if req.node_code == "auto":
        choices = [node for node in servers if compatible_with_desktop(node)] if is_windows else servers
        if not choices:
            raise HTTPException(status_code=422, detail="Нет серверов с поддерживаемым протоколом для Windows")
        matched = choices[0]
    else:
        matched = next((s for s in servers if s.get("node_code") == req.node_code), None)
        if matched is None:
            raise HTTPException(status_code=404, detail="Выбранный сервер больше не доступен")
        if is_windows and not compatible_with_desktop(matched):
            raise HTTPException(status_code=422, detail="Выбранный сервер не поддерживается Windows")
    config = matched.get("config")
    if not config or not config.get("uuid"):
        raise HTTPException(status_code=500, detail="Неполная конфигурация VPN-узла на сервере")
    if config.get("security") == "reality" and not config.get("public_key"):
        raise HTTPException(status_code=500, detail="Неполная конфигурация VPN-узла на сервере")

    return config


@api_router.delete("/vpn/session")
async def delete_vpn_session(user_id: int = Depends(get_current_user_id)):
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 5. DEVICES MANAGEMENT
# ---------------------------------------------------------------------------

def _device_age_label(timestamp: int) -> str:
    """Return a bounded, localized relative age for the mobile device list."""
    elapsed = max(0, int(time.time()) - max(0, int(timestamp or 0)))
    if elapsed < 60:
        return "Сейчас"
    if elapsed < 3600:
        return f"{elapsed // 60} мин. назад"
    if elapsed < 86400:
        return f"{elapsed // 3600} ч. назад"
    return f"{elapsed // 86400} дн. назад"


def _device_is_current(item: dict, request: Request) -> bool:
    """Compare the client HWID with 3x-ui's short SHA-256 fingerprint in-process."""
    hwid = request.headers.get("x-hwid", "").strip()[:256]
    fingerprint = str(item.get("fingerprint") or "").strip().lower()
    if not hwid or not fingerprint or not 12 <= len(fingerprint) <= 64:
        return False
    if not re.fullmatch(r"[0-9a-f]+", fingerprint):
        return False
    digest = hashlib.sha256(hwid.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest[:len(fingerprint)], fingerprint)


def _device_metadata_matches(item: dict, request: Request) -> bool:
    model = request.headers.get("x-device-model", "").strip()[:120]
    platform = request.headers.get("x-device-os", "").strip()[:80]
    os_version = request.headers.get("x-ver-os", "").strip()[:80]
    user_agent = request.headers.get("user-agent", "").strip()[:160]
    return bool(
        model and platform and os_version and user_agent
        and str(item.get("model") or "").strip() == model
        and str(item.get("os") or "").strip().casefold() == platform.casefold()
        and str(item.get("os_version") or "").strip() == os_version
        and str(item.get("user_agent") or "").strip() == user_agent
    )


@api_router.get("/devices")
async def get_devices(request: Request, user_id: int = Depends(get_current_user_id)):
    try:
        from xui_client import get_xui_devices

        result = await get_xui_devices(user_id)
    except Exception as exc:
        logger.warning("Device list unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Список устройств временно недоступен") from None

    raw_devices = result.get("devices", [])
    has_hwid = bool(request.headers.get("x-hwid", "").strip())
    metadata_matches = [item for item in raw_devices if _device_metadata_matches(item, request)]
    devices = []
    for item in raw_devices:
        model = str(item.get("model") or "").strip()[:120]
        platform = str(item.get("os") or "").strip()[:80]
        devices.append({
            "id": str(item["id"]),
            "device_model": model or "Устройство",
            "platform": platform or "Неизвестная система",
            "last_active": _device_age_label(item.get("last_seen", 0)),
            "is_current": (
                _device_is_current(item, request)
                if has_hwid else len(metadata_matches) == 1 and item is metadata_matches[0]
            ),
        })
    return {
        "devices": devices,
        "device_limit": int(result.get("limit_hwid", 0)),
        "devices_used": len(devices),
    }


@api_router.delete("/devices/{device_id}")
async def delete_device(device_id: str, user_id: int = Depends(get_current_user_id)):
    if not device_id.isascii() or not device_id.isdecimal() or len(device_id) > 12:
        raise HTTPException(status_code=400, detail="Некорректный идентификатор устройства")
    parsed_id = int(device_id)
    if parsed_id <= 0:
        raise HTTPException(status_code=400, detail="Некорректный идентификатор устройства")
    try:
        from xui_client import delete_xui_hwid_device

        # The helper first checks the authenticated user's panel list and sends
        # a user-scoped delete only for an exact panel ID. Missing/stale IDs are
        # idempotent success, while panel errors remain a service failure.
        removed = await delete_xui_hwid_device(user_id, parsed_id)
        if not removed:
            raise HTTPException(status_code=503, detail="Не удалось удалить устройство. Попробуйте позже")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Device deletion unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Не удалось удалить устройство. Попробуйте позже") from None
    return {"status": "ok"}
