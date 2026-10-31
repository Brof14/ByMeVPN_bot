"""Helpers for opening the ByMeVPN mobile app from Telegram.

Telegram URL buttons accept HTTP(S) or ``tg://`` URLs, so the bot uses an
HTTPS handoff page. The page presents a user-initiated app-open button, which
avoids browsers blocking an automatic custom-scheme redirect.
"""
from __future__ import annotations

import html
import json
from urllib.parse import urlencode, urlsplit

from config import APP_PUBLIC_URL

APP_SCHEME = "bymevpn"

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
    """Build the public HTTPS/HTTP page that opens the app with one tap."""
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
  <img class="logo" src="/app/logo" alt="Логотип ByMeVPN">
  <h1>Открываем приложение…</h1>
  <p>Нажмите кнопку ниже — приложение откроется и подключит подписку.</p>
  <a id="open-app" class="button" href="{escaped_link}">Открыть ByMeVPN</a>
  <p class="hint">Если приложение не установлено, установите его и откройте эту ссылку снова.</p>
</main>
<script>
(function () {{
  var deepLink = {js_link};
  var target = /Android/i.test(navigator.userAgent) ? {js_intent} : deepLink;
  var button = document.getElementById("open-app");
  if (button) button.href = target;
}})();
</script>
</body>
</html>"""
