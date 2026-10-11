"""
Email authentication handler for existing ByMeVPN clients.
"""
import asyncio
import logging
import secrets
import re
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SUPPORT_USERNAME
from database import (
    ensure_user, get_user_by_email, save_email_auth_code, allow_email_auth_request,
    verify_email_auth_code, link_telegram_to_user, has_active_subscription
)
from keyboards import authorized_user_menu, back_to_menu
from states import EmailAuth
from utils import safe_answer

logger = logging.getLogger(__name__)
router = Router()

# Email validation regex
EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')


def generate_auth_code() -> str:
    """Generate 6-digit random code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def is_valid_email(email: str) -> bool:
    """Validate email format."""
    return bool(EMAIL_REGEX.match(email.strip()))


async def send_auth_email(to_email: str, code: str) -> bool:
    """Send authentication code via SMTP."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD]):
        logger.error("SMTP settings not configured")
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'ByMeVPN - Код для входа'
        msg['From'] = SMTP_USER
        msg['To'] = to_email

        text_body = f"""Здравствуйте!

Ваш одноразовый код для входа в ByMeVPN: {code}

Код действителен в течение 5 минут.
Если вы не запрашивали этот код, просто проигнорируйте это письмо.

С уважением,
Команда ByMeVPN
"""

        html_body = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .code {{ font-size: 32px; font-weight: bold; color: #2c5aa0; letter-spacing: 8px; padding: 20px; background: #f5f5f5; border-radius: 8px; text-align: center; margin: 20px 0; }}
        .footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Здравствуйте!</h2>
        <p>Ваш одноразовый код для входа в ByMeVPN:</p>
        <div class="code">{code}</div>
        <p>Код действителен в течение <strong>5 минут</strong>.</p>
        <p>Если вы не запрашивали этот код, просто проигнорируйте это письмо.</p>
        <div class="footer">
            С уважением,<br>
            Команда ByMeVPN
        </div>
    </div>
</body>
</html>"""

        part1 = MIMEText(text_body, 'plain', 'utf-8')
        part2 = MIMEText(html_body, 'html', 'utf-8')

        msg.attach(part1)
        msg.attach(part2)

        # SMTP is blocking; run it in a worker thread so it cannot stall
        # Telegram update processing while DNS/TLS/authentication is in progress.
        await asyncio.to_thread(_send_smtp_message, to_email, msg.as_string())

        logger.info("Auth email sent")
        return True

    except Exception as e:
        logger.error("Failed to send auth email (%s)", type(e).__name__)
        return False


def _send_smtp_message(to_email: str, message: str) -> None:
    """Blocking SMTP transport, intentionally called via asyncio.to_thread."""
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, to_email, message)


# ---------------------------------------------------------------------------
# Entry point: "Я уже клиент ByMeVPN" button
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "auth_existing_client")
async def cb_existing_client(callback: CallbackQuery, state: FSMContext):
    """Handler for 'Я уже клиент ByMeVPN' button."""
    await safe_answer(callback)
    await state.set_state(EmailAuth.waiting_email)

    text = (
        "Введите email, который вы указывали при оплате или регистрации на сайте.\n\n"
        "Для отмены напишите /cancel"
    )
    try:
        await callback.message.edit_text(text, reply_markup=back_to_menu())
    except Exception:
        # Message has no text (e.g., photo), send new message
        await callback.message.answer(text, reply_markup=back_to_menu())


# ---------------------------------------------------------------------------
# Step 2: Email input handler
# ---------------------------------------------------------------------------

@router.message(EmailAuth.waiting_email, F.text)
async def process_email(message: Message, state: FSMContext):
    """Process email input from user."""
    email = message.text.strip()

    # Check for cancel command
    if email.lower() == '/cancel':
        await state.clear()
        await message.answer("Отменено. Возвращаю в главное меню.")
        # Return to main menu
        from handlers.start import _send_main_menu
        await _send_main_menu(message.bot, message, message.from_user.id, message.from_user.first_name or "друг")
        return

    # Validate email format
    if not is_valid_email(email):
        await message.answer(
            "Некорректный email. Пожалуйста, введите действительный email адрес.\n\n"
            "Для отмены напишите /cancel"
        )
        return

    if not await allow_email_auth_request(message.from_user.id, email):
        await message.answer(
            "Слишком много запросов кода. Подождите минуту перед повторной попыткой.",
            reply_markup=back_to_menu(),
        )
        return

    await ensure_user(message.from_user.id)
    existing_user = await get_user_by_email(email)
    generic_response = (
        "Если этот email зарегистрирован, мы отправили код для входа. "
        "Проверьте почту и папку «Спам». Если письма нет, попробуйте позже."
    )

    if not existing_user:
        await message.answer(generic_response, reply_markup=back_to_menu())
        await state.clear()
        return

    code = generate_auth_code()
    expires_at = int(time.time()) + 300
    await save_email_auth_code(existing_user["user_id"], email, code, expires_at)
    email_sent = await send_auth_email(email, code)

    if not email_sent:
        await state.clear()
        await message.answer(generic_response, reply_markup=back_to_menu())
        return

    await state.update_data(
        auth_email=email,
        auth_user_id=existing_user["user_id"],
        auth_attempts=0,
    )
    await state.set_state(EmailAuth.waiting_code)
    await message.answer(generic_response)


# ---------------------------------------------------------------------------
# Step 4: Code verification handler
# ---------------------------------------------------------------------------

@router.message(EmailAuth.waiting_code, F.text)
async def process_code(message: Message, state: FSMContext):
    """Process verification code input from user."""
    code_input = message.text.strip()

    # Check for cancel command
    if code_input.lower() == '/cancel':
        await state.clear()
        await message.answer("Отменено. Возвращаю в главное меню.")
        # Return to main menu
        from handlers.start import _send_main_menu
        await _send_main_menu(message.bot, message, message.from_user.id, message.from_user.first_name or "друг")
        return

    # Get stored data
    data = await state.get_data()
    email = data.get("auth_email")
    existing_user_id = data.get("auth_user_id")

    if not email or not existing_user_id:
        await message.answer(
            "Сессия истекла. Пожалуйста, начните авторизацию заново.",
            reply_markup=back_to_menu()
        )
        await state.clear()
        return

    # Verify code
    # Use existing_user_id for verification since that's where we saved the code
    is_valid = await verify_email_auth_code(existing_user_id, code_input)

    if not is_valid:
        attempts = int(data.get("auth_attempts", 0)) + 1
        if attempts >= 5:
            await state.clear()
            await message.answer(
                "Лимит попыток исчерпан. Начните авторизацию заново позже.",
                reply_markup=back_to_menu(),
            )
        else:
            await state.update_data(auth_attempts=attempts)
            await message.answer(
                f"Неверный или просроченный код. Осталось попыток: {5 - attempts}. Для отмены напишите /cancel."
            )
        return

    # Code is valid - link this Telegram account to the user
    current_telegram_id = message.from_user.id

    # Update the existing user's record with new telegram ID
    # Or if it's the same user, just confirm
    if existing_user_id != current_telegram_id:
        linked = await link_telegram_to_user(current_telegram_id, email)
        if not linked:
            await state.clear()
            await message.answer(
                "Не удалось безопасно связать аккаунт. Напишите в поддержку, чтобы перенести доступ без потери подписки.",
                reply_markup=back_to_menu(),
            )
            logger.error("Account linking failed after email verification (target user %d)", current_telegram_id)
            return
        logger.info("Telegram account linked after email verification (target user %d)", current_telegram_id)

    # Clear state
    await state.clear()

    # Show success message and authorized user menu
    await message.answer(
        "✅ Авторизация успешно завершена!\n\n"
        "Добро пожаловать в ByMeVPN!",
        reply_markup=authorized_user_menu()
    )


# ---------------------------------------------------------------------------
# Cancel command handler for any auth state
# ---------------------------------------------------------------------------

@router.message(F.text == "/cancel")
async def cmd_cancel(message: Message, state: FSMContext):
    """Cancel any ongoing authentication flow."""
    current_state = await state.get_state()
    if current_state and current_state.startswith("EmailAuth"):
        await state.clear()
        await message.answer("Отменено. Возвращаю в главное меню.")
        # Return to main menu
        from handlers.start import _send_main_menu
        await _send_main_menu(message.bot, message, message.from_user.id, message.from_user.first_name or "друг")
