import unittest
from types import SimpleNamespace

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageMedia, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, MessageEntity

from bot_style import ICON_IDS, PREMIUM_EMOJI_IDS, PremiumEmojiRequestMiddleware, premium_emoji_text


class PremiumEmojiMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_message_uses_custom_emoji_entities(self):
        source = (
            "<b>📱 Устройство</b>\n🔗 Xiaomi\n"
            "⚙️ Система: Android 15\n🕒 Последняя активность: 1 мин назад"
        )
        rendered = premium_emoji_text(source)
        for emoji_id in (ICON_IDS["devices"], ICON_IDS["connect"], ICON_IDS["settings"], ICON_IDS["history"]):
            self.assertIn(f'emoji-id="{emoji_id}"', rendered)
        self.assertEqual(rendered.count("<tg-emoji"), 4)

    async def test_request_middleware_converts_messages_and_button_labels(self):
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎁 Бонус", callback_data="promo")
        ]])
        method = SendMessage(chat_id=1, text="✅ Готово", reply_markup=markup)
        bot = SimpleNamespace(default=SimpleNamespace(parse_mode=ParseMode.HTML))
        captured = {}

        async def downstream(_bot, outgoing):
            captured["method"] = outgoing
            return "ok"

        result = await PremiumEmojiRequestMiddleware()(downstream, bot, method)
        self.assertEqual(result, "ok")
        rendered = captured["method"]
        self.assertIn("<tg-emoji", rendered.text)
        button = rendered.reply_markup.inline_keyboard[0][0]
        self.assertEqual(button.icon_custom_emoji_id, ICON_IDS["gift"])
        self.assertEqual(button.text, "Бонус")

    async def test_text_with_explicit_entities_is_left_untouched(self):
        method = SendMessage(chat_id=1, text="✅", entities=[MessageEntity(type="bold", offset=0, length=1)])
        bot = SimpleNamespace(default=SimpleNamespace(parse_mode=ParseMode.HTML))

        async def downstream(_bot, outgoing):
            return outgoing.text

        result = await PremiumEmojiRequestMiddleware()(downstream, bot, method)
        self.assertEqual(result, "✅")

    async def test_all_emoji_codepoints_in_python_sources_are_covered(self):
        import pathlib

        source_root = pathlib.Path(__file__).resolve().parents[1]
        codepoints = set()
        for path in source_root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for char in path.read_text(encoding="utf-8"):
                if ord(char) >= 0x1F000 or 0x2600 <= ord(char) <= 0x27FF:
                    codepoints.add(char)
        unsupported = {
            char for char in codepoints
            if char not in "\uFE0E\uFE0F\u20E3"
            and not 0x1F1E6 <= ord(char) <= 0x1F1FF
        }
        mapped_codepoints = {char for glyph in PREMIUM_EMOJI_IDS for char in glyph}
        self.assertFalse(unsupported - mapped_codepoints)

    async def test_retries_original_message_if_telegram_rejects_custom_emoji(self):
        method = SendMessage(chat_id=1, text="✅ Готово")
        bot = SimpleNamespace(default=SimpleNamespace(parse_mode=ParseMode.HTML))
        sent = []

        async def downstream(_bot, outgoing):
            sent.append(outgoing)
            if len(sent) == 1:
                raise TelegramBadRequest(method=outgoing, message="can't parse custom emoji")
            return "ok"

        result = await PremiumEmojiRequestMiddleware()(downstream, bot, method)
        self.assertEqual(result, "ok")
        self.assertEqual(len(sent), 2)
        self.assertIn("<tg-emoji", sent[0].text)
        self.assertEqual(sent[1].text, method.text)

    async def test_converts_caption_nested_in_edit_message_media(self):
        caption = "<b>📱 Устройство</b>\n🔗 Laptop\n⚙️ Система: Windows\n🕒 Последняя активность"
        method = EditMessageMedia(
            chat_id=1,
            message_id=2,
            media=InputMediaPhoto(media="file-id", caption=caption, parse_mode=ParseMode.HTML),
        )
        bot = SimpleNamespace(default=SimpleNamespace(parse_mode=ParseMode.HTML))
        captured = {}

        async def downstream(_bot, outgoing):
            captured["method"] = outgoing
            return "ok"

        await PremiumEmojiRequestMiddleware()(downstream, bot, method)
        rendered_caption = captured["method"].media.caption
        self.assertEqual(rendered_caption.count("<tg-emoji"), 4)
        self.assertIn(f'emoji-id="{ICON_IDS["devices"]}"', rendered_caption)


if __name__ == "__main__":
    unittest.main()
