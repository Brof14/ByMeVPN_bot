"""Opt-in churn feedback must be deduplicated and never store raw comments."""
import os
import tempfile
import unittest

import database


class ChurnFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = database.DB_FILE
        await database.close_db()
        database.DB_FILE = os.path.join(self.tmp.name, "churn.db")
        await database.init_db()
        await database.ensure_user(9911)

    async def asyncTearDown(self):
        await database.close_db()
        database.DB_FILE = self.old
        self.tmp.cleanup()

    async def test_only_known_reason_and_one_response(self):
        self.assertTrue(await database.save_churn_feedback(9911, "connection"))
        self.assertFalse(await database.save_churn_feedback(9911, "connection"))
        self.assertEqual(await database.get_churn_feedback_summary(), {"connection": 1})
        with self.assertRaises(ValueError):
            await database.save_churn_feedback(9911, "vpn_token=secret")


if __name__ == "__main__":
    unittest.main()
