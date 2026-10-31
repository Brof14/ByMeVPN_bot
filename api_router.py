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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

import database
from config import XUI_SUB_PATH, XUI_URL

logger = logging.getLogger(__name__)

api_router = APIRouter()

# ---------------------------------------------------------------------------
# Telegram Session Exchange & JWT Signing
# ---------------------------------------------------------------------------
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET or len(JWT_SECRET.encode("utf-8")) < 32:
    raise RuntimeError("JWT_SECRET must be configured with at least 32 bytes")

JWT_ISSUER = "bymevpn-api"
JWT_AUDIENCE = "bymevpn-mobile"


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
    return await database.confirm_mobile_auth_session(
        state_hash,
        user_id,
        (username or f"user_{user_id}")[:80],
        (full_name or username or f"User {user_id}")[:160],
    )


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
            server_ip = "2.27.120.247" if (raw_host == "bymevpn.duckdns.org" or not raw_host) else raw_host

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
            server_ip = "2.27.120.247" if (raw_host == "bymevpn.duckdns.org" or not raw_host) else raw_host

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

    async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
        try:
            resp = await client.get(local_url, headers=upstream_headers)
            if resp.status_code == 200:
                raw_content = resp.text
        except Exception:
            pass

        if not raw_content:
            try:
                resp = await client.get(sub_url, headers=upstream_headers)
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

    # 1. Check local keys table
    user_keys = await database.get_user_keys(user_id)
    active_keys = [k for k in user_keys if int(k.get("expiry", 0)) > now]
    if active_keys:
        best_key = max(active_keys, key=lambda k: int(k.get("expiry", 0)))
        expiry_ts = int(best_key.get("expiry", 0))
        days_count = int(best_key.get("days", 30))
        device_limit = int(best_key.get("limit_ip", 2))
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
                sub_uuid = client_details.get("uuid") or sub_uuid
                sub_url = client_details.get("subscription_url")
                device_limit = int(client_details.get("limit_hwid", 0))
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
        return None, [], {
            "status": "none",
            "plan_name": "Нет подписки",
            "trial_available": True,
            "max_devices": 5,
            "active_devices": 0,
            "subscription_url": None,
        }

    # Fetch and parse real nodes from 3x-ui
    servers = await fetch_and_parse_real_nodes(sub_url, sub_uuid, request_headers)

    seconds_remaining = max(0, expiry_ts - now) if expiry_ts else 30 * 86400
    expires_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry_ts)) if expiry_ts else None

    sub_dict = {
        "status": "active",
        "plan_name": f"Подписка ByMeVPN ({days_count} дн.)",
        "expires_at": expires_at,
        "seconds_remaining": seconds_remaining,
        "subscription_url": sub_url,
        "max_devices": device_limit,
        "active_devices": 1,
    }

    return sub_url, servers, sub_dict


def _subscription_headers_from_request(request: Request) -> Dict[str, str]:
    """Forward only device metadata required by the subscription HWID gate."""
    names = ("X-HWID", "X-Device-OS", "X-Ver-OS", "X-Device-Model", "User-Agent")
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


class TelegramSessionCheckRequest(BaseModel):
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


class SessionRequest(BaseModel):
    node_code: str
    install_id: Optional[str] = None


# ---------------------------------------------------------------------------
# 1. TELEGRAM 1-TAP AUTH (Zero Link Copying)
# ---------------------------------------------------------------------------

@api_router.post("/auth/telegram/create-session", response_model=CreateTelegramSessionResponse)
async def create_telegram_session():
    token = f"auth_{secrets.token_urlsafe(32)}"
    state_hash = hashlib.sha256(token.removeprefix("auth_").encode("utf-8")).hexdigest()
    await database.create_mobile_auth_session(state_hash, int(time.time()) + 600)

    return CreateTelegramSessionResponse(
        session_token=token,
        bot_url=f"https://t.me/ByMeVPNbot?start={token}",
    )


@api_router.get("/auth/telegram/check-session", response_model=TelegramCheckResponse)
async def check_telegram_session(token: str, request: Request):
    state = token.removeprefix("auth_")
    if not state or len(state) > 256:
        return TelegramCheckResponse(status="expired")
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    session = await database.poll_mobile_auth_session(state_hash)
    if not session or session.get("status") == "expired":
        return TelegramCheckResponse(status="expired")
    if session.get("status") == "pending":
        return TelegramCheckResponse(status="pending")

    user_id = int(session["user_id"])
    username = session.get("username", "")
    full_name = session.get("full_name", "Telegram User")

    jwt_token = create_simple_jwt(user_id, f"{user_id}@telegram.bymevpn")
    sub_url, servers, sub_dict = await get_user_real_subscription(
        user_id, _subscription_headers_from_request(request)
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


@api_router.post("/auth/telegram/check-session", response_model=TelegramCheckResponse)
async def check_telegram_session_body(req: TelegramSessionCheckRequest, request: Request):
    """Poll Telegram login state in the request body to keep it out of URL logs."""
    if not req.token or len(req.token) > 256:
        return TelegramCheckResponse(status="expired")
    return await check_telegram_session(req.token, request)


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
    sub_url, servers, sub_dict = await get_user_real_subscription(
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
    sub_url, servers, sub_dict = await get_user_real_subscription(
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


@api_router.post("/auth/google")
async def login_google(req: GoogleLoginRequest, request: Request):
    # The former implementation decoded claims without checking the signature,
    # audience, verified-email flag or nonce. Keep the endpoint closed until a
    # Google client ID and nonce-bound native flow are configured and tested.
    raise HTTPException(
        status_code=503,
        detail="Вход через Google временно недоступен",
    )


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
    return {
        "user": {
            "id": str(user_id),
            "email": f"{username or user_id}@bymevpn.app",
            "name": username or f"User {user_id}",
            "is_google": False,
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
    sub_url, servers, sub_dict = await get_user_real_subscription(
        user_id, _subscription_headers_from_request(request)
    )
    return sub_dict


@api_router.post("/subscription/trial")
async def activate_trial(req: TrialRequest, request: Request, user_id: int = Depends(get_current_user_id)):
    """Atomically grant one free 3-day trial to a user without a subscription."""
    trial_days = 3
    if await database.has_trial_used(user_id):
        raise HTTPException(status_code=409, detail="Пробный период уже использован")
    current_keys = await database.get_user_keys(user_id)
    now = int(time.time())
    if any(int(key.get("expiry") or 0) > now for key in current_keys):
        raise HTTPException(status_code=409, detail="У вас уже есть действующая подписка")
    try:
        from xui_client import get_xui_client_details
        if await get_xui_client_details(user_id):
            raise HTTPException(status_code=409, detail="Для этого аккаунта уже создана подписка")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Trial eligibility lookup failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Не удалось проверить доступность пробного периода") from None

    if not await database.try_claim_trial(user_id):
        raise HTTPException(status_code=409, detail="Пробный период уже использован")
    try:
        from xui_client import create_xui_user
        res = await create_xui_user(user_id=user_id, days=trial_days, limit_ip=0, limit_hwid=2)
    except Exception as exc:
        logger.error("Trial provisioning failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Не удалось подключить пробный период. Напишите в поддержку") from None
    if not res or not res.get("subscription_url"):
        # The claim stays consumed when panel provisioning has an ambiguous
        # outcome; this prevents retries from extending a partially created key.
        raise HTTPException(status_code=503, detail="Не удалось подключить пробный период. Напишите в поддержку")
    sub_url = res["subscription_url"]
    expiry_ts = int(time.time()) + trial_days * 86400
    try:
        await database.add_key(
            user_id=user_id,
            key=sub_url,
            remark="Пробный период — 3 дня",
            uuid=str(res.get("uuid") or ""),
            days=trial_days,
            limit_ip=0,
            expiry=expiry_ts,
        )
    except Exception as exc:
        logger.error("Trial database save failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Пробный период создан, но его статус не сохранился. Напишите в поддержку") from None

    servers = await fetch_and_parse_real_nodes(
        sub_url, request_headers=_subscription_headers_from_request(request)
    )

    return {
        "status": "trial",
        "plan_name": "Пробный период (3 дня)",
        "expires_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry_ts)),
        "seconds_remaining": trial_days * 86400,
        "subscription_url": sub_url,
        "max_devices": 2,
        "active_devices": 1,
        "servers": servers,
    }


@api_router.get("/plans")
async def get_plans(devices: int = 2):
    from constants import get_price_for_months, VALID_DEVICE_LIMITS
    if devices not in VALID_DEVICE_LIMITS:
        raise HTTPException(status_code=400, detail="Выберите 2, 5 или 10 устройств")

    plan_catalog = [
        ("1m", 1, "1 месяц", 0, False),
        ("3m", 3, "4 месяца (3+1)", 11, True),
        ("6m", 6, "8 месяцев (6+2)", 22, False),
        ("12m", 12, "15 месяцев (12+3)", 34, False),
    ]
    plans = [
        {
            "code": code,
            "name": label,
            "price": f"{get_price_for_months(months, devices)[0]} ₽",
            "price_rub": get_price_for_months(months, devices)[0],
            "days": get_price_for_months(months, devices)[1],
            "devices": devices,
            "discount_percent": discount,
            "is_popular": popular,
        }
        for code, months, label, discount, popular in plan_catalog
    ]
    return {
        "trial": {"code": "trial", "name": "3 дня бесплатно", "price": "0 ₽", "price_rub": 0, "days": 3, "devices": 2},
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
    months = plan_map.get(req.plan_code)
    if months is None:
        raise HTTPException(status_code=400, detail="Неизвестный тариф")
    provider = req.provider or "yookassa"
    if provider not in ("yookassa", "crypto"):
        raise HTTPException(status_code=400, detail="Этот способ оплаты недоступен в приложении")
    if not req.request_id or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", req.request_id):
        raise HTTPException(status_code=400, detail="Создайте новый запрос на оплату")

    from constants import get_price_for_months, VALID_DEVICE_LIMITS
    devices = 2 if req.devices is None else req.devices
    if devices not in VALID_DEVICE_LIMITS:
        raise HTTPException(status_code=400, detail="Выберите 2, 5 или 10 устройств")
    # Client amount/days are informational only; backend catalog owns all terms.
    amount_rub, days = get_price_for_months(months, devices)
    reserved = await database.ensure_mobile_payment_request(
        user_id, req.request_id, req.plan_code, provider, devices,
    )
    if (
        reserved["plan_code"] != req.plan_code
        or reserved["provider"] != provider
        or int(reserved.get("devices", 2)) != devices
    ):
        raise HTTPException(status_code=409, detail="Этот запрос уже использован для другого тарифа")

    if provider == "crypto":
        from payments import create_crypto_payment, find_crypto_invoice_by_payload, get_crypto_invoice

        def _crypto_payload(generation: int) -> str:
            material = f"mobile:{user_id}:{req.request_id}:{req.plan_code}:{devices}:{generation}"
            return "bm1_" + hashlib.sha256(material.encode()).hexdigest()

        async def _bind_crypto_invoice(invoice_id: str, payment_url: str, payload: str) -> None:
            saved = await database.save_mobile_crypto_checkout(
                user_id, req.request_id, req.plan_code, devices, invoice_id,
                payment_url, days, amount_rub, payload,
            )
            if not saved:
                raise HTTPException(
                    status_code=503,
                    detail="Счёт создан, но его безопасное сохранение не подтверждено. Повторите проверку позже",
                )

        async def _rotate_expired_crypto(invoice_id: str, generation: int) -> dict:
            rotated = await database.advance_mobile_payment_request_generation(
                user_id, req.request_id, req.plan_code, provider, invoice_id, generation,
            )
            if rotated:
                await database.delete_crypto_pending(invoice_id)
            latest = await database.ensure_mobile_payment_request(
                user_id, req.request_id, req.plan_code, provider, devices,
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
            user_id, req.request_id, req.plan_code, provider,
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
                    user_id, req.request_id, req.plan_code, provider,
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
                user_id, req.request_id, req.plan_code, provider,
                previous_invoice, reserved["generation"],
            )
            # A simultaneous retry may have rotated the request already.
            reserved = await database.ensure_mobile_payment_request(
                user_id, req.request_id, req.plan_code, provider, devices,
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
            "plan_code": req.plan_code,
            "mobile_request_hash": hashlib.sha256(req.request_id.encode()).hexdigest()[:20],
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
        user_id, req.request_id, req.plan_code, provider,
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
        user_id, _subscription_headers_from_request(request)
    )
    active = sub_dict.get("status") in ("active", "trial", "grace")
    return {"status": "succeeded" if active else "processing", "is_active": active,
            "subscription": sub_dict if active else None}


# 4. SERVERS & VPN SESSIONS (Real Nodes Only)
# ---------------------------------------------------------------------------

@api_router.get("/servers")
async def get_servers(request: Request, user_id: int = Depends(get_current_user_id)):
    sub_url, servers, sub_dict = await get_user_real_subscription(
        user_id, _subscription_headers_from_request(request)
    )
    return {
        "servers": servers,
        "subscription_url": sub_url,
    }


@api_router.post("/vpn/session")
async def create_vpn_session(req: SessionRequest, request: Request, user_id: int = Depends(get_current_user_id)):
    sub_url, servers, sub_dict = await get_user_real_subscription(
        user_id, _subscription_headers_from_request(request)
    )
    if not servers:
        raise HTTPException(
            status_code=403,
            detail="Нет активной подписки или серверов. Оформите подписку или активируйте пробный период.",
        )

    matched = next((s for s in servers if s.get("node_code") == req.node_code), servers[0])
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
    if not hwid or not fingerprint or len(fingerprint) > 64:
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
        from xui_client import delete_xui_hwid_device, get_xui_devices

        # Verify ownership against this authenticated user's panel record before
        # issuing deletion. The XUI endpoint itself is scoped by user_id too.
        current = await get_xui_devices(user_id)
        if not any(int(item.get("id", 0)) == parsed_id for item in current.get("devices", [])):
            raise HTTPException(status_code=404, detail="Устройство не найдено")
        removed = await delete_xui_hwid_device(user_id, parsed_id)
        if not removed:
            raise HTTPException(status_code=503, detail="Не удалось удалить устройство. Попробуйте позже")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Device deletion unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Не удалось удалить устройство. Попробуйте позже") from None
    return {"status": "ok"}
