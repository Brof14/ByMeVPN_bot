"""
Referral integrity regression tests.

Invariant under test: ONE qualifying payment → AT MOST ONE referral reward.
  - duplicate add_referral_earning (concurrent webhooks) → single row;
  - award_referral_for_payment is idempotent (UNIQUE + return code);
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
        self.assertEqual(await self._earnings_count(), 1)

    async def test_03_trial_payment_never_awards(self):
        """Intro-trial (1 ₽) is not a paid conversion → no reward, no row."""
        await self._seed_pair()
        result = await award_referral_for_payment(222, "trial-payment-id", is_trial=True, bot=None)
        self.assertFalse(result)
        self.assertEqual(await self._earnings_count(), 0)

    async def test_04_renewal_yields_no_second_reward(self):
        """Second (renewal) payment of the same referred user → no second reward."""
        await self._seed_pair()
        await award_referral_for_payment(222, "first-payment", bot=None)
        await award_referral_for_payment(222, "renewal-payment", bot=None)
        self.assertEqual(await self._earnings_count(), 1)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
