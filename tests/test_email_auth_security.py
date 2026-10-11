"""Regression tests for email-authentication abuse controls and account linking."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DB_FILE", "/tmp/test_email_auth_security.db")

import database


class TestEmailAuthSecurity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.close_db()
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "auth.db")
        self.db_file_patch = patch.object(database, "DB_FILE", self.db_path)
        self.db_file_patch.start()
        database._db_pool = None
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        self.db_file_patch.stop()
        self.tempdir.cleanup()

    async def test_codes_are_hashed_and_single_use(self):
        await database.ensure_user(111)
        now = int(__import__("time").time())
        await database.save_email_auth_code(111, "client@example.com", "123456", now + 300)
        db = await database.get_db()
        cur = await db.execute("SELECT code FROM email_auth WHERE user_id=?", (111,))
        stored = (await cur.fetchone())[0]
        self.assertTrue(stored.startswith("sha256:"))
        self.assertNotEqual(stored, "123456")
        self.assertTrue(await database.verify_email_auth_code(111, "123456"))
        self.assertFalse(await database.verify_email_auth_code(111, "123456"))

    async def test_five_failed_guesses_exhaust_the_code(self):
        await database.ensure_user(222)
        now = int(__import__("time").time())
        await database.save_email_auth_code(222, "client2@example.com", "654321", now + 300)
        for _ in range(5):
            self.assertFalse(await database.verify_email_auth_code(222, "000000"))
        self.assertFalse(await database.verify_email_auth_code(222, "654321"))

    async def test_email_requests_are_rate_limited_persistently(self):
        self.assertTrue(await database.allow_email_auth_request(333, "rate@example.com", now=1000))
        self.assertFalse(await database.allow_email_auth_request(333, "rate@example.com", now=1020))
        self.assertTrue(await database.allow_email_auth_request(333, "rate@example.com", now=1061))
        self.assertFalse(await database.allow_email_auth_request(444, "rate@example.com", now=1062))

    async def test_referral_bonus_reservation_is_idempotent(self):
        await database.ensure_user(901)
        await database.ensure_user(902)
        self.assertTrue(await database.reserve_referral_bonus(901, 902, "trial_bonus"))
        self.assertFalse(await database.reserve_referral_bonus(901, 902, "trial_bonus"))
        await database.finalize_referral_bonus(901, 902, success=False)
        self.assertTrue(await database.reserve_referral_bonus(901, 902, "trial_bonus"))
        await database.finalize_referral_bonus(901, 902, success=True)
        self.assertFalse(await database.reserve_referral_bonus(901, 902, "trial_bonus"))

    async def test_referrer_assignment_is_one_time(self):
        await database.ensure_user(777)
        self.assertTrue(await database.set_referrer(777, 888))
        self.assertFalse(await database.set_referrer(777, 888))
        self.assertFalse(await database.set_referrer(777, 999))
        db = await database.get_db()
        cur = await db.execute("SELECT referrer_id FROM users WHERE user_id=?", (777,))
        self.assertEqual((await cur.fetchone())[0], 888)
        cur = await db.execute("SELECT COUNT(*) FROM referrals WHERE referred_id=?", (777,))
        self.assertEqual((await cur.fetchone())[0], 1)

    async def test_account_link_moves_keys_and_referral_balance(self):
        await database.ensure_user(555)
        await database.ensure_user(666)
        db = await database.get_db()
        await db.execute("UPDATE users SET email=? WHERE user_id=?", ("owner@example.com", 555))
        await db.execute(
            "INSERT INTO keys(user_id,key,remark,uuid,short_id,days,limit_ip,created,expiry) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (555, "sub-url", "test", "uuid-value", "sub-id", 30, 2, 1000, 2000),
        )
        await db.execute(
            "INSERT INTO referral_balance(user_id,balance,total_earned) VALUES(?,?,?)",
            (555, 50, 50),
        )
        await db.commit()

        self.assertTrue(await database.link_telegram_to_user(666, "owner@example.com"))
        cur = await db.execute("SELECT user_id,email FROM users WHERE user_id=666")
        self.assertEqual(await cur.fetchone(), (666, "owner@example.com"))
        cur = await db.execute("SELECT user_id FROM keys WHERE remark='test'")
        self.assertEqual((await cur.fetchone())[0], 666)
        cur = await db.execute("SELECT user_id,balance FROM referral_balance")
        self.assertEqual(await cur.fetchone(), (666, 50))
        cur = await db.execute("SELECT 1 FROM users WHERE user_id=555")
        self.assertIsNone(await cur.fetchone())


    async def test_account_link_refuses_to_merge_into_an_account_with_its_own_key(self):
        await database.ensure_user(1001)
        await database.ensure_user(1002)
        db = await database.get_db()
        await db.execute("UPDATE users SET email=? WHERE user_id=?", ("legacy@example.com", 1001))
        await db.execute(
            "INSERT INTO keys(user_id,key,remark,uuid,short_id,days,limit_ip,created,expiry) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (1002, "target-key", "target", "target-uuid", "target-sub", 30, 2, 1000, 2000),
        )
        await db.commit()

        self.assertFalse(await database.link_telegram_to_user(1002, "legacy@example.com"))
        cur = await db.execute("SELECT user_id,email FROM users WHERE user_id=1001")
        self.assertEqual(await cur.fetchone(), (1001, "legacy@example.com"))
        cur = await db.execute("SELECT user_id,key FROM keys ORDER BY id")
        self.assertEqual(await cur.fetchall(), [(1002, "target-key")])


    async def test_referral_reward_and_balance_are_idempotent(self):
        await database.ensure_user(1201)
        await database.ensure_user(1202)

        self.assertTrue(await database.add_referral_earning(1201, 1202, 50, 987654))
        self.assertFalse(await database.add_referral_earning(1201, 1202, 50, 987654))

        db = await database.get_db()
        cur = await db.execute(
            "SELECT balance,total_earned FROM referral_balance WHERE user_id=?",
            (1201,),
        )
        self.assertEqual(await cur.fetchone(), (50, 50))
        cur = await db.execute(
            "SELECT COUNT(*) FROM referral_earnings WHERE referrer_id=? AND referred_id=?",
            (1201, 1202),
        )
        self.assertEqual((await cur.fetchone())[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
