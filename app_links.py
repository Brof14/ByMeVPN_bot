"""Helpers for opening the ByMeVPN mobile app directly from Telegram.

Telegram only accepts HTTP(S) or ``tg://`` URLs in inline URL buttons, so a
``bymevpn://`` deep link cannot be attached to a button directly.  Instead the
bot points the button at our small ``/app/login`` page, which tries to launch
the app immediately and keeps a manual button as a fallback.
"""
from __future__ import annotations

import html
import base64
import hashlib
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


def build_app_redirect_url(subscription_url: str = "", user_name: str = "") -> str:
    """Only hand credential-bearing subscription URLs to a trusted HTTPS origin."""
    base = urlsplit(APP_PUBLIC_URL)
    if subscription_url and (
        base.scheme != "https" or not base.hostname or base.username is not None
        or base.password is not None or base.query or base.fragment
    ):
        raise ValueError("APP_PUBLIC_URL must be a secure HTTPS origin for app handover")
    query = urlencode(_login_params(subscription_url, user_name))
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

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Открываем ByMeVPN…</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: #0d1117; color: #f0f6fc;
  }}
  main {{ width: 100%; max-width: 420px; text-align: center; }}
  .logo {{
    display: block; width: 132px; height: 132px; object-fit: contain;
    margin: 0 auto 28px;
    filter: drop-shadow(0 12px 30px rgba(0, 212, 255, .18));
  }}
  h1 {{ font-size: 22px; line-height: 1.3; margin: 0 0 12px; }}
  p {{ color: #9da7b3; line-height: 1.5; margin: 0 0 22px; }}
  .button {{
    display: block; padding: 16px 20px; border-radius: 14px;
    background: #2ee59d; color: #0d1117; font-size: 17px;
    font-weight: 800; text-decoration: none;
  }}
  .button:active {{ transform: scale(0.99); }}
  .hint {{ margin: 20px 0 0; font-size: 13px; }}
  .hint a {{ color: #7dd3fc; }}
</style>
</head>
<body>
<main>
  <img class="logo" src="/app/logo" alt="Логотип ByMeVPN" width="132" height="132">
  <h1>Открываем приложение…</h1>
  <p>Если ByMeVPN не открылся автоматически, нажмите кнопку ниже — подписка подключится сама.</p>
  <a id="open-app" class="button" href="{escaped_link}">Открыть ByMeVPN</a>
  <p class="hint">Если приложение не установлено, установите его и откройте эту ссылку снова.</p>
</main>
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


CONNECT_TICKET_TTL_SECONDS = 15 * 60


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
    payload = json.dumps({
        "sub": value,
        "name": "".join(ch for ch in user_name if ch.isprintable())[:MAX_USER_NAME_LENGTH],
        "exp": int(now if now is not None else time.time()) + CONNECT_TICKET_TTL_SECONDS,
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
        subscription_url = str(value.get("sub", ""))
        parsed = urlsplit(subscription_url)
        if (
            expiry < int(now if now is not None else time.time())
            or expiry > int(now if now is not None else time.time()) + CONNECT_TICKET_TTL_SECONDS + 5
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


def build_connect_page_url(subscription_url: str, user_name: str = "") -> str:
    # Keep direct-connect behaviour until a dedicated public HTTPS origin is
    # configured and its /connect route is exposed by the proxy. WEBHOOK_URL
    # may point to an internal or placeholder address and is not sufficient.
    if not APP_CONNECT_PAGE_URL:
        return subscription_url
    base = urlsplit(APP_CONNECT_PAGE_URL)
    if base.scheme != "https" or not base.hostname or base.path or base.username is not None or base.password is not None:
        raise ValueError("APP_CONNECT_PAGE_URL must be an HTTPS origin")
    ticket = build_connect_ticket(subscription_url, user_name)
    return f"{APP_CONNECT_PAGE_URL}/connect?ticket={quote(ticket)}"


def render_connection_choice_page(subscription_url: str, user_name: str = "") -> str:
    """A small, self-hosted OS/app chooser with automatic OS detection and fallback downloads."""
    sub = _js_string(subscription_url)
    byme_link = build_app_login_url(subscription_url, user_name)
    apps = {
        "android": [
            {"name": "ByMeVPN", "deep": byme_link, "download": "https://bymevpn.duckdns.org/app/"},
            {"name": "Happ", "deep": "happ://add/" + quote(subscription_url, safe=""), "download": "https://play.google.com/store/apps/details?id=com.happproxy"},
        ],
        "ios": [{"name": "Happ", "deep": "happ://add/" + quote(subscription_url, safe=""), "download": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"}],
        "windows": [{"name": "Happ", "deep": "happ://add/" + quote(subscription_url, safe=""), "download": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"}],
        "macos": [{"name": "Happ", "deep": "happ://add/" + quote(subscription_url, safe=""), "download": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg"}],
        "linux": [{"name": "Happ", "deep": "happ://add/" + quote(subscription_url, safe=""), "download": "https://github.com/Happ-proxy/happ-desktop/releases/latest"}],
    }
    apps_json = _js_string(json.dumps(apps, ensure_ascii=False))
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Подключение ByMeVPN</title>
<style>
:root{{color-scheme:dark;--bg:#0b1220;--card:#111c2d;--blue:#2878f0;--muted:#9eb0c7;--green:#26c281}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:linear-gradient(160deg,#0c1424,#08101b);color:#eef5ff;font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px;display:grid;place-items:center}}
main{{width:100%;max-width:480px}}h1{{font-size:24px;margin:0 0 8px}}p{{color:var(--muted);margin:0 0 20px}}.card{{background:var(--card);border:1px solid #23344c;border-radius:20px;padding:22px;box-shadow:0 18px 60px #0005}}
.os{{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}}button,a.btn{{font:inherit;color:#fff;border:0;border-radius:12px;padding:12px 15px;text-decoration:none;cursor:pointer}}
.os button{{background:#1b2b43}}.os button.active{{background:var(--blue)}}.app{{padding:15px 0;border-top:1px solid #26364b}}.app:first-child{{border:0}}.app h2{{font-size:17px;margin:0 0 10px}}
.btn{{display:block;text-align:center;background:var(--blue);font-weight:700;margin:8px 0}}.btn.alt{{background:#20324b}}.note{{font-size:13px;margin-top:16px}}.copy{{color:#b9d7ff;text-decoration:underline;background:none;border:0;padding:5px;font:inherit;cursor:pointer}}
</style></head><body><main><section class="card"><h1>Подключить ByMeVPN</h1>
<p>Выберите устройство и приложение. Мы уже определили вашу систему — при необходимости переключите её вручную.</p>
<div class="os" id="os"></div><div id="apps"></div>
<button class="copy" id="copy">Скопировать ссылку для ручного добавления</button>
<p class="note">Если приложение не установлено, скачайте его и вернитесь сюда. Страница подключения действует 15 минут — затем откройте её заново из бота.</p>
</section></main>
<script>
(function(){{const sub={sub};const apps=JSON.parse({apps_json});const labels={{android:"Android",ios:"iPhone / iPad",windows:"Windows",macos:"Mac",linux:"Linux"}};
const ua=navigator.userAgent;let current=/Android/i.test(ua)?"android":/iPhone|iPad/i.test(ua)?"ios":/Windows/i.test(ua)?"windows":/Macintosh/i.test(ua)?"macos":"linux";
const os=document.getElementById("os"),root=document.getElementById("apps");function render(){{os.innerHTML="";Object.keys(labels).forEach(k=>{{const b=document.createElement("button");b.textContent=labels[k];b.className=k===current?"active":"";b.onclick=()=>{{current=k;render()}};os.appendChild(b)}});
root.innerHTML="";apps[current].forEach(a=>{{const d=document.createElement("div");d.className="app";const h=document.createElement("h2");h.textContent=a.name;const open=document.createElement("a");open.className="btn";open.href=a.deep;open.textContent="Открыть и добавить подписку";const download=document.createElement("a");download.className="btn alt";download.href=a.download;download.target="_blank";download.rel="noopener";download.textContent="Скачать приложение";d.append(h,open,download);root.appendChild(d)}})}}
render();document.getElementById("copy").onclick=async()=>{{try{{await navigator.clipboard.writeText(sub);document.getElementById("copy").textContent="Ссылка скопирована"}}catch(_){{prompt("Скопируйте ссылку",sub)}}}};
}})();
</script></body></html>"""
