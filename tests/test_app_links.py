"""Tests for the Telegram → ByMeVPN app handover links."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from app_links import (
    APP_LOGIN_CONTENT_SECURITY_POLICY,
    build_app_login_url,
    build_app_redirect_url,
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

    def test_connection_page_has_os_marks_and_manual_copy_fallback(self):
        page = render_connection_choice_page("https://sub.example/abc")

        for label in ("🤖 Android", "🍎 iPhone / iPad", "🪟 Windows", "🍏 macOS", "🐧 Linux"):
            self.assertIn(label, page)
        self.assertIn("Скопировать ссылку для импорта", page)
        self.assertIn("Ссылка скопирована", page)


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
