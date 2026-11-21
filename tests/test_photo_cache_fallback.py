import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import FSInputFile


class PhotoCacheFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_cached_file_id_retries_bundled_image(self):
        from utils import _send_photo_with_cache_fallback

        photo = "/app/assets/sections/bymevpn.png"
        bot = SimpleNamespace(
            token="12345:test-token",
            send_photo=AsyncMock(side_effect=[RuntimeError("wrong file identifier"), SimpleNamespace(photo=[])]),
        )
        with patch("utils._photo_source", new=AsyncMock(return_value=("stale-file-id", "12345:bymevpn.png"))), \
             patch("utils._remember_photo_file_id", new=AsyncMock()):
            await _send_photo_with_cache_fallback(bot, 123, photo, "Тарифы")

        self.assertEqual(bot.send_photo.await_count, 2)
        fallback = bot.send_photo.await_args_list[1].kwargs["photo"]
        self.assertIsInstance(fallback, FSInputFile)
        self.assertEqual(fallback.path, photo)


if __name__ == "__main__":
    unittest.main()
