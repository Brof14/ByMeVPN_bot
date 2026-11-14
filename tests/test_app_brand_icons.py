"""App chooser must never point to missing Hiddify or sing-box logos."""
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi.testclient import TestClient

from webhook import app
from app_links import render_connection_choice_page


class ConnectionIconTests(unittest.TestCase):
    def test_official_app_marks_exist_and_are_valid_svg(self):
        root = Path(__file__).resolve().parents[1] / "static" / "app" / "assets"
        for icon in ("hiddify", "singbox"):
            with self.subTest(icon=icon):
                file = root / f"{icon}.svg"
                self.assertTrue(file.is_file())
                content = file.read_text(encoding="utf-8")
                self.assertTrue(ET.fromstring(content).tag.endswith("svg"))
                self.assertIn("<svg", content)
                self.assertNotIn("<script", content.lower())
                self.assertNotIn("javascript:", content.lower())

    def test_icons_are_served_with_svg_media_type(self):
        client = TestClient(app)
        for icon in ("hiddify", "singbox"):
            with self.subTest(icon=icon):
                response = client.get(f"/app/assets/{icon}.svg")
                self.assertEqual(response.status_code, 200)
                self.assertIn("image/svg+xml", response.headers["content-type"])

    def test_connection_html_selects_bundled_svg_icons(self):
        page = render_connection_choice_page("https://example.test/sub/test")
        self.assertIn('app.icon==="hiddify"||app.icon==="singbox"', page)
        self.assertIn('?".svg":".png"', page)
