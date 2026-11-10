"""Configuration boundary tests for panel inbound selection."""
import unittest

from config import _parse_xui_inbound_ids


class TestXuiInboundConfig(unittest.TestCase):
    def test_default_preserves_the_current_production_inbounds(self):
        self.assertEqual(_parse_xui_inbound_ids(None), [13, 14, 40])
        self.assertEqual(_parse_xui_inbound_ids(""), [13, 14, 40])

    def test_operator_can_configure_a_unique_positive_inbound_set(self):
        self.assertEqual(_parse_xui_inbound_ids("2, 5,40"), [2, 5, 40])

    def test_invalid_inbound_configuration_fails_closed(self):
        for value in ("abc", "0,2", "-1", "2,2", ",,"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _parse_xui_inbound_ids(value)


if __name__ == "__main__":
    unittest.main()
