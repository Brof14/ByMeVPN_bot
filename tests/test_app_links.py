"""Tests for the Telegram → ByMeVPN app handover links."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from app_links import (
    build_app_login_url,
    build_app_redirect_url,
    render_app_open_page,
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
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["sub"], ["https://sub.example/abc"])
        self.assertEqual(query["name"], ["user"])

    def test_empty_login_url_opens_the_app_without_parameters(self):
        self.assertEqual(build_app_login_url(), "bymevpn://login")
        self.assertTrue(build_app_redirect_url().endswith("/app/login"))


class TestAppOpenPage(unittest.TestCase):
    def test_page_waits_for_user_to_open_app_button(self):
        page = render_app_open_page("bymevpn://login?sub=https%3A%2F%2Fsub.example%2Fabc")

        self.assertNotIn("window.location.href = target", page)
        self.assertIn('id="open-app"', page)
        self.assertIn("Открыть ByMeVPN", page)
        self.assertIn('src="/app/logo"', page)
        self.assertIn("intent://login", page)

    def test_untrusted_values_cannot_break_out_of_script(self):
        page = render_app_open_page(
            'bymevpn://login?name=</script><script>alert(1)</script>'
        )

        self.assertNotIn("</script><script>alert(1)</script>", page)
        self.assertEqual(page.count("</script>"), 1)


class TestAppOpenKeyboard(unittest.TestCase):
    def test_button_uses_http_handoff_without_copy_link(self):
        markup = app_open_kb("https://example.test/app/login?sub=1")

        self.assertEqual(markup.inline_keyboard[0][0].url, "https://example.test/app/login?sub=1")
        self.assertEqual(len(markup.inline_keyboard), 1)


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
        self.assertIn("Нажмите кнопку ниже", text)
        self.assertTrue(open_button.url.startswith("https://bymevpn.duckdns.org/app/login?"))
        self.assertNotIn("bymevpn://", text)
        self.assertNotIn("http://", text)
        self.assertEqual(len(markup.inline_keyboard), 1)


if __name__ == "__main__":
    unittest.main()
