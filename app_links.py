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
  <p>Если ByMeVPN не открылся автоматически, нажмите кнопку ниже — подписка подключится сама.</p>
  <a id="open-app" class="button" href="{escaped_link}">Открыть ByMeVPN</a>
  <p class="hint">Если приложение не установлено, установите его и откройте эту ссылку снова.</p>
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
    """Render a responsive branded OS/app chooser with verified import fallbacks."""
    sub = _js_string(subscription_url)
    byme_link = build_app_login_url(subscription_url, user_name)
    apps = {
        "android": [
            {"name": "ByMeVPN", "icon": "bymevpn", "deep": byme_link, "download": "https://github.com/Brof14/ByMeVPN-app/releases/download/v1.0.12/ByMeVPN-v1.0.12.apk", "hint": "Добавит подписку в ByMeVPN"},
            {"name": "Happ", "icon": "happ", "deep": "happ://add/" + subscription_url, "download": "https://github.com/Happ-proxy/happ-android/releases/latest", "hint": "Если импорт не открылся, скопируйте ссылку и добавьте её в Happ"},
            {"name": "INCY", "icon": "incy", "deep": "incy://import/" + subscription_url, "download": "https://play.google.com/store/apps/details?id=llc.itdev.incy", "hint": "Если импорт не открылся, скопируйте ссылку и добавьте её в INCY"},
        ],
        "ios": [
            {"name": "Happ", "icon": "happ", "deep": "happ://add/" + subscription_url, "download": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215", "hint": "Откроется подтверждение добавления подписки"},
            {"name": "INCY", "icon": "incy", "deep": "incy://import/" + subscription_url, "download": "https://apps.apple.com/us/app/incy/id6756943388", "hint": "Откроется импорт подписки"},
        ],
        "windows": [
            {"name": "Happ", "icon": "happ", "deep": "happ://add/" + subscription_url, "download": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe", "hint": "Откроется подтверждение добавления подписки"},
            {"name": "v2rayN", "icon": "v2rayn", "manual": True, "download": "https://github.com/2dust/v2rayN/releases/latest", "hint": "Добавьте ссылку в подписки; TUN можно включить в настройках v2rayN"},
            {"name": "Koala Clash", "icon": "koala", "manual": True, "download": "https://github.com/coolcoala/koala-clash/releases/latest", "hint": "Clash-совместимый профиль может импортироваться не полностью"},
        ],
        "macos": [
            {"name": "Happ", "icon": "happ", "deep": "happ://add/" + subscription_url, "download": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg", "hint": "Откроется подтверждение добавления подписки"},
            {"name": "INCY", "icon": "incy", "deep": "incy://import/" + subscription_url, "download": "https://docs.incy.cc/", "hint": "Откроется импорт подписки"},
            {"name": "v2rayN", "icon": "v2rayn", "manual": True, "download": "https://github.com/2dust/v2rayN/releases/latest", "hint": "Добавьте ссылку в подписки; TUN можно включить в настройках v2rayN"},
            {"name": "Koala Clash", "icon": "koala", "manual": True, "download": "https://github.com/coolcoala/koala-clash/releases/latest", "hint": "Clash-совместимый профиль может импортироваться не полностью"},
        ],
        "linux": [
            {"name": "Happ", "icon": "happ", "deep": "happ://add/" + subscription_url, "download": "https://github.com/Happ-proxy/happ-desktop/releases/latest", "hint": "Откроется подтверждение добавления подписки"},
            {"name": "v2rayN", "icon": "v2rayn", "manual": True, "download": "https://github.com/2dust/v2rayN/releases/latest", "hint": "Добавьте ссылку в подписки; TUN можно включить в настройках v2rayN"},
            {"name": "Koala Clash", "icon": "koala", "manual": True, "download": "https://github.com/coolcoala/koala-clash/releases/latest", "hint": "Clash-совместимый профиль может импортироваться не полностью"},
        ],
    }
    apps_json = _js_string(json.dumps(apps, ensure_ascii=False))
    template = '''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><meta name="theme-color" content="#061020"><title>Подключение ByMeVPN</title>
<style>
/* Hallmark · v2.0.0 · custom brand direction: calm navy with electric blue and green */
/* Critique: platform choice is the task; custom SVG marks replace emoji, no app claims are invented. */
:root{color-scheme:dark;--font-ui:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;--bg:#061020;--surface:#0d1b2c;--surface-soft:#102238;--line:#20364d;--ink:#f3f8ff;--muted:#b3c2d2;--quiet:#8195aa;--blue:#21a5ff;--cyan:#27d5e8;--green:#43e58b;--focus:#a6ffcb;--shadow:rgb(0 0 0 / 34%);--brand:linear-gradient(112deg,var(--blue),var(--cyan) 54%,var(--green));--measure:980px;--radius:24px}
*,*::before,*::after{box-sizing:border-box}html,body{overflow-x:clip}html{scroll-behavior:smooth}body{min-height:100svh;margin:0;padding:clamp(10px,3vw,32px);display:grid;place-items:center;background:var(--bg);color:var(--ink);font:16px/1.55 var(--font-ui)}
main{width:min(100%,var(--measure));min-width:0}.shell{position:relative;overflow:clip;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);box-shadow:0 24px 80px var(--shadow)}.shell::before{position:absolute;inset:0 0 auto;height:3px;background:var(--brand);content:""}.head{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:24px;padding:clamp(22px,5vw,48px);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:16px;min-width:0}.logo{width:clamp(58px,10vw,92px);height:auto;aspect-ratio:1;object-fit:contain;border:1px solid var(--line);border-radius:20px}.display{min-width:0}h1,h2,p{margin-top:0}h1{margin-bottom:7px;overflow-wrap:break-word;min-width:0;font-size:clamp(1.55rem,4vw,2.25rem);line-height:1.12;letter-spacing:-.035em}.lead{max-width:55ch;margin:0;color:var(--muted)}.secure{padding:8px 12px;border:1px solid var(--line);border-radius:999px;color:var(--quiet);font-size:.84rem;white-space:nowrap}
.content{padding:clamp(20px,4vw,40px)}.section-title{margin-bottom:12px;font-size:1rem;font-weight:650}.platforms{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.platform{min-height:82px;display:grid;place-items:center;align-content:center;gap:7px;padding:10px 7px;border:1px solid var(--line);border-radius:16px;background:var(--surface-soft);color:var(--muted);font:inherit;cursor:pointer}.platform[aria-pressed="true"]{border-color:var(--blue);background:var(--surface);color:var(--ink);box-shadow:inset 0 0 0 1px var(--blue)}.mark{width:30px;height:30px;display:block;object-fit:contain}.platform-label{font-size:.88rem;text-align:center;overflow-wrap:break-word;min-width:0}
.apps{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:26px}.app{min-width:0;padding:18px;border:1px solid var(--line);border-radius:18px;background:var(--surface-soft)}.app-head{display:flex;align-items:center;gap:12px;min-width:0;margin-bottom:10px}.app-mark{width:42px;height:42px;flex:0 0 42px;display:grid;place-items:center;border:1px solid var(--line);border-radius:13px;background:var(--surface);color:var(--green)}.app-logo{width:100%;height:100%;object-fit:contain;border-radius:8px}.app-logo-vector{display:grid;place-items:center}.app-logo-vector svg{width:27px;height:27px}.app h2{min-width:0;margin:0;overflow-wrap:break-word;font-size:1.05rem;line-height:1.25}.hint{min-height:3em;margin-bottom:14px;color:var(--muted);font-size:.9rem}.actions{display:grid;gap:8px}.button{min-height:46px;display:flex;align-items:center;justify-content:center;padding:10px 14px;border:1px solid var(--line);border-radius:12px;background:var(--blue);color:var(--bg);font:inherit;font-weight:700;text-align:center;text-decoration:none;cursor:pointer}.button.secondary{background:transparent;color:var(--ink)}.button.copy{border-color:var(--line);background:var(--surface);color:var(--muted);font-weight:550}.button:hover{filter:brightness(1.08)}button:focus-visible,a:focus-visible{outline:3px solid var(--focus);outline-offset:3px}.status{min-height:1.5em;margin:16px 0 0;color:var(--green);font-size:.9rem}.note{margin:18px 0 0;padding-top:16px;border-top:1px solid var(--line);color:var(--quiet);font-size:.88rem}.noscript{margin:20px 0 0;padding:14px;border:1px solid var(--line);border-radius:12px;color:var(--muted)}
@media(max-width:680px){body{padding:10px;place-items:start center}.head{grid-template-columns:minmax(0,1fr);gap:14px;padding:20px}.secure{justify-self:start}.content{padding:20px}.platforms{grid-template-columns:repeat(2,minmax(0,1fr))}.platform:last-child{grid-column:span 2}.apps{grid-template-columns:minmax(0,1fr);gap:10px;margin-top:20px}.app{padding:16px}.hint{min-height:0}.logo{border-radius:16px}}
@media(max-width:360px){.brand{align-items:flex-start;gap:10px}.logo{width:52px}.head,.content{padding:16px}.platform{min-height:74px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
</style></head><body><main><section class="shell">
<header class="head"><div class="brand"><img class="logo" src="/app/logo" alt="Логотип ByMeVPN" width="1254" height="1254"><div class="display"><h1>Подключение ByMeVPN</h1><p class="lead">Выберите устройство и приложение. Подписка добавится автоматически, если приложение поддерживает импорт.</p></div></div><span class="secure">Защищённая ссылка</span></header>
<div class="content"><h2 class="section-title">Ваше устройство</h2><div class="platforms" id="platforms" aria-label="Выбор устройства"></div><div class="apps" id="apps"></div><p class="status" id="status" role="status" aria-live="polite"></p><p class="note">Если импорт не открылся, скопируйте ссылку и добавьте её в разделе подписок. Время действия страницы ограничено: откройте подключение заново из бота, если ссылка истекла.</p>
<noscript><p class="noscript">Чтобы выбрать приложение и передать подписку автоматически, включите JavaScript в браузере. Для Android можно установить <a href="https://github.com/Brof14/ByMeVPN-app/releases/download/v1.0.12/ByMeVPN-v1.0.12.apk">ByMeVPN</a>, затем повторно открыть подключение из бота.</p></noscript>
</div></section></main>
<script>
(function(){const sub=__SUB__;const apps=JSON.parse(__APPS__);const platforms={android:"Android",ios:"iPhone / iPad",windows:"Windows",macos:"macOS",linux:"Linux"};
const platformIcons={android:"android.png",ios:"apple.png",windows:"windows.png",macos:"apple.png",linux:"linux.png"};
const appIcons={bymevpn:"/app/logo",happ:"/app/assets/happ.png",incy:"/app/assets/incy.png"};
const marks={v2rayn:'<svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="m5 9 11 16L27 9M9 9l7 10 7-10" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',koala:'<svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><circle cx="9" cy="11" r="4" stroke="currentColor" stroke-width="2"/><circle cx="23" cy="11" r="4" stroke="currentColor" stroke-width="2"/><path d="M7 19c0-5 4-8 9-8s9 3 9 8-4 9-9 9-9-4-9-9Z" stroke="currentColor" stroke-width="2"/><path d="M13 19h.1m6 0h.1m-4 3 1 .5 1-.5" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>'};
const ua=navigator.userAgent;let current=/Android/i.test(ua)?"android":/iPhone|iPad/i.test(ua)?"ios":/Windows/i.test(ua)?"windows":/Macintosh/i.test(ua)?"macos":"linux";const os=document.getElementById("platforms"),root=document.getElementById("apps"),status=document.getElementById("status");
async function copy(){try{await navigator.clipboard.writeText(sub);return true}catch(_){window.prompt("Скопируйте ссылку",sub);return false}}
function makeImage(src,className){const img=document.createElement("img");img.className=className;img.src=src;img.alt="";img.decoding="async";img.loading="lazy";return img}
function render(){os.replaceChildren();Object.keys(platforms).forEach(k=>{const b=document.createElement("button");b.type="button";b.className="platform";b.setAttribute("aria-pressed",String(k===current));b.append(makeImage("/app/assets/"+platformIcons[k],"mark"));const label=document.createElement("span");label.className="platform-label";label.textContent=platforms[k];b.append(label);b.onclick=()=>{current=k;render()};os.appendChild(b)});root.replaceChildren();apps[current].forEach(a=>{const d=document.createElement("article");d.className="app";const head=document.createElement("div");head.className="app-head";const icon=document.createElement("span");icon.className="app-mark";if(appIcons[a.icon]){icon.append(makeImage(appIcons[a.icon],"app-logo"))}else{const vector=document.createElement("span");vector.className="app-logo app-logo-vector";vector.innerHTML=marks[a.icon];icon.append(vector)}const h=document.createElement("h2");h.textContent=a.name;head.append(icon,h);const hint=document.createElement("p");hint.className="hint";hint.textContent=a.hint;const actions=document.createElement("div");actions.className="actions";if(!a.manual){const action=document.createElement("a");action.className="button";action.textContent="Добавить в "+a.name;action.href=a.deep;actions.append(action)}const fallback=document.createElement("button");fallback.type="button";fallback.className="button copy";fallback.textContent=a.manual?"Скопировать ссылку":"Если не открылось — скопировать ссылку";fallback.onclick=async()=>{await copy();status.textContent="Ссылка скопирована. Откройте приложение и добавьте подписку."};const download=document.createElement("a");download.className="button secondary";download.href=a.download;download.target="_blank";download.rel="noopener noreferrer";download.textContent="Установить "+a.name;actions.append(fallback,download);d.append(head,hint,actions);root.appendChild(d)})}
render();})();
</script></body></html>'''
    return template.replace("__SUB__", sub).replace("__APPS__", apps_json)
