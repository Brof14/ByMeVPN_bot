"""Customer copy contracts: clarity without weakening payment disclosures."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery

from constants import get_price_for_months
from handlers.buy import _checkout_confirmation_text, _tariff_intro, cb_enter_promo
from keyboards import payment_kb, tariff_selection_kb
from subscription import _activation_message
from utils import send_text_screen


def actions(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class MinimalCopyTests(unittest.IsolatedAsyncioTestCase):
    def test_device_choices_share_one_row_and_keep_all_plans(self):
        for devices in (2, 5, 10):
            kb = tariff_selection_kb(devices)
            self.assertEqual(
                actions(SimpleNamespace(inline_keyboard=kb.inline_keyboard[:1])),
                ["devtier_2", "devtier_5", "devtier_10"],
            )
            self.assertIn("✓", kb.inline_keyboard[0][(2, 5, 10).index(devices)].text)
            self.assertEqual(
                [callback for callback in actions(kb) if callback.startswith("tariff_")],
                [f"tariff_{months}_{devices}" for months in (1, 3, 6, 12)],
            )
            self.assertNotIn("promo_enter", actions(kb))
            self.assertEqual(len(kb.inline_keyboard), 6)

    def test_promo_is_on_payment_screen_before_providers(self):
        callbacks = actions(payment_kb(129, 30, devices=10))
        self.assertEqual(
            callbacks,
            ["promo_enter", "pay_stars", "pay_yookassa", "pay_crypto", "devtier_10"],
        )

    def test_renewal_intro_is_one_instruction(self):
        self.assertEqual(
            _tariff_intro(10, renewal=True),
            "<b>Продление подписки</b>\n\nВыберите количество устройств и срок подписки.",
        )

    def test_full_checkout_discloses_recurring_charge_and_documents(self):
        for months in (1, 3, 6, 12):
            for devices in (2, 5, 10):
                price, days = get_price_for_months(months, devices)
                message = _checkout_confirmation_text({
                    "months": months, "devices": devices, "price_rub": price, "days": days,
                })
                self.assertIn(f"К оплате: <b>{price} ₽</b>", message)
                self.assertIn(f"{price} ₽ каждые {days} дней", message)
                self.assertIn("условия автопродления", message)
                self.assertIn("Политика конфиденциальности", message)
                self.assertIn("Stars и CryptoBot — без автопродления.", message)

    def test_promo_discount_does_not_change_future_billing_disclosure(self):
        message = _checkout_confirmation_text({
            "months": 3, "devices": 2, "price_rub": 221, "days": 125,
            "original_price": 316,
        })
        self.assertIn("К оплате: <b>221 ₽</b>", message)
        self.assertIn("Без промокода: <s>316 ₽</s>", message)
        self.assertIn("316 ₽ каждые 120 дней", message)

    def test_activation_uses_correct_day_word(self):
        for days, word in ((1, "день"), (2, "дня"), (3, "дня"), (11, "дней"), (21, "день"), (124, "дня")):
            self.assertIn(
                f"Доступ: <b>{days} {word}</b>",
                _activation_message("<b>Подписка активирована</b>", "", days, 2),
            )

    async def test_enter_promo_preserves_selected_tariff_for_return(self):
        callback = SimpleNamespace(from_user=SimpleNamespace(id=1))
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"months": 6, "devices": 5}),
            set_state=AsyncMock(),
        )
        with (
            patch("handlers.buy.safe_answer", new=AsyncMock()),
            patch("handlers.buy.send_text_screen", new=AsyncMock()) as send,
        ):
            await cb_enter_promo(callback, object(), state)
        keyboard = send.await_args.args[3]
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "tariff_6_5")
        self.assertIn("Промокод", send.await_args.args[2])

    async def test_photo_screen_becomes_plain_text(self):
        callback = MagicMock(spec=CallbackQuery)
        callback.message.photo = [object()]
        with (
            patch("utils._replace_text_screen", new=AsyncMock()) as replace,
            patch("utils.send_or_edit", new=AsyncMock()) as edit,
        ):
            await send_text_screen(object(), callback, "<b>Тарифы</b>")
        replace.assert_awaited_once()
        edit.assert_not_awaited()
