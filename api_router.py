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
from mobile_refresh_compat import install_mobile_refresh_storage_compat
from google_signin import GoogleIdTokenError, verify_google_id_token
from desktop_vpn_capabilities import windows_vpn_compatible

logger = logging.getLogger(__name__)

# Older production deployments use a narrow bind-mounted database.py shim.
# Fill only missing Telegram session helpers; native DB implementations stay
# authoritative when present.
install_telegram_auth_storage_compat(database)

# Keep the public API as one mount point while separating route groups by
# responsibility. Authentication and response contracts stay unchanged.
api_router = APIRouter()
auth_api = APIRouter(prefix="/auth", tags=["authentication"])
account_api = APIRouter(tags=["account"])
subscription_api = APIRouter(prefix="/subscription", tags=["subscription"])
billing_api = APIRouter(tags=["billing"])
vpn_access_api = APIRouter(tags=["vpn-access"])

# ---------------------------------------------------------------------------
# Telegram Session Exchange & JWT Signing
# ---------------------------------------------------------------------------
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET or len(JWT_SECRET.encode("utf-8")) < 32:
    raise RuntimeError("JWT_SECRET must be configured with at least 32 bytes")

JWT_ISSUER = "bymevpn-api"
JWT_AUDIENCE = "bymevpn-mobile"
MOBILE_TRIAL_BOOTSTRAP_TTL_SECONDS = 10 * 60
TRIAL_CLAIM_RECONCILE_AFTER_SECONDS = 120
_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def _request_correlation_id(request: Request) -> str:
    """Return a safe caller ID or generate one without logging request data."""
    provided = request.headers.get("X-Correlation-ID", "")
    if _SAFE_CORRELATION_ID.fullmatch(provided):
        return provided
    return secrets.token_hex(8)


def _correlated_http_error(
    status_code: int,
    detail: str,
    correlation_id: str,
    error_code: Optional[str] = None,
) -> HTTPException:
    headers = {"X-Correlation-ID": correlation_id}
    if status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if error_code:
        headers["X-Error-Code"] = error_code
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=headers,
    )


async def _servers_user_id(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> int:
    """Authenticate /servers while preserving a safe correlation on refusals."""
    try:
        return await get_current_user_id(authorization)
    except HTTPException as exc:
        correlation_id = _request_correlation_id(request)
        error_code = "authentication_required" if exc.status_code == 401 else "session_verification_unavailable"
        logger.warning(
            "Servers failure stage=authentication code=%s status=%s correlation_id=%s",
            error_code, exc.status_code, correlation_id,
        )
        detail = "Войдите в аккаунт" if exc.status_code == 401 else "Не удалось проверить сессию"
        raise _correlated_http_error(exc.status_code, detail, correlation_id, error_code) from None


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


REFRESH_TOKEN_DAYS = 90


async def issue_session_tokens(user_id: int, email: str = "") -> dict[str, str]:
    """Issue independently expiring access and one-time opaque refresh tokens."""
    issued_at = int(time.time())
    expires_at = issued_at + 30 * 86400
    jti = secrets.token_urlsafe(18)
    access = create_simple_jwt(
        user_id, email, issued_at=issued_at, expires_at=expires_at, jwt_id=jti,
    )
    # Production can use an older bind-mounted database adapter; its missing
    # refresh methods are installed against the same DB connection and lock.
    install_mobile_refresh_storage_compat(database)
    refresh = "rf2_" + secrets.token_urlsafe(40)
    await database.create_mobile_refresh_session(
        hashlib.sha256(refresh.encode("ascii")).hexdigest(),
        user_id, email, jti, expires_at,
        issued_at + REFRESH_TOKEN_DAYS * 86400,
    )
    return {"access": access, "refresh": refresh}


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


async def _is_removed_account(user_id: int) -> bool:
    """Check account tombstones, including deployed legacy auth adapters.

    Older patched runtime modules may have JWT storage methods but not the
    new deletion helper. Create only the additive tombstone table, then make
    the same fail-closed read instead of returning 503 for healthy sessions.
    """
    checker = getattr(database, "is_account_tombstoned", None)
    if callable(checker):
        return bool(await checker(user_id))

    db = await database.get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS account_tombstones (
            user_id INTEGER PRIMARY KEY,
            deleted_at INTEGER NOT NULL,
            source TEXT NOT NULL
        )"""
    )
    await db.commit()
    cursor = await db.execute(
        "SELECT 1 FROM account_tombstones WHERE user_id=? LIMIT 1",
        (int(user_id),),
    )
    return await cursor.fetchone() is not None


async def get_current_user_id(authorization: Optional[str] = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    claims = _verified_token_claims(token)
    if not claims:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        if await database.is_mobile_jwt_revoked(claims["jti"]):
            raise HTTPException(
                status_code=401, detail="Session has been logged out",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if await _is_removed_account(claims["user_id"]):
            raise HTTPException(
                status_code=401, detail="Account has been removed",
                headers={"WWW-Authenticate": "Bearer"},
            )
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

            address = urlparse(line)
            raw_host = address.hostname or ""
            port = address.port or 443
            server_ip = raw_host

            params = parse_qs(query_str)
            pbk = params.get("pbk", [""])[0]
            sid = params.get("sid", [""])[0]
            sni = params.get("sni", [raw_host or "www.cloudflare.com"])[0]
            fp = params.get("fp", ["chrome"])[0]
            # Subscription URI is the source of truth. Never invent Vision for
            # gRPC, WS, TLS or nodes that did not advertise a flow.
            flow = params.get("flow", [""])[0]
            security = params.get("security", ["reality" if pbk else "tls"])[0]
            net_type = params.get("type", ["tcp"])[0]
            transport_path = (params.get("serviceName")
                              or params.get("servicename")
                              or params.get("path") or [""])[0]
            transport_host = params.get("host", [""])[0]
            transport_mode = params.get("mode", [""])[0]

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
                    "encryption": params.get("encryption", ["none"])[0],
                    "flow": flow,
                    "security": security,
                    "server_name": sni,
                    "public_key": pbk,
                    "short_id": sid,
                    "fingerprint": fp,
                    "network": net_type,
                    "transport_path": transport_path,
                    "transport_host": transport_host,
                    "transport_mode": transport_mode,
                    "alpn": params.get("alpn", [""])[0],
                },
            }
        elif line.startswith("hysteria2://") or line.startswith("hy2://"):
            clean = line.split("://", 1)[1]
            hash_parts = clean.split("#", 1)
            main_part = hash_parts[0]
            remark = unquote(hash_parts[1]) if len(hash_parts) > 1 else f"ByMeVPN Fast {index + 1}"

            at_parts = main_part.split("@", 1)
            password = unquote(at_parts[0])
            host_and_rest = at_parts[1]

            q_parts = host_and_rest.split("?", 1)
            address = urlparse(line)
            raw_host = address.hostname or ""
            port = address.port or 443
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
                    "encryption": params.get("obfs", [""])[0],
                    "flow": params.get("obfs-password", [""])[0],
                    "security": "tls",
                    "server_name": sni,
                    "public_key": "",
                    "short_id": "",
                    "fingerprint": params.get("fp", ["firefox"])[0],
                    "network": "udp",
                    "alpn": params.get("alpn", [""])[0],
                },
            }
    except Exception:
        # Subscription URIs embed credentials; never log even a truncated URI
        # or exception text containing untrusted userinfo.
        logger.warning("Rejected malformed subscription node at index %s", index)
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
            # A failed trial may have created a panel client without finishing
            # the atomic claim. Offer a retry instead of treating that partial
            # client as a paid subscription. POST performs the actual repair.
            pending = await database.get_trial_claim_reservation(user_id)
            if pending and client_details and not await database.has_trial_used(user_id):
                from constants import TRIAL_DAYS
                created = int(pending.get("created_at") or now)
                panel_expiry = int(client_details.get("expiry_time") or 0) // 1000
                if (panel_expiry > now and client_details.get("enable") is not False
                        and abs(panel_expiry - (created + TRIAL_DAYS * 86400)) <= 120):
                    eligible = now - created >= TRIAL_CLAIM_RECONCILE_AFTER_SECONDS
                    if eligible and _entitlement_requested(request_headers or {}):
                        device_binding(user_id, str((request_headers or {}).get("X-HWID") or "").strip())
                        load_private_key()
                    return None, [], {
                        "status": "none", "plan_code": None, "plan_name": "Нет подписки",
                        "trial_available": eligible,
                        "trial_unavailable_reason": None if eligible else "trial_activation_pending",
                        "max_devices": device_limit, "active_devices": 0,
                        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "subscription_url": None,
                    }
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
                get_pending_claim = getattr(database, "get_trial_claim_reservation", None)
                pending_claim = (
                    await get_pending_claim(user_id)
                    if get_pending_claim is not None
                    else None
                )
                reservation_age = (
                    max(0, now - int(pending_claim.get("created_at") or now))
                    if isinstance(pending_claim, dict)
                    else None
                )
                if (pending_claim is not None
                        and reservation_age < TRIAL_CLAIM_RECONCILE_AFTER_SECONDS):
                    trial_reason = "trial_activation_pending"
                else:
                    from xui_client import get_xui_client_details
                    # The POST activation route reconciles stale reservations
                    # before granting access. Keep GET read-only, but do not
                    # leave an old reservation blocking the trial button
                    # forever when an authoritative panel lookup confirms no
                    # active client. An uncertain lookup still fails closed.
                    panel_client = await get_xui_client_details(
                        user_id, strict=pending_claim is not None,
                    )
                    panel_expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
                    if panel_client and (panel_expiry <= 0 or panel_expiry > now):
                        trial_reason = "existing_panel_subscription"
                    elif not _entitlement_requested(request_headers or {}):
                        trial_available = True
                        trial_reason = None
                    else:
                        # Panel HWID evidence appears only after first tunnel
                        # connection. Eligibility checks the signed client
                        # binding here; the bootstrap path separately verifies
                        # the live panel client, limits and available slot.
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
    # A subscription link alone must not manufacture a month of paid access.
    if not expiry_ts:
        return None, [], {
            "status": "unknown",
            "plan_code": None,
            "plan_name": "Срок подписки не подтверждён",
            "trial_available": False,
            "trial_unavailable_reason": "subscription_verification_required",
            "access_ready": False,
            "config_status": "unverified",
            "max_devices": device_limit,
            "active_devices": 0,
            "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "subscription_url": None,
        }

    servers = await fetch_and_parse_real_nodes(sub_url, sub_uuid, request_headers)

    seconds_remaining = max(0, expiry_ts - now)
    expires_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry_ts))

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
        # Entitlement and ability to obtain a usable VPN configuration differ.
        "config_status": "ready" if servers else "unavailable",
        "access_ready": bool(servers),
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
    # Callers may reuse an in-memory snapshot in a single request pipeline;
    # never allow bootstrap markers from an earlier signing pass to leak into
    # the confirmed-device response.
    subscription.pop("entitlement_bootstrap", None)
    subscription.pop("entitlement_expires_at", None)
    if subscription.get("status") not in ("active", "trial", "grace"):
        subscription["entitlement"] = None
        return subscription

    hwid = str(headers.get("X-HWID") or "").strip()
    try:
        device_state = await _require_registered_entitlement_device(user_id, headers)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        if subscription.get("status") == "trial":
            trial_bootstrap_expiry = await _validated_trial_bootstrap_expiry(user_id, headers)
            if trial_bootstrap_expiry is not None:
                remaining_bootstrap = trial_bootstrap_expiry - int(time.time())
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
                subscription["entitlement_expires_at"] = trial_bootstrap_expiry
                return subscription
        bootstrap_expiry = await _validated_active_device_bootstrap_expiry(
            user_id, headers, subscription, create_if_missing=True,
        )
        if bootstrap_expiry is not None:
            remaining_bootstrap = bootstrap_expiry - int(time.time())
            if remaining_bootstrap <= 0:
                raise HTTPException(status_code=403, detail="Первичное подключение истекло") from None
            subscription["max_devices"] = int(subscription.get("max_devices") or 0)
            subscription["active_devices"] = int(subscription.get("active_devices") or 0)
            bootstrap_status = dict(subscription)
            bootstrap_status["seconds_remaining"] = min(
                int(subscription.get("seconds_remaining") or 0), remaining_bootstrap,
            )
            subscription["entitlement"] = create_entitlement(user_id, hwid, bootstrap_status)
            subscription["entitlement_bootstrap"] = True
            subscription["entitlement_expires_at"] = bootstrap_expiry
            return subscription
        trial_bootstrap_expiry = await _validated_trial_bootstrap_expiry(user_id, headers)
        if trial_bootstrap_expiry is not None:
            remaining_bootstrap = trial_bootstrap_expiry - int(time.time())
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
            subscription["entitlement_expires_at"] = trial_bootstrap_expiry
            return subscription
        # The panel may have learned the HWID between reads. Retry once so the
        # request upgrades directly to an ordinary entitlement on that race.
        device_state = await _require_registered_entitlement_device(user_id, headers)
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
        await database.delete_mobile_device_bootstrap(user_id, device_binding(user_id, hwid))
    except Exception as exc:
        logger.warning("Confirmed device bootstrap cleanup failed (%s)", type(exc).__name__)
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
        # 3x-ui 3.11 returns a 12-hex SHA-256 prefix. Longer supported
        # fingerprints remain valid; unknown shorter formats do not.
        and (len(fingerprint) == 12 or 16 <= len(fingerprint) <= 64)
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
                or not isinstance(device_state.get("devices"), list)
                or len(device_state.get("devices") or []) >= 2):
            return None
        # If the panel has already learned this device, stop using bootstrap.
        if _panel_state_contains_hwid(user_id, hwid, device_state):
            await database.delete_mobile_trial_bootstrap(user_id)
            return None
        return int(expiry)
    except Exception as exc:
        logger.warning("Trial bootstrap panel validation failed (%s)", type(exc).__name__)
        return None


async def _validated_active_device_bootstrap_expiry(
    user_id: int,
    headers: Dict[str, str],
    subscription: Dict[str, Any],
    *,
    create_if_missing: bool,
) -> Optional[int]:
    """Issue/resume one short signed bootstrap only for a verified active panel slot."""
    hwid = str(headers.get("X-HWID") or "").strip()
    try:
        device_hash = device_binding(user_id, hwid)
    except EntitlementSigningError:
        return None
    if subscription.get("status") not in ("active", "trial", "grace"):
        return None
    if subscription.get("status") == "trial" and not await database.has_trial_used(user_id):
        return None
    try:
        subscription_seconds = int(subscription.get("seconds_remaining") or 0)
        subscription_limit = int(subscription.get("max_devices") or 0)
    except (TypeError, ValueError):
        return None
    if subscription_seconds <= 0:
        return None

    try:
        from xui_client import get_xui_client_details, get_xui_devices, has_xui_client
        if not await has_xui_client(user_id):
            return None
        panel_client, device_state = await asyncio.gather(
            get_xui_client_details(user_id, strict=True),
            get_xui_devices(user_id),
        )
        now = int(time.time())
        panel_expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
        panel_limit = int((device_state or {}).get("limit_hwid") or 0)
        ip_limit = int((device_state or {}).get("limit_ip") or 0)
        devices = (device_state or {}).get("devices")
        if (not isinstance(panel_client, dict) or panel_client.get("enable") is False
                or panel_expiry <= now or panel_limit not in (2, 5, 10)
                or panel_limit != subscription_limit or ip_limit != 0
                or not isinstance(devices, list)):
            return None
        if _panel_state_contains_hwid(user_id, hwid, device_state):
            await database.delete_mobile_device_bootstrap(user_id, device_hash)
            return None
        if len(devices) >= panel_limit:
            return None
        # The panel expiry is a stable subscription generation identifier. The
        # signed JWT below is still capped by the authoritative remaining time
        # in the subscription snapshot, so a retry cannot move this anchor.
        effective_subscription_expiry = panel_expiry
        if effective_subscription_expiry <= now:
            return None
        subscription["max_devices"] = panel_limit
        subscription["active_devices"] = len(devices)

        existing = await database.get_mobile_device_bootstrap(
            user_id, device_hash, effective_subscription_expiry,
        )
        if existing is not None:
            return existing
        if not create_if_missing:
            return None
        return await database.reserve_mobile_device_bootstrap(
            user_id=user_id,
            device_hash=device_hash,
            subscription_expiry=effective_subscription_expiry,
            expires_at=now + MOBILE_TRIAL_BOOTSTRAP_TTL_SECONDS,
            active_devices=len(devices),
            device_limit=panel_limit,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Device bootstrap panel validation failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Не удалось проверить устройства подписки",
        ) from None


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
        _, _, subscription = await get_user_real_subscription(user_id, headers)
        bootstrap_expiry = await _validated_active_device_bootstrap_expiry(
            user_id, headers, subscription, create_if_missing=True,
        )
        if bootstrap_expiry is None and await _validated_trial_bootstrap_expiry(user_id, headers) is None:
            # Retry once because panel state may change between reads.
            await _require_registered_entitlement_device(user_id, headers)
    return headers


def _bootstrap_entitlement_fields(subscription: Dict[str, Any]) -> Dict[str, Any]:
    """Return only the signed access metadata required by the opted-in client."""
    if not subscription.get("entitlement"):
        return {}
    return {
        key: subscription[key]
        for key in ("entitlement", "entitlement_bootstrap", "entitlement_expires_at")
        if key in subscription
    }


async def _auth_subscription_snapshot(user_id: int, headers: Dict[str, str]):
    """Keep login independent from 3x-ui subscription availability."""
    _entitlement_requested(headers)
    try:
        sub_url, servers, sub_dict = await get_user_real_subscription(user_id, headers)
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

    try:
        sub_dict = await _attach_signed_entitlement(user_id, headers, sub_dict)
        return sub_url, servers, sub_dict
    except HTTPException as exc:
        if exc.status_code != 403:
            logger.warning("Subscription snapshot unavailable during login (HTTP %s)", exc.status_code)
            return None, [], {
                "status": "unknown", "plan_name": "Проверяем подписку",
                "trial_available": False, "trial_unavailable_reason": "subscription_check_unavailable",
                "max_devices": 0, "active_devices": 0,
            }
        # A valid login must not be rewritten as "no subscription" merely
        # because this installation has not yet been confirmed by 3x-ui. Keep
        # only non-secret subscription facts; never return its URL or configs
        # until the panel confirms this HWID and a signed entitlement is issued.
        logger.info("Subscription snapshot stage=device_confirmation code=required")
        return None, [], _device_confirmation_status_snapshot(sub_dict)
    except Exception as exc:
        logger.warning("Subscription lookup unavailable during login (%s)", type(exc).__name__)
        return None, [], {
            "status": "unknown", "plan_name": "Проверяем подписку",
            "trial_available": False, "trial_unavailable_reason": "subscription_check_unavailable",
            "max_devices": 0, "active_devices": 0,
        }


def _device_confirmation_status_snapshot(subscription: Dict[str, Any]) -> Dict[str, Any]:
    """Return truthful status metadata without credentials for an unconfirmed device."""
    visible_fields = (
        "status", "plan_code", "plan_name", "expires_at", "seconds_remaining",
        "server_time", "max_devices", "active_devices", "trial_available",
    )
    safe = {key: subscription[key] for key in visible_fields if key in subscription}
    safe["entitlement"] = None
    safe["device_confirmation_required"] = True
    safe["subscription_url"] = None
    return safe


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


class GoogleDesktopCodeRequest(BaseModel):
    code: str
    code_verifier: str
    redirect_uri: str
    nonce: str
    install_id: Optional[str] = None


class RefreshRequest(BaseModel):
    refresh: str


class TrialRequest(BaseModel):
    install_id: Optional[str] = None
    device_model: Optional[str] = None


async def _require_trial_user_id(
    request: Request, authorization: Optional[str] = Header(None),
) -> int:
    """Authenticate trial requests while attaching a safe diagnostic ID."""
    correlation_id = _request_correlation_id(request)
    try:
        return await get_current_user_id(authorization)
    except HTTPException as exc:
        code = "unauthorized" if exc.status_code == 401 else "session_check_unavailable"
        logger.warning(
            "Trial failure stage=authorization code=%s correlation_id=%s",
            code, correlation_id,
        )
        detail = exc.detail if isinstance(exc.detail, str) else "Не удалось проверить сессию"
        raise _correlated_http_error(exc.status_code, detail, correlation_id) from None


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

@auth_api.post("/telegram/create-session", response_model=CreateTelegramSessionResponse)
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


@auth_api.get("/telegram/check-session", response_model=TelegramCheckResponse)
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
        session_tokens = await issue_session_tokens(user_id, f"{user_id}@telegram.bymevpn")
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
        access_token=session_tokens["access"],
        refresh_token=session_tokens["refresh"],
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


@auth_api.post("/telegram/cancel-session")
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

@auth_api.post("/register")
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
    # Do not derive the internal identity from a 32-bit email hash.
    # Allocate and persist an unpredictable ID atomically in the same DB.
    try:
        user_id = await database.create_email_account(
            email, pw_hash, salt, req.name or email.split("@")[0],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session_tokens = await issue_session_tokens(user_id, email)
    sub_url, servers, sub_dict = await _auth_subscription_snapshot(
        user_id, _subscription_headers_from_request(request)
    )

    return {
        "access": session_tokens["access"],
        "refresh": session_tokens["refresh"],
        "user": {
            "id": str(user_id),
            "email": email,
            "name": req.name or email.split("@")[0],
            "is_google": False,
        },
        "subscription": sub_dict,
        "servers": servers,
    }


@auth_api.post("/login")
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

    session_tokens = await issue_session_tokens(user_id, email)
    sub_url, servers, sub_dict = await _auth_subscription_snapshot(
        user_id, _subscription_headers_from_request(request)
    )

    return {
        "access": session_tokens["access"],
        "refresh": session_tokens["refresh"],
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


def _configured_google_audiences() -> tuple[str, ...]:
    """Explicit Google clients only; never trust an audience supplied by a user."""
    web = os.getenv("GOOGLE_WEB_CLIENT_ID", "").strip()
    desktop = os.getenv("GOOGLE_DESKTOP_CLIENT_ID", "").strip()
    valid_id = r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com"
    if desktop != web and re.fullmatch(valid_id, desktop):
        return (web, desktop)
    return (web,)


@auth_api.get("/google/desktop-client")
async def get_google_desktop_client():
    """Public OAuth client ID only: desktop PKCE never ships a client secret."""
    if not _google_signin_configured():
        raise HTTPException(status_code=503, detail="Вход через Google временно недоступен")
    client_id = os.getenv("GOOGLE_DESKTOP_CLIENT_ID", "").strip()
    if (not client_id.endswith(".apps.googleusercontent.com")
            or not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id)):
        raise HTTPException(status_code=503, detail="Google OAuth для Windows ещё не настроен")
    return {
        "client_id": client_id,
        "server_code_exchange": bool(os.getenv("GOOGLE_DESKTOP_CLIENT_SECRET", "").strip()),
    }


@auth_api.post("/google/nonce")
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


@auth_api.post("/google/desktop-exchange")
async def exchange_google_desktop_code(req: GoogleDesktopCodeRequest, request: Request):
    """Exchange PKCE authorization code server-side without distributing the
    desktop OAuth client secret in the Windows executable. The normal
    Google ID-token and single-use nonce checks still run in login_google.
    """
    if not _google_signin_configured():
        raise HTTPException(status_code=503, detail="Вход через Google временно недоступен")
    client_id = os.getenv("GOOGLE_DESKTOP_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_DESKTOP_CLIENT_SECRET", "").strip()
    if not client_secret or not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id):
        raise HTTPException(status_code=503, detail="Обмен Google Desktop OAuth ещё не настроен")

    callback = urlparse(req.redirect_uri)
    # Only native loopback callbacks created by our Windows app are valid.
    try:
        valid_port = callback.port is not None and 1 <= callback.port <= 65535
    except ValueError:
        valid_port = False
    if (callback.scheme != "http" or callback.hostname != "127.0.0.1"
            or not valid_port or callback.path != "/oauth2/callback"
            or callback.username or callback.password or callback.query or callback.fragment
            or callback.params or len(req.redirect_uri) > 180):
        raise HTTPException(status_code=400, detail="Некорректный адрес возврата Google")
    if (not (12 <= len(req.code) <= 2048)
            or any(c.isspace() for c in req.code)
            or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", req.code_verifier)
            or not (24 <= len(req.nonce) <= 256)
            or (req.install_id is not None and len(req.install_id) > 128)):
        raise HTTPException(status_code=400, detail="Некорректный запрос Google OAuth")

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": req.code,
                    "code_verifier": req.code_verifier,
                    "redirect_uri": req.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Google OAuth временно недоступен") from None

    if response.status_code != 200:
        # Provider responses and credentials are never exposed or logged.
        try:
            provider_code = response.json().get("error", "")
        except ValueError:
            provider_code = ""
        allowed_codes = {
            "invalid_client", "invalid_grant", "invalid_request",
            "redirect_uri_mismatch", "unauthorized_client", "access_denied",
        }
        safe_code = provider_code if provider_code in allowed_codes else "provider_error"
        logger.warning("Desktop Google code exchange rejected code=%s", safe_code)
        raise HTTPException(status_code=401, detail=f"Google OAuth: {safe_code}")

    try:
        token_data = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Некорректный ответ Google OAuth") from None
    token = token_data.get("id_token")
    if not isinstance(token, str) or len(token) > 16_384 or token.count(".") != 2:
        raise HTTPException(status_code=502, detail="Google не вернул ID-токен")
    # This invokes exactly the same signature/audience/nonce/account verifier
    # used by Android and the original Windows token-based endpoint.
    return await login_google(
        GoogleLoginRequest(id_token=token, nonce=req.nonce, install_id=req.install_id),
        request,
    )


@auth_api.post("/google")
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

    expected_audiences = _configured_google_audiences()
    try:
        claims = await asyncio.to_thread(verify_google_id_token, req.id_token, expected_audiences)
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
    session_tokens = await issue_session_tokens(user_id, identity["email"])
    _, servers, sub_dict = await _auth_subscription_snapshot(
        user_id, _subscription_headers_from_request(request)
    )
    return {
        "access": session_tokens["access"],
        "refresh": session_tokens["refresh"],
        "user": {
            "id": str(user_id),
            "email": identity["email"],
            "name": identity["name"],
            "is_google": True,
        },
        "subscription": sub_dict,
        "servers": servers,
    }


@auth_api.post("/refresh")
async def refresh_token(req: RefreshRequest):
    install_mobile_refresh_storage_compat(database)
    # V2 refresh credentials are opaque, single-use and independent of JWT expiry.
    if req.refresh.startswith("rf2_") and len(req.refresh) <= 128:
        new_refresh = "rf2_" + secrets.token_urlsafe(40)
        new_jti = secrets.token_urlsafe(18)
        now = int(time.time())
        try:
            session = await database.rotate_mobile_refresh_session(
                hashlib.sha256(req.refresh.encode("ascii")).hexdigest(),
                hashlib.sha256(new_refresh.encode("ascii")).hexdigest(),
                new_jti, now + 30 * 86400, now + REFRESH_TOKEN_DAYS * 86400,
            )
            if not session:
                raise HTTPException(status_code=401, detail="Сессия истекла")
            if await _is_removed_account(session["user_id"]):
                raise HTTPException(status_code=401, detail="Аккаунт удалён")
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Refresh token rotation unavailable (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Сессия временно недоступна") from None
        return {
            "access": create_simple_jwt(
                session["user_id"], session["email"],
                issued_at=now, expires_at=now + 30 * 86400, jwt_id=new_jti,
            ),
            "refresh": new_refresh,
        }

    # Existing app releases may still hold ref_<access JWT>. Upgrade those
    # sessions to independently refreshable credentials until their JWT expires.
    if not req.refresh.startswith("ref_"):
        raise HTTPException(status_code=401, detail="Сессия истекла")
    claims = _verified_token_claims(req.refresh.removeprefix("ref_"))
    if not claims:
        raise HTTPException(status_code=401, detail="Сессия истекла")
    try:
        if await database.is_mobile_jwt_revoked(claims["jti"]):
            raise HTTPException(status_code=401, detail="Сессия завершена")
        if await _is_removed_account(claims["user_id"]):
            raise HTTPException(status_code=401, detail="Аккаунт удалён")
        return await issue_session_tokens(claims["user_id"], claims["email"])
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Legacy refresh upgrade unavailable (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Сессия временно недоступна") from None


@auth_api.post("/logout")
async def logout(
    authorization: Optional[str] = Header(None),
    user_id: int = Depends(get_current_user_id),
):
    token = (authorization or "").removeprefix("Bearer ").strip()
    claims = _verified_token_claims(token)
    if not claims or claims["user_id"] != user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")
    try:
        install_mobile_refresh_storage_compat(database)
        await database.revoke_mobile_refresh_for_access(claims["jti"])
        await database.revoke_mobile_jwt(claims["jti"], claims["expires_at"])
    except Exception as exc:
        logger.error("JWT revocation failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Не удалось завершить сессию. Попробуйте позже") from None
    return {"status": "ok"}


@account_api.get("/me")
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


@account_api.delete("/me")
async def delete_me(
    user_id: int = Depends(get_current_user_id),
    confirmation: Optional[str] = Header(None, alias="X-Confirm-Account-Deletion"),
):
    """Permanently revoke confirmed VPN access and erase personal account data.

    The caller must explicitly confirm loss of paid access. A failed or
    uncertain panel operation leaves SQLite keys intact and stops recurring
    billing; clients can retry safely after the panel is reconciled.
    """
    if confirmation != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="Подтвердите удаление заголовком X-Confirm-Account-Deletion: DELETE. "
                   "Оплаченный срок не сохраняется.",
        )

    from config import ADMIN_IDS
    if user_id in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Удаление администратора через приложение недоступно")

    token = secrets.token_urlsafe(24)
    try:
        acquired = await database.try_acquire_delivery_lease(user_id, token)
    except Exception as exc:
        logger.error("Account deletion lease unavailable for %d (%s)", user_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Удаление временно недоступно") from None
    if not acquired:
        raise HTTPException(
            status_code=409, detail="Другая операция с подпиской ещё выполняется. Повторите запрос",
        )

    try:
        try:
            await database.suspend_all_user_autorenew(user_id)
        except Exception as exc:
            logger.error("Account billing cancellation failed for %d (%s)", user_id, type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось подтвердить отключение автосписаний") from None

        try:
            if await database.has_recent_unsettled_checkout(user_id):
                raise HTTPException(
                    status_code=409,
                    detail="Есть недавно созданный неопределённый платёж. "
                           "Автосписания отключены. Дождитесь его проверки и повторите удаление",
                )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Account deletion checkout check failed for %d (%s)", user_id, type(exc).__name__)
            raise HTTPException(status_code=503, detail="Не удалось проверить незавершённые платежи") from None

        from xui_client import delete_xui_user
        try:
            confirmed_absent = await delete_xui_user(user_id)
        except Exception as exc:
            logger.error("Account deletion panel failure for %d (%s)", user_id, type(exc).__name__)
            confirmed_absent = False
        if not confirmed_absent:
            raise HTTPException(
                status_code=503,
                detail="Удаление VPN на всех серверах не подтверждено. "
                       "Автосписания отключены; доступ и данные оставлены для сверки. "
                       "Не оплачивайте повторно",
            )
        try:
            owned = await database.renew_delivery_lease(user_id, token)
        except Exception:
            owned = False
        if not owned:
            raise HTTPException(
                status_code=503,
                detail="VPN удалён, но подтверждение блокировки БД потеряно. "
                       "Автосписания отключены; требуется сверка",
            )

        try:
            await database.delete_user_and_keys(user_id, source="self_service")
        except Exception as exc:
            logger.exception("Account erasure incomplete after panel deletion for user %d", user_id)
            raise HTTPException(
                status_code=503,
                detail="VPN удалён, но очистка данных не подтверждена. "
                       "Автосписания отключены; обратитесь в поддержку",
            ) from None
        return {
            "status": "deleted",
            "access_revoked": True,
            "recurring_charges_disabled": True,
            "retained_records": "Финансовые транзакции сохраняются для расчётов и спорных платежей",
        }
    finally:
        try:
            await database.release_delivery_lease(user_id, token)
        except Exception:
            logger.exception("Account deletion lease release failed for user %d", user_id)


# ---------------------------------------------------------------------------
# 3. SUBSCRIPTION & TRIAL (Calls 3x-ui to Create Real Clients)
# ---------------------------------------------------------------------------

@subscription_api.get("/status")
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
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        # Preserve the account's real subscription state while withholding the
        # URL, node credentials, and entitlement until panel HWID confirmation.
        return _device_confirmation_status_snapshot(sub_dict)
    return sub_dict


@subscription_api.post("/trial")
async def activate_trial(
    req: TrialRequest, request: Request, response: Response = None,
    user_id: int = Depends(_require_trial_user_id),
):
    """Atomically grant one free 3-day trial to a user without a subscription."""
    from constants import DEFAULT_DEVICE_LIMIT, TRIAL_DAYS

    correlation_id = _request_correlation_id(request)
    if response is not None:
        response.headers["X-Correlation-ID"] = correlation_id
    trial_days = TRIAL_DAYS
    trial_device_limit = DEFAULT_DEVICE_LIMIT
    headers = _subscription_headers_from_request(request)
    try:
        wants_entitlement = _entitlement_requested(headers)
    except HTTPException as exc:
        logger.warning(
            "Trial failure stage=client_contract code=unsupported_entitlement_version correlation_id=%s",
            correlation_id,
        )
        raise _correlated_http_error(
            exc.status_code, "Версия проверки подписки не поддерживается", correlation_id,
        ) from None
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
            logger.warning(
                "Trial failure stage=device_binding code=invalid_device_identifier correlation_id=%s",
                correlation_id,
            )
            raise _correlated_http_error(
                422, "Не удалось подтвердить это устройство", correlation_id,
            ) from None
        if req.install_id and not hmac.compare_digest(req.install_id.strip(), hwid):
            logger.warning(
                "Trial failure stage=device_binding code=install_identifier_mismatch correlation_id=%s",
                correlation_id,
            )
            raise _correlated_http_error(
                422, "Данные устройства не совпадают", correlation_id,
            )
    try:
        if await database.has_trial_used(user_id):
            logger.info(
                "Trial failure stage=eligibility code=trial_already_used correlation_id=%s",
                correlation_id,
            )
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
        logger.info(
            "Trial failure stage=eligibility code=active_subscription_exists correlation_id=%s",
            correlation_id,
        )
        raise _correlated_http_error(
            409, "У вас уже есть действующая подписка", correlation_id,
        )
    try:
        pending_claim = await database.get_trial_claim_reservation(user_id)
    except Exception as exc:
        logger.warning(
            "Trial failure stage=trial_reservation_lookup code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось проверить доступность пробного периода", correlation_id,
        ) from None
    if pending_claim and now - int(pending_claim.get("created_at") or now) < TRIAL_CLAIM_RECONCILE_AFTER_SECONDS:
        logger.info(
            "Trial failure stage=trial_reservation code=activation_in_progress correlation_id=%s",
            correlation_id,
        )
        raise _correlated_http_error(
            409, "Активация пробного периода уже выполняется", correlation_id,
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
    recovered_result = None
    if pending_claim:
        # A stale reservation is reconciled only with an authoritative panel
        # read. Active, correctly limited access completes the shared claim;
        # a confirmed missing/expired client releases it for a safe retry.
        try:
            pending_expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
            if panel_client and pending_expiry > now and panel_client.get("enable") is not False:
                # Only recover the client created for this reservation. Never
                # extend its expiry or adopt an unrelated active subscription.
                expected_expiry = int(pending_claim["created_at"]) + trial_days * 86400
                if abs(pending_expiry - expected_expiry) > 120:
                    raise RuntimeError("Pending trial expiry does not match reservation")
                from xui_client import get_xui_devices, set_xui_client_limits
                pending_devices = await get_xui_devices(user_id)
                if (not isinstance(pending_devices, dict)
                        or int(pending_devices.get("limit_ip") or 0) != 0
                        or not isinstance(pending_devices.get("devices"), list)):
                    raise RuntimeError("Pending trial panel state is not confirmed")
                if int(pending_devices.get("limit_hwid") or 0) != trial_device_limit:
                    if not await set_xui_client_limits(user_id, limit_ip=0, limit_hwid=trial_device_limit):
                        raise RuntimeError("Pending trial device limit could not be repaired")
                if not panel_client.get("subscription_url") or not panel_client.get("uuid"):
                    raise RuntimeError("Pending trial client details are incomplete")
                recovered_result = panel_client
            else:
                await database.release_trial_claim(user_id, pending_claim["claim_id"])
                panel_client = None
                pending_claim = None
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "Trial failure stage=trial_reconciliation code=%s correlation_id=%s",
                type(exc).__name__, correlation_id,
            )
            raise _correlated_http_error(
                503, "Не удалось подтвердить предыдущую попытку. Попробуйте позже", correlation_id,
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
    if not recovered_result and panel_client and (panel_expiry <= 0 or panel_expiry > now):
        logger.info(
            "Trial failure stage=eligibility code=panel_subscription_exists correlation_id=%s",
            correlation_id,
        )
        raise _correlated_http_error(
            409, "Для этого аккаунта уже создана подписка", correlation_id,
        )

    if recovered_result:
        claim_id = pending_claim["claim_id"]
    else:
        try:
            claim_id = await database.reserve_trial_claim(user_id)
        except Exception as exc:
            logger.error(
                "Trial failure stage=trial_reservation code=%s correlation_id=%s",
                type(exc).__name__, correlation_id,
            )
            raise _correlated_http_error(
                503, "Не удалось активировать пробный период", correlation_id,
            ) from None
        if not claim_id:
            logger.info(
                "Trial failure stage=trial_reservation code=already_claimed_or_pending correlation_id=%s",
                correlation_id,
            )
            raise _correlated_http_error(
                409, "Пробный период уже использован", correlation_id,
            )

    async def reconcile_trial_reservation_after_failure() -> None:
        """Commit or release only after a definitive read of the panel state."""
        try:
            from xui_client import get_xui_client_details, get_xui_devices
            panel_client = await get_xui_client_details(user_id, strict=True)
            panel_expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
        except Exception as exc:
            logger.warning(
                "Trial failure stage=reservation_readback code=%s correlation_id=%s",
                type(exc).__name__, correlation_id,
            )
            return
        if not panel_client or panel_expiry <= int(time.time()) or panel_client.get("enable") is False:
            try:
                await database.release_trial_claim(user_id, claim_id)
            except Exception as exc:
                logger.warning(
                    "Trial failure stage=reservation_release code=%s correlation_id=%s",
                    type(exc).__name__, correlation_id,
                )
            return
        try:
            state = await get_xui_devices(user_id)
            if (isinstance(state, dict) and int(state.get("limit_hwid") or 0) == trial_device_limit
                    and int(state.get("limit_ip") or 0) == 0
                    and isinstance(state.get("devices"), list)):
                await database.complete_trial_claim(user_id, claim_id)
        except Exception as exc:
            logger.warning(
                "Trial failure stage=reservation_confirmation code=%s correlation_id=%s",
                type(exc).__name__, correlation_id,
            )

    provision_started_at = int(pending_claim["created_at"]) if recovered_result else time.time()
    if recovered_result:
        res = recovered_result
    else:
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
            await reconcile_trial_reservation_after_failure()
            raise _correlated_http_error(
                503, "Не удалось подключить пробный период. Напишите в поддержку", correlation_id,
            ) from None
        if not res or not res.get("subscription_url"):
            await reconcile_trial_reservation_after_failure()
            raise _correlated_http_error(
                503, "Не удалось подключить пробный период. Напишите в поддержку", correlation_id,
            )

    # Do not persist or report a trial until the panel confirms the actual
    # HWID/IP limits. The legacy DB column is named `limit_ip`, but the bot
    # stores the device allowance there; the panel itself must keep IPs open.
    try:
        from xui_client import get_xui_client_details, get_xui_devices
        panel_client = await get_xui_client_details(user_id, strict=True)
        panel_devices = await get_xui_devices(user_id)
        confirmed_expiry = int((panel_client or {}).get("expiry_time") or 0) // 1000
        expected_expiry = int(provision_started_at) + trial_days * 86400
        if (
            not isinstance(panel_client, dict)
            or panel_client.get("enable") is False
            or confirmed_expiry <= int(time.time())
            or abs(confirmed_expiry - expected_expiry) > (120 if recovered_result else 5)
            or not isinstance(panel_devices, dict)
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

    try:
        if not await database.complete_trial_claim(user_id, claim_id):
            raise RuntimeError("Trial reservation is no longer owned by this request")
    except Exception as exc:
        logger.error(
            "Trial failure stage=trial_commit code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Панель подтвердила пробный период, но не удалось сохранить право. Напишите в поддержку",
            correlation_id,
        ) from None

    sub_url = res["subscription_url"]
    expiry_ts = confirmed_expiry
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
        "seconds_remaining": max(0, expiry_ts - int(time.time())),
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


@billing_api.get("/plans")
async def get_plans(devices: int = 2):
    """Server-owned tariff catalog with legacy and desktop plan identifiers."""
    from constants import get_price_for_months, VALID_DEVICE_LIMITS, TARIFF_DAYS
    if devices not in VALID_DEVICE_LIMITS:
        raise HTTPException(status_code=400, detail="Выберите 2, 5 или 10 устройств")

    plan_catalog = [
        ("1m", "month", 1, "1 месяц", False),
        ("3m", "quarter", 3, "4 месяца (3+1)", True),
        ("6m", "half_year", 6, "8 месяцев (6+2)", False),
        ("12m", "year", 12, "15 месяцев (12+3)", False),
    ]
    plans = []
    base_month_price = get_price_for_months(1, devices)[0]
    for code, desktop_code, months, label, popular in plan_catalog:
        options = [
            {"devices": limit, "price_rub": get_price_for_months(months, limit)[0],
             "days": get_price_for_months(months, limit)[1]}
            for limit in VALID_DEVICE_LIMITS
        ]
        selected = next(option for option in options if option["devices"] == devices)
        included_months = TARIFF_DAYS[months] // 30
        discount = round(100 * (1 - selected["price_rub"] / (base_month_price * included_months)))
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
@billing_api.post("/payment/create")
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


@billing_api.get("/payment/check")
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


# 4. SERVERS & VPN SESSIONS (Real Nodes Only)
# ---------------------------------------------------------------------------

@vpn_access_api.get("/servers")
async def get_servers(
    request: Request,
    response: Response = None,
    user_id: int = Depends(_servers_user_id),
):
    response = response or Response()
    correlation_id = _request_correlation_id(request)
    response.headers["X-Correlation-ID"] = correlation_id
    try:
        headers = await _require_opted_in_device_access(user_id, request)
    except HTTPException as exc:
        error_code = {
            401: "authentication_required",
            403: "device_confirmation_required",
            422: "device_contract_invalid",
            503: "device_validation_unavailable",
        }.get(exc.status_code, "device_access_denied")
        logger.warning(
            "Servers failure stage=device_access code=%s correlation_id=%s",
            error_code, correlation_id,
        )
        detail = (
            "Войдите в аккаунт" if exc.status_code == 401 else
            "Устройство ещё не подтверждено подпиской" if exc.status_code == 403 else
            "Не удалось подтвердить устройство. Проверьте данные приложения" if exc.status_code == 422 else
            "Не удалось проверить доступ. Попробуйте позже"
        )
        raise _correlated_http_error(exc.status_code, detail, correlation_id, error_code) from None
    try:
        sub_url, servers, sub_dict = await get_user_real_subscription(user_id, headers)
    except HTTPException as exc:
        error_code = "subscription_required" if exc.status_code == 403 else "subscription_lookup_failed"
        logger.warning(
            "Servers failure stage=subscription_lookup code=%s status=%s correlation_id=%s",
            error_code, exc.status_code, correlation_id,
        )
        detail = "Подписка не найдена или истекла" if exc.status_code == 403 else "Не удалось обновить список серверов"
        raise _correlated_http_error(exc.status_code, detail, correlation_id, error_code) from None
    except Exception as exc:
        logger.warning(
            "Servers failure stage=subscription_lookup code=%s correlation_id=%s",
            type(exc).__name__, correlation_id,
        )
        raise _correlated_http_error(
            503, "Не удалось обновить список серверов", correlation_id, "subscription_lookup_failed",
        ) from None
    if not servers:
        raise _correlated_http_error(
            503, "Не удалось получить конфигурацию серверов. Подписка сохранена",
            correlation_id, "subscription_configuration_unavailable",
        )
    payload = {
        "servers": servers,
        "subscription_url": sub_url,
    }
    if _entitlement_requested(headers):
        try:
            sub_dict = await _attach_signed_entitlement(user_id, headers, sub_dict)
            access_fields = _bootstrap_entitlement_fields(sub_dict)
            if not access_fields:
                logger.warning(
                    "Servers failure stage=entitlement code=entitlement_missing correlation_id=%s",
                    correlation_id,
                )
                raise _correlated_http_error(
                    403, "Устройство ещё не подтверждено подпиской", correlation_id,
                    "entitlement_missing",
                )
        except HTTPException as exc:
            if exc.headers and exc.headers.get("X-Correlation-ID"):
                raise
            error_code = "device_confirmation_required" if exc.status_code == 403 else "entitlement_unavailable"
            logger.warning(
                "Servers failure stage=entitlement code=%s status=%s correlation_id=%s",
                error_code, exc.status_code, correlation_id,
            )
            detail = "Устройство ещё не подтверждено подпиской" if exc.status_code == 403 else "Не удалось подтвердить подписку"
            raise _correlated_http_error(exc.status_code, detail, correlation_id, error_code) from None
        except Exception as exc:
            logger.warning(
                "Servers failure stage=entitlement code=%s correlation_id=%s",
                type(exc).__name__, correlation_id,
            )
            raise _correlated_http_error(
                503, "Не удалось подтвердить подписку", correlation_id, "entitlement_unavailable",
            ) from None
        payload.update(access_fields)
    return payload


@vpn_access_api.post("/vpn/session")
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

    # Older Windows releases still receive only VLESS Reality/TCP. New
    # versions advertise extra protocols explicitly at session selection.
    capabilities = request.headers.get("x-vpn-capabilities", "")

    def compatible_with_desktop(server: dict) -> bool:
        return windows_vpn_compatible(server, capabilities)

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

    response = dict(config)
    if _entitlement_requested(headers):
        sub_dict = await _attach_signed_entitlement(user_id, headers, sub_dict)
        access_fields = _bootstrap_entitlement_fields(sub_dict)
        if not access_fields:
            raise HTTPException(status_code=403, detail="Устройство не подтверждено для этой подписки")
        response.update(access_fields)
    return response


@vpn_access_api.delete("/vpn/session")
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


@vpn_access_api.get("/devices")
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


@vpn_access_api.delete("/devices/{device_id}")
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


# Assemble the versioned API at one mount point; route groups above keep
# authentication, billing, subscription, account, and VPN access boundaries
# explicit and independently reviewable.
api_router.include_router(auth_api)
api_router.include_router(account_api)
api_router.include_router(subscription_api)
api_router.include_router(billing_api)
api_router.include_router(vpn_access_api)
api_router.include_router(mobile_v2_router, prefix="/mobile/v2", tags=["mobile-v2"])
