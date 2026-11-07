"""
Referral integrity regression tests.

Invariant under test: ONE qualifying payment → AT MOST ONE referral day reward.
  - legacy duplicate add_referral_earning remains idempotent;
  - award_referral_for_payment creates one days-only reward record;
  - intro-trial payments (1 ₽) never produce a reward;
  - renewals (second payment of the same referred user) produce no second reward;
  - duplicate provider payments (same provider_payment_id) are recorded once.

Run:  python3 tests/test_referral_integrity.py
"""
import os
import sys
import asyncio
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

TEST_DB_PATH = "/tmp/test_referral_vpnbot.db"
os.environ["DB_FILE"] = TEST_DB_PATH

import database  # noqa: E402
from referral_system_new import award_referral_for_payment  # noqa: E402
from referral_system_new import (  # noqa: E402
    retry_pending_signup_bonuses,
    process_pending_referral_payment_rewards,
)


class TestReferralIntegrity(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        for ext in ["", "-wal", "-shm"]:
            if os.path.exists(TEST_DB_PATH + ext):
                os.remove(TEST_DB_PATH + ext)
        database.DB_FILE = TEST_DB_PATH
        database._db_pool = None
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        for ext in ["", "-wal", "-shm"]:
            if os.path.exists(TEST_DB_PATH + ext):
                os.remove(TEST_DB_PATH + ext)

    async def _seed_pair(self, referrer_id: int = 111, referred_id: int = 222):
        await database.ensure_user(referrer_id)
        await database.ensure_user(referred_id)
        await database.set_referrer(referred_id, referrer_id)

    async def _earnings_count(self) -> int:
        db = await database.get_db()
        cur = await db.execute("SELECT COUNT(*) FROM referral_earnings")
        row = await cur.fetchone()
        return row[0]

    async def _payment_rewards_count(self) -> int:
        db = await database.get_db()
        cur = await db.execute("SELECT COUNT(*) FROM referral_payment_rewards")
        return (await cur.fetchone())[0]

    # ------------------------------------------------------------------
    async def test_01_duplicate_earning_insert_is_blocked(self):
        """Two concurrent-looking inserts for the same pair → one row, one credit."""
        await self._seed_pair()
        first = await database.add_referral_earning(111, 222, 50, "pay-1")
        second = await database.add_referral_earning(111, 222, 50, "pay-1-duplicate-webhook")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(await self._earnings_count(), 1)
        balance = await database.get_referral_balance(111)
        self.assertEqual(balance["balance"], 50, "balance must be credited exactly once")

    async def test_02_award_helper_idempotent(self):
        """award_referral_for_payment twice (duplicate webhook) → single reward."""
        await self._seed_pair()
        first = await award_referral_for_payment(222, "yk-payment-id-1", bot=None)
        second = await award_referral_for_payment(222, "yk-payment-id-1", bot=None)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(await self._payment_rewards_count(), 1)
        self.assertEqual(await self._earnings_count(), 0, "new referral rewards must not create a cash ledger")
        events = await database.get_referral_events(111)
        self.assertTrue(any(event["event_type"] == "payment_bonus" and event["days_awarded"] == 30 for event in events))

    async def test_03_trial_payment_never_awards(self):
        """Intro-trial (1 ₽) is not a paid conversion → no reward, no row."""
        await self._seed_pair()
        result = await award_referral_for_payment(222, "trial-payment-id", is_trial=True, bot=None)
        self.assertFalse(result)
        self.assertEqual(await self._payment_rewards_count(), 0)

    async def test_04_renewal_yields_no_second_reward(self):
        """Second (renewal) payment of the same referred user → no second reward."""
        await self._seed_pair()
        await award_referral_for_payment(222, "first-payment", bot=None)
        await award_referral_for_payment(222, "renewal-payment", bot=None)
        self.assertEqual(await self._payment_rewards_count(), 1)

    async def test_05_no_referrer_no_reward(self):
        """Payment by a user without a referrer → nothing happens."""
        await database.ensure_user(333)
        result = await award_referral_for_payment(333, "some-payment", bot=None)
        self.assertFalse(result)
        self.assertEqual(await self._earnings_count(), 0)

    async def test_06_duplicate_provider_payment_recorded_once(self):
        """Same (provider, provider_payment_id) twice → one payments row."""
        await database.ensure_user(444)
        args = dict(
            user_id=444, amount=89, currency="RUB", method="yookassa", days=30,
            payload="dup-payment-id", status="success", tariff="t", devices=2,
            provider="yookassa", provider_payment_id="dup-payment-id",
        )
        is_new_1, pay_id_1 = await database.record_payment_idempotent(**args)
        is_new_2, pay_id_2 = await database.record_payment_idempotent(**args)
        self.assertTrue(is_new_1)
        self.assertFalse(is_new_2)
        db = await database.get_db()
        cur = await db.execute(
            "SELECT COUNT(*) FROM payments WHERE provider='yookassa' AND provider_payment_id='dup-payment-id'"
        )
        self.assertEqual((await cur.fetchone())[0], 1)
        self.assertEqual(pay_id_1, pay_id_2, "duplicate must map to the original payments row")

    async def test_07_failed_signup_bonus_is_retryable_and_not_duplicated(self):
        """A panel outage leaves the 15-day signup reward pending for retry."""
        from unittest.mock import AsyncMock, patch

        await self._seed_pair(referrer_id=711, referred_id=722)
        delivery = AsyncMock(side_effect=[False, True])
        with patch("subscription.deliver_key", new=delivery):
            pending_after_failure = await retry_pending_signup_bonuses(None, 711)
            self.assertEqual(pending_after_failure, 1)
            pending_after_retry = await retry_pending_signup_bonuses(None, 711)
            self.assertEqual(pending_after_retry, 0)
            pending_after_success = await retry_pending_signup_bonuses(None, 711)
            self.assertEqual(pending_after_success, 0)

        self.assertEqual(delivery.await_count, 2)
        self.assertTrue(delivery.await_args.kwargs["is_paid"])
        self.assertEqual(delivery.await_args.kwargs["amount"], 0)
        self.assertEqual(delivery.await_args.kwargs["payload"], "ref_signup_711_722")
        events = await database.get_referral_events(711)
        signup_rewards = [e for e in events if e["referred_id"] == 722 and e["event_type"] == "registration_bonus"]
        self.assertEqual(len(signup_rewards), 1)
        self.assertEqual(signup_rewards[0]["days_awarded"], 15)

    async def test_08_paid_referral_bonus_is_automatic_and_retryable(self):
        from unittest.mock import AsyncMock, patch

        await self._seed_pair(referrer_id=911, referred_id=922)
        await database.record_referral_payment_reward(911, 922, "payment-922")
        delivery = AsyncMock(side_effect=[False, True])
        bot = type("BotStub", (), {"send_message": AsyncMock()})()
        with patch("subscription.deliver_key", new=delivery):
            self.assertEqual(await process_pending_referral_payment_rewards(bot), 0)
            self.assertEqual(await process_pending_referral_payment_rewards(bot), 1)
            self.assertEqual(await process_pending_referral_payment_rewards(bot), 0)

        self.assertEqual(delivery.await_count, 2)
        self.assertEqual(delivery.await_args.kwargs["days"], 30)
        self.assertTrue(delivery.await_args.kwargs["is_paid"])
        self.assertEqual(delivery.await_args.kwargs["amount"], 0)
        self.assertEqual(delivery.await_args.kwargs["payload"], "ref_bonus_911_922")
        self.assertFalse(await database.get_pending_referral_payment_rewards())

    async def test_09_retry_repairs_missing_payment_event(self):
        """A crash after recording a reward must not permanently hide a conversion."""
        from unittest.mock import AsyncMock, patch
        await self._seed_pair(referrer_id=1011, referred_id=1022)
        with patch("database.add_referral_event", new=AsyncMock(side_effect=RuntimeError("db unavailable"))):
            self.assertFalse(await award_referral_for_payment(1022, "payment-1022", bot=None))
        await award_referral_for_payment(1022, "payment-1022", bot=None)
        await award_referral_for_payment(1022, "payment-1022", bot=None)
        events = await database.get_referral_events(1011)
        self.assertEqual(len([e for e in events if e["referred_id"] == 1022 and e["event_type"] == "payment_bonus"]), 1)

    async def test_10_second_inviter_does_not_create_a_phantom_referral(self):
        for uid in (1101, 1102, 1103):
            await database.ensure_user(uid)
        await database.set_referrer(1103, 1101)
        await database.set_referrer(1103, 1102)
        self.assertEqual(await database.get_referrer(1103), 1101)
        self.assertEqual(await database.count_referrals(1101), 1)
        self.assertEqual(await database.count_referrals(1102), 0)
        self.assertEqual(await database.get_referrals_missing_signup_bonus(1102), [])

    async def test_11_historical_phantom_does_not_block_retry_queue(self):
        for uid in (1201, 1202, 1203):
            await database.ensure_user(uid)
        await database.set_referrer(1203, 1201)
        db = await database.get_db()
        await db.execute("INSERT INTO referrals(referrer_id, referred_id) VALUES(?,?)", (1202, 1203))
        await db.commit()
        self.assertEqual(await database.get_referrals_missing_signup_bonus(1202), [])
        self.assertEqual(await database.get_referrals_missing_signup_bonus(1201), [1203])
        self.assertEqual((await database.get_referral_stats_enhanced(1202))["total_registrations"], 0)
        self.assertEqual(await database.get_referred_users_list(1202), [])
        from growth_metrics import referral_growth_snapshot
        self.assertEqual((await referral_growth_snapshot(days=30))["invited"], 1)

    async def test_12_private_invite_matches_legacy_lowercase_application(self):
        await database.create_ad_application(
            user_id=1301, channel_id=None, channel_username="", subscriber_count=20,
            bonus_months=2, channel_ref="+abcdef012345", status="channel_pending",
        )
        self.assertTrue(await database.channel_already_used(None, "+AbCdEf012345"))

    async def test_13_concurrent_signup_reward_conflict_is_reported_as_success(self):
        from unittest.mock import AsyncMock, patch
        from referral_system_new import award_referral_signup_bonus
        with (
            patch("database.get_referrer", new=AsyncMock(return_value=1401)),
            patch("database.has_referral_event", new=AsyncMock(side_effect=[False, True])),
            patch("database.get_user_keys", new=AsyncMock(return_value=[])),
            patch("database.add_referral_event", new=AsyncMock(return_value=0)),
            patch("subscription.deliver_key", new=AsyncMock(return_value=True)),
        ):
            self.assertTrue(await award_referral_signup_bonus(None, 1401, 1402))


if __name__ == "__main__":
    unittest.main(verbosity=2)
