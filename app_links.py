"""Helpers for opening the ByMeVPN mobile app directly from Telegram.

Telegram only accepts HTTP(S) or ``tg://`` URLs in inline URL buttons, so a
``bymevpn://`` deep link cannot be attached to a button directly.  Instead the
bot points the button at our small ``/app/login`` page, which tries to launch
the app immediately and keeps a manual button as a fallback.
"""
from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import quote, urlencode, urlsplit

from config import APP_CONNECT_PAGE_URL, APP_PUBLIC_URL, BOT_TOKEN

APP_SCHEME = "bymevpn"

# The handover page is intentionally locked down. The brand mark is served from
# the same origin, so only ``img-src 'self'`` is added to the otherwise closed
# default policy; without it browsers hide the logo and show its alt text.
APP_LOGIN_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
)

# Keep the redirect page small and predictable; these limits also protect the
# endpoint from unusually long query strings.
MAX_SUBSCRIPTION_URL_LENGTH = 2048
MAX_USER_NAME_LENGTH = 128


def _login_params(subscription_url: str = "", user_name: str = "") -> dict[str, str]:
    params: dict[str, str] = {}
    if subscription_url:
        params["sub"] = subscription_url
    if user_name:
        params["name"] = user_name
    return params


def build_app_login_url(subscription_url: str = "", user_name: str = "") -> str:
    """Build the deep link understood by the ByMeVPN app."""
    query = urlencode(_login_params(subscription_url, user_name))
    return f"{APP_SCHEME}://login" + (f"?{query}" if query else "")


def build_app_telegram_auth_url() -> str:
    """Start the app's own Telegram-confirmed sign-in without exposing a VPN key."""
    return f"{APP_SCHEME}://login?auth=telegram"


def build_app_redirect_url(subscription_url: str = "", user_name: str = "") -> str:
    """Hand a short-lived encrypted ticket to the trusted HTTPS app opener."""
    base = urlsplit(APP_PUBLIC_URL)
    if subscription_url and (
        base.scheme != "https" or not base.hostname or base.username is not None
        or base.password is not None or base.query or base.fragment
    ):
        raise ValueError("APP_PUBLIC_URL must be a secure HTTPS origin for app handover")
    query = urlencode({"ticket": build_connect_ticket(subscription_url, user_name)} if subscription_url else {})
    return f"{APP_PUBLIC_URL}/app/login" + (f"?{query}" if query else "")


def build_android_intent_url(deep_link: str) -> str:
    """Android intent URL; more reliable than a bare custom scheme in Chrome."""
    parts = urlsplit(deep_link)
    target = f"{parts.netloc}{parts.path}"
    query = f"?{parts.query}" if parts.query else ""
    return f"intent://{target}{query}#Intent;scheme={parts.scheme};end"


def _js_string(value: str) -> str:
    """Encode a value for inline JavaScript without allowing </script> breaks."""
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_app_open_page(deep_link: str) -> str:
    """Render the page that hands the user over from the browser to the app."""
    if not deep_link.startswith(f"{APP_SCHEME}://"):
        raise ValueError("invalid app deep link")

    escaped_link = html.escape(deep_link, quote=True)
    js_link = _js_string(deep_link)
    js_intent = _js_string(build_android_intent_url(deep_link))

    install_url = html.escape(APP_PUBLIC_URL.rstrip("/") + "/app", quote=True)
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Открываем ByMeVPN…</title>
<style>
  /* Hallmark · v2.0.0 · custom brand direction: quiet navy, electric blue and green */
  /* Critique: one clear handoff action, with the subscription-safe deep link preserved. */
  :root {{
    color-scheme: dark;
    --font-ui: Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
    --color-bg: #061020;
    --color-surface: #0d1b2c;
    --color-line: #20364d;
    --color-ink: #f3f8ff;
    --color-muted: #b3c2d2;
    --color-quiet: #8195aa;
    --color-blue: #21a5ff;
    --color-cyan: #27d5e8;
    --color-green: #43e58b;
    --color-focus: #a6ffcb;
    --color-shadow: rgb(0 0 0 / 34%);
    --radius-card: 24px;
    --radius-control: 14px;
  }}
  *, *::before, *::after {{ box-sizing: border-box; }}
  html, body {{ overflow-x: clip; }}
  body {{
    margin: 0; min-height: 100svh; display: grid; place-items: center;
    padding: clamp(16px, 5vw, 40px);
    font-family: var(--font-ui); background: var(--color-bg); color: var(--color-ink);
  }}
  main {{ width: min(100%, 440px); min-width: 0; }}
  .panel {{ position: relative; overflow: clip; padding: clamp(24px, 6vw, 40px); border: 1px solid var(--color-line); border-radius: var(--radius-card); background: var(--color-surface); box-shadow: 0 24px 70px var(--color-shadow); text-align: center; }}
  .panel::before {{ position: absolute; inset: 0 0 auto; height: 3px; background: linear-gradient(112deg, var(--color-blue), var(--color-cyan) 54%, var(--color-green)); content: ""; }}
  .logo {{
    display: block; width: min(36vw, 132px); height: auto; aspect-ratio: 1; object-fit: contain;
    margin: 0 auto 24px; border: 1px solid var(--color-line); border-radius: 22px;
  }}
  h1 {{ font-size: clamp(1.4rem, 6vw, 1.8rem); line-height: 1.2; overflow-wrap: break-word; min-width: 0; margin: 0 0 12px; }}
  p {{ color: var(--color-muted); line-height: 1.55; margin: 0 0 22px; }}
  .button {{
    display: flex; min-height: 54px; align-items: center; justify-content: center;
    padding: 14px 18px; border: 1px solid var(--color-green); border-radius: var(--radius-control);
    background: var(--color-green); color: var(--color-bg); font-size: 17px;
    font-weight: 750; text-decoration: none;
  }}
  .button:hover {{ filter: brightness(1.08); }}
  a:focus-visible {{ outline: 3px solid var(--color-focus); outline-offset: 4px; }}
  .hint {{ margin: 18px 0 0; color: var(--color-quiet); font-size: .9rem; }}
  .hint a {{ color: var(--color-blue); }}
  @media (prefers-reduced-motion: reduce) {{ html {{ scroll-behavior: auto; }} }}
</style>
</head>
<body>
<main><section class="panel">
  <img class="logo" src="/app/logo" alt="Логотип ByMeVPN" width="132" height="132">
  <h1>Открываем приложение…</h1>
  <p>Открываем ByMeVPN. Страница передаёт подписку в приложение; само открытие ещё не подтверждает подключение VPN.</p>
  <a id="open-app" class="button" href="{escaped_link}">Открыть ByMeVPN</a>
  <p class="hint">Если приложение не открылось, нажмите кнопку ещё раз. <a href="{install_url}">Скачать ByMeVPN для Android</a> · <a href="https://t.me/ByMeVPNSupportBot">Поддержка</a></p>
</section></main>
<script>
(function () {{
  var deepLink = {js_link};
  var target = /Android/i.test(navigator.userAgent) ? {js_intent} : deepLink;
  var button = document.getElementById("open-app");
  if (button) button.href = target;
  window.setTimeout(function () {{ window.location.href = target; }}, 150);
}})();
</script>
</body>
</html>"""


CONNECT_TICKET_TTL_SECONDS = 60 * 60
LEGACY_CONNECT_TICKET_TTL_SECONDS = 15 * 60


def _connect_aead():
    """Use the shared API secret so primary and secondary bots can serve the same page."""
    handoff_secret = os.getenv("APP_HANDOFF_SECRET") or os.getenv("JWT_SECRET") or BOT_TOKEN
    if not handoff_secret:
        raise RuntimeError("connection page secret is not configured")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = hashlib.sha256(b"bymevpn-connect-page-v1\0" + handoff_secret.encode("utf-8")).digest()
    return AESGCM(key)


def build_connect_ticket(subscription_url: str, user_name: str = "", *, now: int | None = None) -> str:
    """Encrypt the subscription link in a short-lived browser handover ticket."""
    value = subscription_url.strip()
    parsed = urlsplit(value)
    if (
        len(value) > MAX_SUBSCRIPTION_URL_LENGTH or parsed.scheme != "https"
        or not parsed.hostname or parsed.username is not None or parsed.password is not None
    ):
        raise ValueError("invalid secure subscription URL")
    issued = int(now if now is not None else time.time())
    payload = json.dumps({
        "sub": value,
        "name": "".join(ch for ch in user_name if ch.isprintable())[:MAX_USER_NAME_LENGTH],
        "iat": issued,
        "exp": issued + CONNECT_TICKET_TTL_SECONDS,
    }, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(12)
    encrypted = _connect_aead().encrypt(nonce, payload, b"bymevpn-connect-ticket-v1")
    return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii").rstrip("=")


def read_connect_ticket(ticket: str, *, now: int | None = None) -> dict[str, str]:
    """Decrypt and validate a handover ticket; invalid and expired tickets fail closed."""
    if not ticket or len(ticket) > 4096:
        raise ValueError("invalid connection ticket")
    try:
        raw = base64.urlsafe_b64decode(ticket + "=" * (-len(ticket) % 4))
        if len(raw) < 29:
            raise ValueError
        payload = _connect_aead().decrypt(raw[:12], raw[12:], b"bymevpn-connect-ticket-v1")
        value = json.loads(payload)
        expiry = int(value.get("exp", 0))
        current = int(now if now is not None else time.time())
        if "iat" in value:
            issued = int(value["iat"])
            if issued > current + 5 or expiry != issued + CONNECT_TICKET_TTL_SECONDS:
                raise ValueError
        else:
            # Tickets created by the earlier 15-minute release remain usable for
            # up to one hour from issue, including links already sent by the bot.
            if expiry > current + LEGACY_CONNECT_TICKET_TTL_SECONDS + 5:
                raise ValueError
            expiry += CONNECT_TICKET_TTL_SECONDS - LEGACY_CONNECT_TICKET_TTL_SECONDS
        subscription_url = str(value.get("sub", ""))
        parsed = urlsplit(subscription_url)
        if (
            expiry < current
            or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or len(subscription_url) > MAX_SUBSCRIPTION_URL_LENGTH
        ):
            raise ValueError
        return {
            "subscription_url": subscription_url,
            "user_name": "".join(ch for ch in str(value.get("name", "")) if ch.isprintable())[:MAX_USER_NAME_LENGTH],
        }
    except Exception as exc:
        raise ValueError("invalid or expired connection ticket") from exc


def build_connect_page_url(subscription_url: str, user_name: str = "", *, user_id: int | None = None) -> str:
    if not APP_CONNECT_PAGE_URL:
        raise RuntimeError("APP_CONNECT_PAGE_URL must be configured for secure connection handover")
    base = urlsplit(APP_CONNECT_PAGE_URL)
    if base.scheme != "https" or not base.hostname or base.path or base.username is not None or base.password is not None:
        raise ValueError("APP_CONNECT_PAGE_URL must be an HTTPS origin")
    if user_id is not None:
        secret = os.getenv("APP_HANDOFF_SECRET") or os.getenv("JWT_SECRET") or BOT_TOKEN
        if not secret or int(user_id) <= 0:
            raise RuntimeError("connection page secret is not configured")
        uid = str(int(user_id))
        mac = hmac.new(hashlib.sha256(secret.encode()).digest(), f"connect-v1:{uid}".encode(), hashlib.sha256).hexdigest()
        return f"{APP_CONNECT_PAGE_URL}/connect?u={uid}&s={mac}"
    ticket = build_connect_ticket(subscription_url, user_name)
    return f"{APP_CONNECT_PAGE_URL}/connect?ticket={quote(ticket)}"


def verify_permanent_connect_token(user_id: int, signature: str) -> bool:
    secret = os.getenv("APP_HANDOFF_SECRET") or os.getenv("JWT_SECRET") or BOT_TOKEN
    if not secret or int(user_id) <= 0 or len(signature) != 64:
        return False
    expected = hmac.new(hashlib.sha256(secret.encode()).digest(), f"connect-v1:{int(user_id)}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@lru_cache(maxsize=1)
def _connection_page_template() -> str:
    return (Path(__file__).resolve().parent / "templates" / "connect.html").read_text(encoding="utf-8")


def render_connection_choice_page(subscription_url: str, user_name: str = "") -> str:
    """Render a responsive branded OS/app chooser with verified import fallbacks."""
    sub = _js_string(subscription_url)
    encoded_sub = quote(subscription_url, safe="")
    bymevpn_download = f"{APP_PUBLIC_URL.rstrip('/')}/app"
    tun = {"name": "v2rayTun", "icon": "v2raytun", "deep": "v2raytun://import/" + subscription_url, "download": "https://v2raytun.com/"}
    hiddify = {"name": "Hiddify", "icon": "hiddify", "deep": "hiddify://install-sub?url=" + encoded_sub + "#ByMeVPN", "download": "https://github.com/hiddify/hiddify-app/releases/latest"}
    singbox = {"name": "sing-box", "icon": "singbox", "manual": True, "note": "Требуется конфигурация sing-box в формате JSON; обычная ссылка подписки может не подойти.", "download": "https://sing-box.sagernet.org/clients/"}
    apps = {
        "android": [
            {"name": "ByMeVPN", "icon": "bymevpn", "deep": build_app_telegram_auth_url(), "download": bymevpn_download},
            {"name": "Happ", "icon": "happ", "deep": "happ://add/" + subscription_url, "download": "https://github.com/Happ-proxy/happ-android/releases/latest"},
            {"name": "INCY", "icon": "incy", "deep": "incy://import/" + subscription_url, "download": "https://play.google.com/store/apps/details?id=llc.itdev.incy"},
            tun,
            {"name": "v2rayNG", "icon": "v2rayng", "deep": "v2rayng://install-sub?url=" + encoded_sub + "#ByMeVPN", "download": "https://github.com/2dust/v2rayNG/releases/latest"},
            hiddify, singbox,
        ],
        "ios": [
            {"name": "Happ", "icon": "happ", "deep": "happ://add/" + subscription_url, "download": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"},
            {"name": "INCY", "icon": "incy", "deep": "incy://import/" + subscription_url, "download": "https://apps.apple.com/us/app/incy/id6756943388"},
            tun, hiddify, singbox,
        ],
        "windows": [
            {"name": "Happ", "icon": "happ", "deep": "happ://add/" + subscription_url, "download": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"},
            {"name": "v2rayN", "icon": "v2rayn", "manual": True, "download": "https://github.com/2dust/v2rayN/releases/latest"},
            {"name": "Koala Clash", "icon": "koala", "manual": True, "download": "https://github.com/coolcoala/koala-clash/releases/latest"},
            tun, hiddify,
        ],
        "macos": [
            {"name": "Happ", "icon": "happ", "deep": "happ://add/" + subscription_url, "download": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg"},
            {"name": "INCY", "icon": "incy", "deep": "incy://import/" + subscription_url, "download": "https://docs.incy.cc/"},
            {"name": "v2rayN", "icon": "v2rayn", "manual": True, "download": "https://github.com/2dust/v2rayN/releases/latest"},
            {"name": "Koala Clash", "icon": "koala", "manual": True, "download": "https://github.com/coolcoala/koala-clash/releases/latest"},
            tun, hiddify,
        ],
        "linux": [
            {"name": "Happ", "icon": "happ", "deep": "happ://add/" + subscription_url, "download": "https://github.com/Happ-proxy/happ-desktop/releases/latest"},
            {"name": "v2rayN", "icon": "v2rayn", "manual": True, "download": "https://github.com/2dust/v2rayN/releases/latest"},
            {"name": "Koala Clash", "icon": "koala", "manual": True, "download": "https://github.com/coolcoala/koala-clash/releases/latest"},
            hiddify,
        ],
    }
    apps_json = _js_string(json.dumps(apps, ensure_ascii=False))
    return _connection_page_template().replace("__APPS__", apps_json, 1).replace("__SUB__", sub, 1)
