"""YooKassa checkout creation is serialized and idempotent across workers."""
import os
import tempfile
import unittest

import database


class CheckoutLeaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = database.DB_FILE
        await database.close_db()
        database.DB_FILE = os.path.join(self.tmp.name, "checkout.db")
        await database.init_db()
        await database.ensure_user(445566)

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.saved
        self.tmp.cleanup()

    async def test_same_order_retries_provider_key_without_parallel_invoice(self):
        fingerprint = "a" * 64
        first = await database.claim_yookassa_checkout_attempt(445566, fingerprint)
        self.assertTrue(first["token"])
        self.assertEqual(len(first["idempotency_key"]), 64)
        self.assertIsNone(await database.claim_yookassa_checkout_attempt(445566, fingerprint))
        await database.release_yookassa_checkout_attempt(445566, first["token"])
        second = await database.claim_yookassa_checkout_attempt(445566, fingerprint)
        self.assertEqual(second["idempotency_key"], first["idempotency_key"])
        self.assertNotEqual(second["token"], first["token"])
        await database.release_yookassa_checkout_attempt(445566, first["token"])
        self.assertIsNone(await database.claim_yookassa_checkout_attempt(445566, fingerprint))
        await database.release_yookassa_checkout_attempt(445566, second["token"])

    async def test_changed_plan_receives_another_key(self):
        first = await database.claim_yookassa_checkout_attempt(445566, "a" * 64)
        await database.release_yookassa_checkout_attempt(445566, first["token"])
        second = await database.claim_yookassa_checkout_attempt(445566, "b" * 64)
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])
        await database.release_yookassa_checkout_attempt(445566, second["token"])

    async def test_malformed_fingerprint_fails_without_state_change(self):
        with self.assertRaises(ValueError):
            await database.claim_yookassa_checkout_attempt(445566, "wrong")


if __name__ == "__main__":
    unittest.main()
