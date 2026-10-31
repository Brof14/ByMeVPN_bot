"""Regression tests for the one-tap Telegram login session exchange."""
import unittest

import api_router


class TestTelegramSessionFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._created_tokens = []

    async def asyncTearDown(self):
        for token in self._created_tokens:
            api_router._TELEGRAM_SESSIONS.pop(token, None)

    async def _create_session(self):
        created = await api_router.create_telegram_session()
        self._created_tokens.append(created.session_token)
        return created

    async def test_confirmation_updates_the_session_the_app_polls(self):
        created = await self._create_session()
        self.assertEqual(
            api_router._TELEGRAM_SESSIONS[created.session_token]["status"],
            "pending",
        )

        confirmed = api_router.confirm_telegram_session(
            created.session_token,
            user_id=424242,
            username="probe",
            full_name="Probe User",
        )

        self.assertTrue(confirmed)
        session = api_router._TELEGRAM_SESSIONS[created.session_token]
        self.assertEqual(session["status"], "authenticated")
        self.assertEqual(session["user_id"], 424242)
        self.assertEqual(session["full_name"], "Probe User")

    async def test_confirmation_does_not_leave_a_detached_duplicate(self):
        created = await self._create_session()

        api_router.confirm_telegram_session(created.session_token, user_id=7)

        unprefixed = created.session_token.removeprefix("auth_")
        self.assertNotIn(unprefixed, api_router._TELEGRAM_SESSIONS)
        self.assertEqual(
            api_router._TELEGRAM_SESSIONS[created.session_token]["status"],
            "authenticated",
        )
