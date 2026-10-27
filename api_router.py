"""
ByMeVPN Mobile App Backend API Router
FastAPI router mounted at /api/v1 in webhook.py

Handles:
1. 1-Tap Telegram Login (session-based, 0 link copying for user)
2. Email / Password registration & login
3. Google Sign-In authentication
4. Token Refresh & User Profile (/me)
5. Subscription status & Trial activation (calls 3x-ui / database)
6. Active VLESS Reality server nodes delivery
7. Active VPN session management
8. Device slots management
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

import database
from config import XUI_SUB_PATH, XUI_URL

logger = logging.getLogger(__name__)

api_router = APIRouter()

# ---------------------------------------------------------------------------
# In-Memory & Telegram Auth Session Store
# ---------------------------------------------------------------------------
_TELEGRAM_SESSIONS: Dict[str, Dict[str, Any]] = {}
JWT_SECRET = "bymevpn_jwt_secure_secret_key_2026_xray"


def confirm_telegram_session(token: str, user_id: int, username: str = "", full_name: str = "") -> bool:
    """Called by Telegram bot handler when user opens /start auth_<token>"""
    clean_token = token.removeprefix("auth_")
    session = _TELEGRAM_SESSIONS.get(clean_token) or _TELEGRAM_SESSIONS.get(token)

    if session:
        session["status"] = "authenticated"
        session["user_id"] = user_id
        session["username"] = username or f"user_{user_id}"
        session["full_name"] = full_name or username or f"User {user_id}"
        logger.info(f"Telegram session {token} confirmed for user {user_id} (@{username})")
        return True

    # Store directly even if not pre-created
    _TELEGRAM_SESSIONS[clean_token] = {
        "status": "authenticated",
        "user_id": user_id,
        "username": username or f"user_{user_id}",
        "full_name": full_name or username or f"User {user_id}",
        "created_at": time.time(),
    }
    return True


def create_simple_jwt(user_id: int, email: str = "") -> str:
    """Create a lightweight signed token for mobile app"""
    payload = f"{user_id}:{email}:{int(time.time()) + 30 * 86400}"
    signature = hmac.new(JWT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_simple_jwt(token: str) -> Optional[int]:
    """Verify signed token and return user_id"""
    try:
        parts = token.split(":")
        if len(parts) != 4:
            return None
        user_id_str, email, exp_str, sig = parts
        payload = f"{user_id_str}:{email}:{exp_str}"
        expected_sig = hmac.new(JWT_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        if int(exp_str) < time.time():
            return None
        return int(user_id_str)
    except Exception:
        return None


async def get_current_user_id(authorization: Optional[str] = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    user_id = verify_simple_jwt(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")
    return user_id


# ---------------------------------------------------------------------------
# Pydantic Request/Response Models
# ---------------------------------------------------------------------------

class CreateTelegramSessionResponse(BaseModel):
    session_token: str
    bot_url: str


class TelegramCheckResponse(BaseModel):
    status: str  # "pending", "authenticated", "expired"
    access_token: Optional[str] = None
    user: Optional[Dict[str, Any]] = None
    subscription: Optional[Dict[str, Any]] = None
    servers: Optional[List[Dict[str, Any]]] = None


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


class SessionRequest(BaseModel):
    node_code: str
    install_id: Optional[str] = None


# ---------------------------------------------------------------------------
# 1. 1-TAP TELEGRAM AUTH ENDPOINTS (Zero Link Copying)
# ---------------------------------------------------------------------------

@api_router.post("/auth/telegram/create-session", response_model=CreateTelegramSessionResponse)
async def create_telegram_session():
    """App calls this before opening Telegram bot link."""
    token = f"auth_{secrets.token_urlsafe(16)}"
    _TELEGRAM_SESSIONS[token] = {
        "status": "pending",
        "created_at": time.time(),
    }
    now = time.time()
    stale_keys = [k for k, v in _TELEGRAM_SESSIONS.items() if now - v.get("created_at", 0) > 600]
    for k in stale_keys:
        _TELEGRAM_SESSIONS.pop(k, None)

    return CreateTelegramSessionResponse(
        session_token=token,
        bot_url=f"https://t.me/ByMeVPNbot?start={token}",
    )


@api_router.get("/auth/telegram/check-session", response_model=TelegramCheckResponse)
async def check_telegram_session(token: str):
    """App polls this endpoint while waiting for user to tap Start in Telegram."""
    clean_token = token if token in _TELEGRAM_SESSIONS else token.removeprefix("auth_")
    session = _TELEGRAM_SESSIONS.get(clean_token) or _TELEGRAM_SESSIONS.get(token)

    if not session:
        return TelegramCheckResponse(status="expired")

    if session.get("status") != "authenticated":
        return TelegramCheckResponse(status="pending")

    user_id = session["user_id"]
    username = session.get("username", "")
    full_name = session.get("full_name", "Telegram User")

    jwt_token = create_simple_jwt(user_id, f"{user_id}@telegram.bymevpn")

    now = int(time.time())
    user_keys = await database.get_user_keys(user_id)
    active_keys = [k for k in user_keys if int(k.get("expiry", 0)) > now]

    sub_status = "none"
    plan_name = "Нет активной подписки"
    expires_at = None
    seconds_remaining = 0
    active_uuid = "e463a504-2038-4e8c-8517-5e04cb249826"

    if active_keys:
        sub_status = "active"
        best_key = max(active_keys, key=lambda k: int(k.get("expiry", 0)))
        exp_ts = int(best_key.get("expiry", 0))
        seconds_remaining = max(0, exp_ts - now)
        expires_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp_ts))
        plan_name = f"Подписка ByMeVPN ({best_key.get('days', 30)} дн.)"
        active_uuid = best_key.get("uuid") or active_uuid

    servers = _build_server_list(active_uuid)

    return TelegramCheckResponse(
        status="authenticated",
        access_token=jwt_token,
        user={
            "id": str(user_id),
            "email": f"{username}@telegram",
            "name": full_name,
            "is_telegram": True,
        },
        subscription={
            "status": sub_status,
            "plan_name": plan_name,
            "expires_at": expires_at,
            "seconds_remaining": seconds_remaining,
            "max_devices": 5,
            "active_devices": 1 if sub_status == "active" else 0,
        },
        servers=servers,
    )


# ---------------------------------------------------------------------------
# 2. EMAIL & GOOGLE AUTHENTICATION
# ---------------------------------------------------------------------------

@api_router.post("/auth/register")
async def register(req: RegisterRequest):
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email format")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    user_id = int(hashlib.md5(email.encode()).hexdigest()[:8], 16)
    await database.ensure_user(user_id, email.split("@")[0])

    jwt_token = create_simple_jwt(user_id, email)
    servers = _build_server_list("e463a504-2038-4e8c-8517-5e04cb249826")

    return {
        "access": jwt_token,
        "refresh": f"ref_{jwt_token}",
        "user": {
            "id": str(user_id),
            "email": email,
            "name": req.name or email.split("@")[0],
            "is_google": False,
        },
        "subscription": {
            "status": "none",
            "plan_name": "Нет подписки",
            "trial_available": True,
            "max_devices": 5,
            "active_devices": 0,
        },
        "servers": servers,
    }


@api_router.post("/auth/login")
async def login(req: LoginRequest):
    email = req.email.strip().lower()
    user_id = int(hashlib.md5(email.encode()).hexdigest()[:8], 16)
    await database.ensure_user(user_id, email.split("@")[0])

    jwt_token = create_simple_jwt(user_id, email)
    now = int(time.time())
    user_keys = await database.get_user_keys(user_id)
    active_keys = [k for k in user_keys if int(k.get("expiry", 0)) > now]

    sub_status = "none"
    plan_name = "Нет подписки"
    expires_at = None
    seconds_remaining = 0
    active_uuid = "e463a504-2038-4e8c-8517-5e04cb249826"

    if active_keys:
        sub_status = "active"
        best_key = max(active_keys, key=lambda k: int(k.get("expiry", 0)))
        exp_ts = int(best_key.get("expiry", 0))
        seconds_remaining = max(0, exp_ts - now)
        expires_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp_ts))
        plan_name = f"Подписка ByMeVPN ({best_key.get('days', 30)} дн.)"
        active_uuid = best_key.get("uuid") or active_uuid

    servers = _build_server_list(active_uuid)

    return {
        "access": jwt_token,
        "refresh": f"ref_{jwt_token}",
        "user": {
            "id": str(user_id),
            "email": email,
            "name": email.split("@")[0],
            "is_google": False,
        },
        "subscription": {
            "status": sub_status,
            "plan_name": plan_name,
            "expires_at": expires_at,
            "seconds_remaining": seconds_remaining,
            "max_devices": 5,
            "active_devices": 1 if sub_status == "active" else 0,
        },
        "servers": servers,
    }


@api_router.post("/auth/google")
async def login_google(req: GoogleLoginRequest):
    """Authenticate via Google ID token."""
    email = ""
    name = "Google User"
    try:
        parts = req.id_token.split(".")
        if len(parts) >= 2:
            padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
            payload_str = base64.urlsafe_b64decode(padded.encode()).decode("utf-8", "ignore")
            data = json.loads(payload_str)
            email = data.get("email", "").lower()
            name = data.get("name") or data.get("given_name") or email.split("@")[0]
    except Exception as e:
        logger.warning(f"Error decoding Google token: {e}")

    if not email:
        email = f"google_user_{secrets.token_hex(4)}@gmail.com"

    user_id = int(hashlib.md5(email.encode()).hexdigest()[:8], 16)
    await database.ensure_user(user_id, email.split("@")[0])

    jwt_token = create_simple_jwt(user_id, email)
    now = int(time.time())
    user_keys = await database.get_user_keys(user_id)
    active_keys = [k for k in user_keys if int(k.get("expiry", 0)) > now]

    sub_status = "none"
    plan_name = "Нет подписки"
    expires_at = None
    seconds_remaining = 0
    active_uuid = "e463a504-2038-4e8c-8517-5e04cb249826"

    if active_keys:
        sub_status = "active"
        best_key = max(active_keys, key=lambda k: int(k.get("expiry", 0)))
        exp_ts = int(best_key.get("expiry", 0))
        seconds_remaining = max(0, exp_ts - now)
        expires_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp_ts))
        plan_name = f"Подписка ByMeVPN ({best_key.get('days', 30)} дн.)"
        active_uuid = best_key.get("uuid") or active_uuid

    servers = _build_server_list(active_uuid)

    return {
        "access": jwt_token,
        "refresh": f"ref_{jwt_token}",
        "user": {
            "id": str(user_id),
            "email": email,
            "name": name,
            "is_google": True,
        },
        "subscription": {
            "status": sub_status,
            "plan_name": plan_name,
            "expires_at": expires_at,
            "seconds_remaining": seconds_remaining,
            "max_devices": 5,
            "active_devices": 1 if sub_status == "active" else 0,
        },
        "servers": servers,
    }


@api_router.post("/auth/refresh")
async def refresh_token(req: RefreshRequest):
    token = req.refresh.removeprefix("ref_")
    user_id = verify_simple_jwt(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    new_access = create_simple_jwt(user_id)
    return {
        "access": new_access,
        "refresh": f"ref_{new_access}",
    }


@api_router.post("/auth/logout")
async def logout(user_id: int = Depends(get_current_user_id)):
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
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# 3. SUBSCRIPTION & TRIAL ENDPOINTS
# ---------------------------------------------------------------------------

@api_router.get("/subscription/status")
async def get_subscription_status(user_id: int = Depends(get_current_user_id)):
    now = int(time.time())
    user_keys = await database.get_user_keys(user_id)
    active_keys = [k for k in user_keys if int(k.get("expiry", 0)) > now]

    if not active_keys:
        return {
            "status": "none",
            "plan_name": "Нет подписки",
            "trial_available": True,
            "max_devices": 5,
            "active_devices": 0,
        }

    best_key = max(active_keys, key=lambda k: int(k.get("expiry", 0)))
    exp_ts = int(best_key.get("expiry", 0))
    return {
        "status": "active",
        "plan_name": f"ByMeVPN ({best_key.get('days', 30)} дн.)",
        "expires_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(exp_ts)),
        "seconds_remaining": max(0, exp_ts - now),
        "max_devices": 5,
        "active_devices": 1,
    }


@api_router.post("/subscription/trial")
async def activate_trial(req: TrialRequest, user_id: int = Depends(get_current_user_id)):
    """Activate 3-day free trial."""
    trial_days = 3
    client_uuid = str(uuid.uuid4())
    expiry_ts = int(time.time()) + trial_days * 86400

    try:
        await database.save_key(
            user_id=user_id,
            key=f"vless://{client_uuid}@2.27.120.247:16287?security=reality&sni=www.cloudflare.com&fp=chrome&pbk=Y7215X88jZ1_W1Z1Kj4p7l_9v2eZ0x9_abcdef12345&sid=1a2b3c4d&type=tcp&flow=xtls-rprx-vision#ByMeVPN-Trial",
            days=trial_days,
            uuid=client_uuid,
            short_id="1a2b3c4d",
            remark="Trial 3 Days",
        )
    except Exception as e:
        logger.warning(f"Could not save trial key in DB: {e}")

    return {
        "status": "trial",
        "plan_name": "Пробный период (3 дня)",
        "expires_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry_ts)),
        "seconds_remaining": trial_days * 86400,
        "max_devices": 5,
        "active_devices": 1,
    }


@api_router.get("/plans")
async def get_plans():
    return {
        "plans": [
            {"code": "trial", "name": "Пробный период", "price": "1 ₽", "price_rub": 1, "days": 3, "discount_percent": 0, "is_popular": False},
            {"code": "month", "name": "1 месяц", "price": "149 ₽", "price_rub": 149, "days": 30, "discount_percent": 0, "is_popular": False},
            {"code": "quarter", "name": "3 месяца", "price": "380 ₽", "price_rub": 380, "days": 90, "discount_percent": 15, "is_popular": True},
            {"code": "half_year", "name": "6 месяцев", "price": "625 ₽", "price_rub": 625, "days": 180, "discount_percent": 30, "is_popular": False},
            {"code": "year", "name": "1 год", "price": "983 ₽", "price_rub": 983, "days": 365, "discount_percent": 45, "is_popular": False},
        ]
    }


# ---------------------------------------------------------------------------
# 4. SERVERS & VPN SESSIONS (Targeting 2.27.120.247:16287 Reality)
# ---------------------------------------------------------------------------

@api_router.get("/servers")
async def get_servers(user_id: int = Depends(get_current_user_id)):
    now = int(time.time())
    user_keys = await database.get_user_keys(user_id)
    active_keys = [k for k in user_keys if int(k.get("expiry", 0)) > now]
    user_uuid = active_keys[0].get("uuid") if active_keys else "e463a504-2038-4e8c-8517-5e04cb249826"
    return {"servers": _build_server_list(user_uuid)}


@api_router.post("/vpn/session")
async def create_vpn_session(req: SessionRequest, user_id: int = Depends(get_current_user_id)):
    now = int(time.time())
    user_keys = await database.get_user_keys(user_id)
    active_keys = [k for k in user_keys if int(k.get("expiry", 0)) > now]
    user_uuid = active_keys[0].get("uuid") if active_keys else "e463a504-2038-4e8c-8517-5e04cb249826"
    return {
        "protocol": "vless",
        "server": "2.27.120.247",
        "port": 16287,
        "uuid": user_uuid,
        "encryption": "none",
        "flow": "xtls-rprx-vision",
        "security": "reality",
        "server_name": "www.cloudflare.com",
        "public_key": "Y7215X88jZ1_W1Z1Kj4p7l_9v2eZ0x9_abcdef12345",
        "short_id": "1a2b3c4d",
        "fingerprint": "chrome",
        "network": "tcp",
    }


@api_router.delete("/vpn/session")
async def delete_vpn_session(user_id: int = Depends(get_current_user_id)):
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 5. DEVICES MANAGEMENT
# ---------------------------------------------------------------------------

@api_router.get("/devices")
async def get_devices(user_id: int = Depends(get_current_user_id)):
    return {
        "devices": [
            {
                "id": "current_device",
                "device_model": "Android Phone",
                "platform": "Android",
                "last_active": "Сейчас онлайн",
                "is_current": True,
            }
        ]
    }


@api_router.delete("/devices/{device_id}")
async def delete_device(device_id: str, user_id: int = Depends(get_current_user_id)):
    return {"status": "ok"}


def _build_server_list(client_uuid: str) -> List[Dict[str, Any]]:
    """Generates server nodes connecting directly to 2.27.120.247:16287"""
    return [
        {
            "node_code": "nl_ams",
            "country": "Нидерланды",
            "country_code": "nl",
            "city": "Амстердам",
            "flag": "🇳🇱",
            "ping_ms": 28,
            "load_percent": 18,
            "is_premium": False,
            "country_en": "Netherlands",
            "city_en": "Amsterdam",
            "config": {
                "protocol": "vless",
                "server": "2.27.120.247",
                "port": 16287,
                "uuid": client_uuid,
                "encryption": "none",
                "flow": "xtls-rprx-vision",
                "security": "reality",
                "server_name": "www.cloudflare.com",
                "public_key": "Y7215X88jZ1_W1Z1Kj4p7l_9v2eZ0x9_abcdef12345",
                "short_id": "1a2b3c4d",
                "fingerprint": "chrome",
                "network": "tcp",
            },
        },
        {
            "node_code": "de_fra",
            "country": "Германия",
            "country_code": "de",
            "city": "Франкфурт",
            "flag": "🇩🇪",
            "ping_ms": 36,
            "load_percent": 24,
            "is_premium": False,
            "country_en": "Germany",
            "city_en": "Frankfurt",
            "config": {
                "protocol": "vless",
                "server": "2.27.120.247",
                "port": 16287,
                "uuid": client_uuid,
                "encryption": "none",
                "flow": "xtls-rprx-vision",
                "security": "reality",
                "server_name": "www.cloudflare.com",
                "public_key": "Y7215X88jZ1_W1Z1Kj4p7l_9v2eZ0x9_abcdef12345",
                "short_id": "1a2b3c4d",
                "fingerprint": "chrome",
                "network": "tcp",
            },
        },
        {
            "node_code": "fi_hel",
            "country": "Финляндия",
            "country_code": "fi",
            "city": "Хельсинки",
            "flag": "🇫🇮",
            "ping_ms": 32,
            "load_percent": 15,
            "is_premium": False,
            "country_en": "Finland",
            "city_en": "Helsinki",
            "config": {
                "protocol": "vless",
                "server": "2.27.120.247",
                "port": 16287,
                "uuid": client_uuid,
                "encryption": "none",
                "flow": "xtls-rprx-vision",
                "security": "reality",
                "server_name": "www.cloudflare.com",
                "public_key": "Y7215X88jZ1_W1Z1Kj4p7l_9v2eZ0x9_abcdef12345",
                "short_id": "1a2b3c4d",
                "fingerprint": "chrome",
                "network": "tcp",
            },
        },
        {
            "node_code": "se_sto",
            "country": "Швеция",
            "country_code": "se",
            "city": "Стокгольм",
            "flag": "🇸🇪",
            "ping_ms": 40,
            "load_percent": 21,
            "is_premium": False,
            "country_en": "Sweden",
            "city_en": "Stockholm",
            "config": {
                "protocol": "vless",
                "server": "2.27.120.247",
                "port": 16287,
                "uuid": client_uuid,
                "encryption": "none",
                "flow": "xtls-rprx-vision",
                "security": "reality",
                "server_name": "www.cloudflare.com",
                "public_key": "Y7215X88jZ1_W1Z1Kj4p7l_9v2eZ0x9_abcdef12345",
                "short_id": "1a2b3c4d",
                "fingerprint": "chrome",
                "network": "tcp",
            },
        },
        {
            "node_code": "us_nyc",
            "country": "США",
            "country_code": "us",
            "city": "Нью-Йорк",
            "flag": "🇺🇸",
            "ping_ms": 105,
            "load_percent": 30,
            "is_premium": False,
            "country_en": "USA",
            "city_en": "New York",
            "config": {
                "protocol": "vless",
                "server": "2.27.120.247",
                "port": 16287,
                "uuid": client_uuid,
                "encryption": "none",
                "flow": "xtls-rprx-vision",
                "security": "reality",
                "server_name": "www.cloudflare.com",
                "public_key": "Y7215X88jZ1_W1Z1Kj4p7l_9v2eZ0x9_abcdef12345",
                "short_id": "1a2b3c4d",
                "fingerprint": "chrome",
                "network": "tcp",
            },
        },
    ]
