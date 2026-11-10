"""Tests for the Telegram → ByMeVPN app handover links."""
import time
import unittest
import os
import json
import re
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from app_links import (
    APP_LOGIN_CONTENT_SECURITY_POLICY,
    build_app_login_url,
    build_app_redirect_url,
    build_connect_ticket,
    read_connect_ticket,
    CONNECT_TICKET_TTL_SECONDS,
    render_app_open_page,
    render_connection_choice_page,
)
from keyboards import app_open_kb


class TestAppLinkBuilders(unittest.TestCase):
    def test_login_url_keeps_subscription_and_user_name(self):
        url = build_app_login_url("https://sub.example/abc?token=1&x=2", "@user name")

        self.assertTrue(url.startswith("bymevpn://login?"))
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["sub"], ["https://sub.example/abc?token=1&x=2"])
        self.assertEqual(query["name"], ["@user name"])

    def test_redirect_url_targets_the_public_handover_page(self):
        url = build_app_redirect_url("https://sub.example/abc", "user")

        self.assertIn("/app/login?", url)
        self.assertEqual(urlsplit(url).scheme, "https")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["sub"], ["https://sub.example/abc"])
        self.assertEqual(query["name"], ["user"])

    def test_unsafe_public_url_cannot_carry_subscription_credentials(self):
        with patch("app_links.APP_PUBLIC_URL", "http://public.example:8080"):
            with self.assertRaises(ValueError):
                build_app_redirect_url("https://sub.example/private-token", "user")
            self.assertNotIn("sub=", build_app_redirect_url())

    def test_empty_login_url_opens_the_app_without_parameters(self):
        self.assertEqual(build_app_login_url(), "bymevpn://login")
        self.assertTrue(build_app_redirect_url().endswith("/app/login"))


class TestAppOpenPage(unittest.TestCase):
    def test_page_contains_automatic_redirect_and_manual_button(self):
        page = render_app_open_page("bymevpn://login?sub=https%3A%2F%2Fsub.example%2Fabc")

        self.assertIn("window.location.href = target", page)
        self.assertIn('id="open-app"', page)
        self.assertIn("Открыть ByMeVPN", page)
        self.assertIn("intent://login", page)

    def test_page_uses_same_origin_brand_mark(self):
        page = render_app_open_page("bymevpn://login")

        self.assertIn('src="/app/logo"', page)
        self.assertIn('alt="Логотип ByMeVPN"', page)

    def test_content_security_policy_allows_same_origin_logo(self):
        self.assertIn("default-src 'none'", APP_LOGIN_CONTENT_SECURITY_POLICY)
        self.assertIn("img-src 'self'", APP_LOGIN_CONTENT_SECURITY_POLICY)

    def test_brand_mark_asset_is_committed(self):
        from pathlib import Path

        logo = Path(__file__).resolve().parents[1] / "static" / "bymevpn-mark.png"
        self.assertTrue(logo.is_file())
        self.assertGreater(logo.stat().st_size, 1024)
        self.assertEqual(logo.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        optimized = logo.parent / "app" / "assets" / "bymevpn-logo.png"
        self.assertTrue(optimized.is_file())
        self.assertLess(optimized.stat().st_size, logo.stat().st_size)
        self.assertEqual(struct.unpack(">II", optimized.read_bytes()[16:24]),
                         struct.unpack(">II", logo.read_bytes()[16:24]))

    def test_untrusted_values_cannot_break_out_of_script(self):
        page = render_app_open_page(
            'bymevpn://login?name=</script><script>alert(1)</script>'
        )

        self.assertNotIn("</script><script>alert(1)</script>", page)
        self.assertEqual(page.count("</script>"), 1)

    def test_connection_page_uses_import_links_not_encoded_open_only_urls(self):
        page = render_connection_choice_page("https://sub.example/abc")

        self.assertIn("happ://add/https://sub.example/abc", page)
        self.assertIn("incy://import/https://sub.example/abc", page)
        self.assertNotIn("happ://add/https%3A%2F%2F", page)
        self.assertIn("Koala Clash", page)
        self.assertIn("v2rayN", page)
        self.assertIn("koala-clash/releases/latest", page)
        self.assertIn("play.google.com/store/apps/details?id=llc.itdev.incy", page)
        self.assertIn("github.com/Happ-proxy/happ-android/releases/latest", page)

    def test_download_and_platform_mapping_preserves_existing_clients(self):
        page = render_connection_choice_page("https://sub.example/abc")
        encoded = re.search(r'const apps=JSON.parse\(("(?:\\.|[^"\\])*")\)', page)
        self.assertIsNotNone(encoded)
        apps = json.loads(json.loads(encoded.group(1)))
        self.assertEqual([a["name"] for a in apps["android"]], ["ByMeVPN", "Happ", "INCY", "v2rayTun", "v2rayNG", "Hiddify", "sing-box"])
        self.assertEqual(apps["android"][0]["download"],
                         "https://github.com/Brof14/ByMeVPN-app/releases/download/v1.0.13/ByMeVPN-v1.0.13.apk")
        self.assertEqual([a["name"] for a in apps["ios"]], ["Happ", "INCY", "v2rayTun", "Hiddify", "sing-box"])
        for platform in ("windows", "macos", "linux"):
            self.assertIn("v2rayN", [a["name"] for a in apps[platform]])
            self.assertIn("Koala Clash", [a["name"] for a in apps[platform]])
            self.assertTrue(all("ByMeVPN-v1.0.13.apk" not in a["download"] for a in apps[platform]))

    def test_connection_page_has_os_marks_and_manual_copy_fallback(self):
        page = render_connection_choice_page("https://sub.example/abc")

        for label in ("Android", "iOS", "Windows", "macOS", "Linux"):
            self.assertIn(label, page)
        self.assertNotIn("🤖", page)
        self.assertIn('id="platforms"', page)
        self.assertIn("overflow-x:clip", page)
        self.assertIn("prefers-reduced-motion", page)
        self.assertIn("Скопировать ссылку", page)
        self.assertNotIn("Если не открылось — скопировать", page)
        self.assertIn('src="/app/assets/bymevpn-logo.png"', page)
        self.assertIn("Выберите приложение и подключитесь", page)
        self.assertIn("github.com/Brof14/ByMeVPN-app/releases/download/v1.0.13/ByMeVPN-v1.0.13.apk", page)
        self.assertIn("Рекомендуем", page)
        self.assertIn('<details id="alternatives" open>', page)
        self.assertIn('id="copy-subscription"', page)
        self.assertEqual(page.count("Скопировать ссылку"), 1)
        self.assertNotIn("Если не открылось — скопировать ссылку", page)
        self.assertIn("https://play.google.com/store/apps/details?id=llc.itdev.incy", page)
        for symbol in ("android", "apple", "windows", "linux"):
            self.assertIn(f'id="os-{symbol}"', page)
        self.assertIn('id="external-download"', page)
        self.assertIn("viewport-fit=cover", page)

    def test_connection_page_escapes_script_endings_and_template_markers(self):
        page = render_connection_choice_page("https://sub.example/__APPS__?x=</script>")
        self.assertNotIn("</script>\u003cscript>", page)
        self.assertEqual(page.count("</script>"), 1)
        self.assertIn("__APPS__", page)  # appears only inside the safely encoded URL
        self.assertIn("\\u003c/script\\u003e", page)


class TestConnectionPageHttp(unittest.TestCase):
    def test_extended_lifetime_and_legacy_ticket(self):
        import base64
        from app_links import _connect_aead
        with patch.dict(os.environ, {"APP_HANDOFF_SECRET": "isolated-test-secret"}):
            ticket = build_connect_ticket("https://sub.example/demo", now=1_000_000)
            self.assertEqual(read_connect_ticket(ticket, now=1_000_000 + 86400)["subscription_url"], "https://sub.example/demo")
            with self.assertRaises(ValueError):
                read_connect_ticket(ticket, now=1_000_001 + CONNECT_TICKET_TTL_SECONDS)
            payload = json.dumps({"sub": "https://sub.example/demo", "exp": 1_000_000 + 900}).encode()
            nonce = os.urandom(12)
            legacy = base64.urlsafe_b64encode(nonce + _connect_aead().encrypt(nonce, payload, b"bymevpn-connect-ticket-v1")).decode().rstrip("=")
            self.assertEqual(read_connect_ticket(legacy, now=1_000_000 + 86400)["subscription_url"], "https://sub.example/demo")
            with self.assertRaises(ValueError):
                read_connect_ticket(legacy, now=1_000_001 + CONNECT_TICKET_TTL_SECONDS)

    def test_verified_import_links_escape_query_and_warning(self):
        page = render_connection_choice_page("https://sub.example/abc?x=1&y=2")
        encoded = re.search(r'const apps=JSON.parse\(("(?:\\.|[^"\\])*")\)', page)
        apps = json.loads(json.loads(encoded.group(1)))
        android = {a["name"]: a for a in apps["android"]}
        self.assertEqual(parse_qs(urlsplit(android["v2rayNG"]["deep"]).query)["url"], ["https://sub.example/abc?x=1&y=2"])
        self.assertEqual(parse_qs(urlsplit(android["Hiddify"]["deep"]).query)["url"], ["https://sub.example/abc?x=1&y=2"])
        self.assertIn("v2raytun://import/https://sub.example/abc?x=1&y=2", android["v2rayTun"]["deep"])
        self.assertTrue(android["sing-box"]["manual"])
        self.assertIn("JSON", android["sing-box"]["note"])

    def test_valid_and_expired_tickets_keep_existing_security_contract(self):
        from fastapi.testclient import TestClient
        from webhook import app

        with patch.dict(os.environ, {"APP_HANDOFF_SECRET": "isolated-test-secret"}):
            client = TestClient(app)
            ticket = build_connect_ticket("https://sub.example/preview")
            response = client.get("/connect", params={"ticket": ticket})
            self.assertEqual(response.status_code, 200)
            self.assertIn("Подключение VPN", response.text)
            self.assertIn("no-store", response.headers["cache-control"])
            self.assertIn("default-src 'none'", response.headers["content-security-policy"])
            expired = build_connect_ticket("https://sub.example/preview", now=int(time.time()) - CONNECT_TICKET_TTL_SECONDS - 1)
            self.assertEqual(client.get("/connect", params={"ticket": expired}).status_code, 400)
            self.assertEqual(client.get("/connect", params={"ticket": "invalid"}).status_code, 400)


class TestAppOpenKeyboard(unittest.TestCase):
    def test_long_deep_link_does_not_exceed_telegram_copy_limit(self):
        markup = app_open_kb("https://public.example/app/login", "bymevpn://login?sub=" + "x" * 400)
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://public.example/app/login")
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, "my_keys")

    def test_first_button_opens_app_and_copy_stays_as_fallback(self):
        markup = app_open_kb("http://example.test/app/login?sub=1", "bymevpn://login?sub=1")

        self.assertEqual(markup.inline_keyboard[0][0].url, "http://example.test/app/login?sub=1")
        self.assertEqual(
            markup.inline_keyboard[1][0].copy_text.text,
            "bymevpn://login?sub=1",
        )


class TestAppLoginHandler(unittest.IsolatedAsyncioTestCase):
    async def test_active_customer_gets_one_tap_open_button(self):
        from handlers.start import _handle_app_login

        message = SimpleNamespace(
            from_user=SimpleNamespace(username="customer"),
            answer=AsyncMock(),
        )
        key = {
            "expiry": int(time.time()) + 86400,
            "key": "https://sub.example/abc",
        }
        with (
            patch("handlers.start._user_state", new=AsyncMock(return_value="active")),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[key])),
        ):
            await _handle_app_login(message, 123, "Customer")

        text = message.answer.await_args.args[0]
        markup = message.answer.await_args.kwargs["reply_markup"]
        open_button = markup.inline_keyboard[0][0]
        self.assertIn("Открыть приложение", text)
        self.assertIn("/app/login?", open_button.url)
        self.assertTrue(open_button.url.startswith("http"))
        self.assertEqual(
            markup.inline_keyboard[1][0].copy_text.text.startswith("bymevpn://login?"),
            True,
        )

    async def test_insecure_public_handover_uses_copy_only(self):
        from handlers.start import _handle_app_login
        message = SimpleNamespace(from_user=SimpleNamespace(username="test"), answer=AsyncMock())
        key = {"expiry": int(time.time()) + 86400, "key": "https://sub.example/secret"}
        with (
            patch("handlers.start._user_state", new=AsyncMock(return_value="active")),
            patch("handlers.start.get_user_keys", new=AsyncMock(return_value=[key])),
            patch("app_links.APP_PUBLIC_URL", "http://public.example:8080"),
        ):
            await _handle_app_login(message, 123, "Test")
        text = message.answer.await_args.args[0]
        kb = message.answer.await_args.kwargs["reply_markup"]
        self.assertIn("Автоматический переход временно недоступен", text)
        self.assertFalse(any(b.url for row in kb.inline_keyboard for b in row))
        self.assertTrue(any(b.copy_text for row in kb.inline_keyboard for b in row))


if __name__ == "__main__":
    unittest.main()
