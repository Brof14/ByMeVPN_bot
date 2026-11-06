import unittest

from bot_instance import normalize_bot_instance_mode, runs_primary_services


class TestBotInstanceMode(unittest.TestCase):
    def test_default_mode_is_primary(self):
        self.assertEqual(normalize_bot_instance_mode(None), "primary")
        self.assertTrue(runs_primary_services("primary"))

    def test_polling_only_mode_disables_singleton_workers(self):
        self.assertEqual(normalize_bot_instance_mode(" POLLING-ONLY "), "polling-only")
        self.assertFalse(runs_primary_services("polling-only"))

    def test_unknown_mode_fails_closed(self):
        with self.assertRaises(ValueError):
            normalize_bot_instance_mode("secondary-but-full-workers")


if __name__ == "__main__":
    unittest.main()
