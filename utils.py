import logging
import asyncio
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import qrcode
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InputMediaPhoto, InputMediaAnimation

from config import MENU_PHOTO
from emojis import get_emoji, EmojiTheme, create_progress_bar_emoji, get_status_emoji

logger = logging.getLogger(__name__)


async def update_menu(
    bot: Bot,
    target: Message | CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup = None,
    photo: str = MENU_PHOTO
):
    """Enhanced menu update with better error handling"""
    try:
        if isinstance(target, CallbackQuery):
            message = target.message
        else:
            message = target

        # Try to edit existing message first
        try:
            if photo:
                await bot.edit_message_media(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    media=InputMediaPhoto(media=photo, caption=text, parse_mode="HTML"),
                    reply_markup=reply_markup
                )
            else:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
        except Exception as edit_error:
            logger.debug(f"Failed to edit message: {edit_error}")
            
            # If edit fails, send new message
            try:
                if photo:
                    await bot.send_photo(
                        message.chat.id,
                        photo=photo,
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )
                else:
                    await bot.send_message(
                        message.chat.id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )
            except Exception as send_error:
                logger.error(f"Failed to send new message: {send_error}")
                raise
                
    except Exception as e:
        logger.error(f"Critical error in update_menu: {e}")
        raise


def create_beautiful_qr_code(key: str, add_logo: bool = True) -> BytesIO:
    """
    Create a beautiful QR code with logo and gradient background
    
    Args:
        key: VPN key string
        add_logo: Whether to add logo to center
        
    Returns:
        BytesIO object with QR code image
    """
    try:
        # Generate QR code with higher quality and error correction
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,  # High error correction for logo
            box_size=12,
            border=4,
        )
        qr.add_data(key)
        qr.make(fit=True)
        
        # Create QR code image with white background
        qr_img = qr.make_image(fill_color="#1a1a1a", back_color="white")
        
        # Convert to RGBA for transparency
        qr_img = qr_img.convert("RGBA")
        
        # Add gradient background
        width, height = qr_img.size
        gradient = Image.new('RGBA', (width, height))
        draw = ImageDraw.Draw(gradient)
        
        # Create subtle gradient
        for y in range(height):
            color_value = int(240 + (15 * y / height))  # Light gradient
            color = (color_value, color_value, color_value + 5, 255)
            draw.line([(0, y), (width, y)], fill=color)
        
        # Composite gradient with QR code
        qr_img = Image.alpha_composite(gradient, qr_img)
        
        # Add logo in center if requested
        if add_logo:
            try:
                # Try to load logo from config or create a simple one
                logo_size = min(width, height) // 6
                logo = Image.new('RGBA', (logo_size, logo_size), (26, 26, 26, 255))  # Dark gray logo
                
                # Add a simple shield icon to the logo
                logo_draw = ImageDraw.Draw(logo)
                # Draw a simple shield shape
                center = logo_size // 2
                margin = logo_size // 8
                logo_draw.ellipse(
                    [margin, margin, logo_size - margin, logo_size - margin],
                    fill=(26, 26, 26, 255),  # Dark gray color instead of blue
                    outline=(255, 255, 255, 255),
                    width=2
                )
                
                # Calculate position to center the logo
                logo_pos = ((width - logo_size) // 2, (height - logo_size) // 2)
                
                # Paste logo with transparency
                qr_img.paste(logo, logo_pos, logo)
                
            except Exception as logo_error:
                logger.warning(f"Could not add logo to QR code: {logo_error}")
        
        # Convert to bytes
        bio = BytesIO()
        qr_img.save(bio, "PNG", optimize=True, quality=95)
        bio.seek(0)
        
        return bio
        
    except Exception as e:
        logger.error(f"Error creating beautiful QR code: {e}")
        # Fallback to simple QR code
        return generate_simple_qr_fallback(key)

def generate_simple_qr_fallback(key: str) -> BytesIO:
    """Fallback simple QR code generation"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(key)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        bio = BytesIO()
        img.save(bio, "PNG", optimize=True)
        bio.seek(0)
        
        return bio
        
    except Exception as e:
        logger.error(f"Error in fallback QR generation: {e}")
        raise
async def generate_and_send_beautiful_qr(
    bot: Bot,
    chat_id: int,
    key: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup = None,
    use_beautiful_qr: bool = True
):
    """Enhanced QR code generation with beautiful design and error handling"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Generate QR code with fallback on each attempt
            if attempt == 0 and use_beautiful_qr:
                qr_bio = create_beautiful_qr_code(key, add_logo=True)
            elif attempt == 1:
                qr_bio = create_beautiful_qr_code(key, add_logo=False)
            else:
                qr_bio = generate_simple_qr_fallback(key)
            
            # Send photo with QR code
            await bot.send_photo(
                chat_id=chat_id,
                photo=BufferedInputFile(qr_bio.getvalue(), "qrcode.png"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
            logger.info(f"QR code sent successfully to user {chat_id} on attempt {attempt + 1}")
            return
            
        except Exception as e:
            logger.warning(f"QR code generation attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                # Final fallback - send key as text
                error_caption = (
                    f"{caption}\n\n"
                    f"⚠️ <b>QR-код не удалось сгенерировать</b>\n"
                    f"📋 <b>Ваш ключ:</b>\n"
                    f"<code>{key}</code>"
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=error_caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
                logger.error(f"All QR generation attempts failed for user {chat_id}, sent text fallback")
            else:
                await asyncio.sleep(0.5)  # Brief delay between retries

def create_user_profile_card(user_data: Dict, keys_data: List[Dict]) -> str:
    """
    Create a beautiful user profile card with emojis and formatting
    
    Args:
        user_data: User information dict
        keys_data: List of user's keys
        
    Returns:
        Formatted HTML string for profile card
    """
    try:
        user_name = user_data.get('name', 'Пользователь')
        user_id = user_data.get('id', 0)
        
        # Get subscription status
        active_keys = [k for k in keys_data if k.get('expiry', 0) > int(datetime.now().timestamp())]
        total_keys = len(keys_data)
        
        # Calculate subscription info
        if active_keys:
            latest_expiry = max(k.get('expiry', 0) for k in active_keys)
            days_left = (latest_expiry - int(datetime.now().timestamp())) // 86400
            status_emoji = get_status_emoji('active')
            status_text = "Активна"
            status_theme = EmojiTheme.SUCCESS
        else:
            days_left = 0
            status_emoji = get_status_emoji('expired')
            status_text = "Истекла"
            status_theme = EmojiTheme.ERROR
        
        # Create progress bar for subscription
        if total_keys > 0:
            # Assuming 30-day subscription for progress calculation
            progress_bar = create_progress_bar_emoji(days_left, 30, length=10)
            progress_percent = min(100, int((days_left / 30) * 100))
        else:
            progress_bar = "▫️" * 10
            progress_percent = 0
        
        # Build profile card
        profile_text = f"""
{get_emoji('logo', EmojiTheme.PREMIUM)} <b>Профиль пользователя</b>

━━━━━━━━━━━━━━━━━━

{get_emoji('users', EmojiTheme.INFO)} <b>Имя:</b> {user_name}
{get_emoji('gear', EmojiTheme.INFO)} <b>ID:</b> <code>{user_id}</code>

{status_emoji} <b>Статус подписки:</b> {status_text}
{get_emoji('clock', status_theme)} <b>Дней осталось:</b> {days_left}

{get_emoji('chart', EmojiTheme.INFO)} <b>Прогресс:</b> {progress_percent}%
{progress_bar}

{get_emoji('key', EmojiTheme.DEFAULT)} <b>Всего ключей:</b> {total_keys}
{get_emoji('check', EmojiTheme.SUCCESS)} <b>Активных:</b> {len(active_keys)}
━━━━━━━━━━━━━━━━━━
"""
        
        # Add key cards if user has keys
        if keys_data:
            profile_text += "\n<b>Ваши ключи:</b>\n\n"
            for i, key_data in enumerate(keys_data[:3], 1):  # Show max 3 keys
                expiry_date = format_expiry_date(key_data.get('expiry', 0))
                key_status = get_status_emoji('active' if key_data.get('expiry', 0) > int(datetime.now().timestamp()) else 'expired')
                days = key_data.get('days', 0)
                
                profile_text += f"""
{key_status} <b>Ключ #{i}</b>
   Срок: {expiry_date}
   Дней: {days}
   Примечание: {key_data.get('remark', 'Основной')}
\n"""
        
        return profile_text
        
    except Exception as e:
        logger.error(f"Error creating user profile card: {e}")
        return f"{get_emoji('error', EmojiTheme.ERROR)} <b>Ошибка профиля</b>\n\nНе удалось загрузить данные профиля."

def create_subscription_progress_card(expiry_timestamp: int, total_days: int = 30) -> str:
    """
    Create a subscription progress card with visual indicators
    
    Args:
        expiry_timestamp: Subscription expiry timestamp
        total_days: Total subscription days
        
    Returns:
        Formatted HTML string for progress card
    """
    try:
        now = int(datetime.now().timestamp())
        
        if expiry_timestamp <= now:
            return f"""
{get_status_emoji('expired')} <b>Подписка истекла</b>

{create_progress_bar_emoji(0, total_days, length=12)}
0% использовано
\n{get_emoji('warning', EmojiTheme.WARNING)} Продлите подписку для продолжения использования
"""
        
        remaining_seconds = expiry_timestamp - now
        remaining_days = remaining_seconds // 86400
        used_days = total_days - remaining_days
        
        if remaining_days <= 0:
            progress_percent = 100
            theme = EmojiTheme.ERROR
        elif remaining_days <= 7:
            progress_percent = int((used_days / total_days) * 100)
            theme = EmojiTheme.WARNING
        else:
            progress_percent = int((used_days / total_days) * 100)
            theme = EmojiTheme.SUCCESS
        
        progress_bar = create_progress_bar_emoji(remaining_days, total_days, length=12)
        
        return f"""
{get_status_emoji('active')} <b>Прогресс подписки</b>

{progress_bar}
Использовано: {progress_percent}%
Осталось: {remaining_days} дней

{get_emoji('clock', theme)} Истекает: {format_expiry_date(expiry_timestamp)}
"""
        
    except Exception as e:
        logger.error(f"Error creating subscription progress card: {e}")
        return f"{get_emoji('error', EmojiTheme.ERROR)} <b>Ошибка прогресса</b>"


async def send_popup_alert(bot: Bot, callback_query: CallbackQuery, text: str, 
                         show_alert: bool = True, cache_time: int = 0):
    """
    Send popup alert notification to user
    
    Args:
        bot: Bot instance
        callback_query: Callback query to answer
        text: Alert text
        show_alert: Whether to show as alert or toast
        cache_time: Cache time for the callback
    """
    try:
        await bot.answer_callback_query(
            callback_query_id=callback_query.id,
            text=text,
            show_alert=show_alert,
            cache_time=cache_time
        )
        logger.info(f"Popup alert sent to user {callback_query.from_user.id}")
    except Exception as e:
        logger.error(f"Error sending popup alert: {e}")

async def send_success_animation(bot: Bot, chat_id: int, message_text: str):
    """Send success animation with celebration"""
    try:
        # Send celebration animation (placeholder for actual .tgs file)
        animation_text = f"""
{get_emoji('gift', EmojiTheme.SUCCESS)} {get_emoji('check', EmojiTheme.SUCCESS)} {get_emoji('star', EmojiTheme.PREMIUM)}

<b>{message_text}</b>

{get_emoji('fire', EmojiTheme.PREMIUM)} Поздравляем с успешной активацией!
"""
        
        await bot.send_message(
            chat_id=chat_id,
            text=animation_text,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error sending success animation: {e}")

async def send_welcome_animation_sequence(bot: Bot, chat_id: int, user_name: str):
    """Send welcome animation sequence for new users"""
    try:
        # Step 1: Welcome animation
        welcome_text = f"""
{get_emoji('rocket', EmojiTheme.SUCCESS)} {get_emoji('star', EmojiTheme.PREMIUM)} {get_emoji('gift', EmojiTheme.SUCCESS)}

<b>Добро пожаловать, {user_name}!</b>

{get_emoji('shield', EmojiTheme.DEFAULT)} Готовимся запустить ваш VPN...
"""
        
        await bot.send_message(chat_id=chat_id, text=welcome_text, parse_mode="HTML")
        await asyncio.sleep(1)  # Small delay for effect
        
        # Step 2: Loading animation
        loading_text = f"""
{get_emoji('refresh', EmojiTheme.INFO)} Настраиваем ваш персональный доступ...

{get_emoji('gear', EmojiTheme.INFO)} Подготавливаем серверы...
"""
        
        await bot.send_message(chat_id=chat_id, text=loading_text, parse_mode="HTML")
        await asyncio.sleep(1)
        
        # Step 3: Ready animation
        ready_text = f"""
{get_emoji('check', EmojiTheme.SUCCESS)} {get_emoji('diamond', EmojiTheme.PREMIUM)} {get_emoji('rocket', EmojiTheme.SUCCESS)}

<b>Всё готово!</b>

{get_emoji('star', EmojiTheme.PREMIUM)} Ваш VPN доступ активирован
"""
        
        await bot.send_message(chat_id=chat_id, text=ready_text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error in welcome animation sequence: {e}")

async def send_expiry_warning_animation(bot: Bot, chat_id: int, days_left: int):
    """Send warning animation for expiring subscription"""
    try:
        if days_left <= 0:
            warning_emoji = get_emoji('cross', EmojiTheme.ERROR)
            urgency_text = "истекла"
        elif days_left <= 3:
            warning_emoji = get_emoji('warning', EmojiTheme.WARNING)
            urgency_text = f"истекает через {days_left} дня"
        else:
            warning_emoji = get_emoji('clock', EmojiTheme.WARNING)
            urgency_text = f"истекает через {days_left} дней"
        
        warning_text = f"""
{warning_emoji} {warning_emoji} {warning_emoji}

<b>Внимание!</b>

Ваша подписка {urgency_text}

{get_emoji('star', EmojiTheme.PREMIUM)} Продлите сейчас, чтобы не потерять доступ!
"""
        
        await bot.send_message(chat_id=chat_id, text=warning_text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error sending expiry warning animation: {e}")


def detect_user_theme(user_data: dict = None) -> str:
    """
    Detect user theme preference (simplified version)
    In production, this could use Mini App data or user preferences
    
    Returns: 'dark' or 'light'
    """
    try:
        # For now, default to dark theme (most Telegram users prefer it)
        # In production, you could:
        # 1. Use Mini App to detect system theme
        # 2. Store user preference in database
        # 3. Use time-based detection (dark at night)
        
        current_hour = datetime.now().hour
        
        # Auto dark theme during evening/night hours (20:00-08:00)
        if 20 <= current_hour or current_hour <= 8:
            return 'dark'
        else:
            return 'light'
            
    except Exception as e:
        logger.error(f"Error detecting user theme: {e}")
        return 'dark'  # Safe default

def get_theme_colors(theme: str) -> dict:
    """
    Get color scheme based on theme
    
    Args:
        theme: 'dark' or 'light'
        
    Returns:
        Dictionary with color codes
    """
    if theme == 'light':
        return {
            'primary': '#4A90E2',      # Blue
            'secondary': '#7B68EE',    # Purple
            'success': '#52C41A',      # Green
            'warning': '#FAAD14',      # Orange
            'error': '#FF4D4F',        # Red
            'background': '#FFFFFF',   # White
            'text': '#262626',         # Dark gray
            'border': '#D9D9D9',       # Light gray
        }
    else:  # dark theme
        return {
            'primary': '#1890FF',      # Bright blue
            'secondary': '#722ED1',    # Purple
            'success': '#52C41A',      # Green
            'warning': '#FAAD14',      # Orange
            'error': '#FF4D4F',        # Red
            'background': '#141414',   # Dark gray
            'text': '#FFFFFF',         # White
            'border': '#434343',       # Medium gray
        }

def create_themed_message(text: str, theme: str = 'dark') -> str:
    """
    Create themed message with appropriate styling
    
    Args:
        text: Message text
        theme: 'dark' or 'light'
        
    Returns:
        Themed HTML message
    """
    colors = get_theme_colors(theme)
    
    # Add theme-specific styling
    if theme == 'light':
        themed_prefix = f"<span style='color: {colors['primary']}'>{get_emoji('logo', EmojiTheme.DEFAULT)}</span>"
    else:
        themed_prefix = f"<span style='color: {colors['primary']}'>{get_emoji('logo', EmojiTheme.PREMIUM)}</span>"
    
    return f"{themed_prefix} {text}"

def format_expiry_date(timestamp: int) -> str:
    """Format timestamp to readable date"""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y в %H:%M")
    except Exception as e:
        logger.error(f"Error formatting date {timestamp}: {e}")
        return "Некорректная дата"


def get_time_until_expiry(expiry_timestamp: int) -> dict:
    """Get time remaining until expiry"""
    try:
        now = int(datetime.now().timestamp())
        remaining = expiry_timestamp - now
        
        if remaining <= 0:
            return {"days": 0, "hours": 0, "minutes": 0, "expired": True}
        
        days = remaining // 86400
        hours = (remaining % 86400) // 3600
        minutes = (remaining % 3600) // 60
        
        return {
            "days": days,
            "hours": hours, 
            "minutes": minutes,
            "expired": False
        }
    except Exception as e:
        logger.error(f"Error calculating time until expiry: {e}")
        return {"days": 0, "hours": 0, "minutes": 0, "expired": True}


def format_time_remaining(expiry_timestamp: int) -> str:
    """Format remaining time in human readable format"""
    try:
        time_left = get_time_until_expiry(expiry_timestamp)
        
        if time_left["expired"]:
            return "Истекла"
        
        parts = []
        if time_left["days"] > 0:
            parts.append(f"{time_left['days']} д")
        if time_left["hours"] > 0:
            parts.append(f"{time_left['hours']} ч")
        if time_left["minutes"] > 0 and time_left["days"] == 0:
            parts.append(f"{time_left['minutes']} мин")
        
        return " ".join(parts) if parts else "Менее минуты"
    except Exception as e:
        logger.error(f"Error formatting time remaining: {e}")
        return "Ошибка"


def create_progress_bar(current: int, total: int, length: int = 10) -> str:
    """Create a text progress bar"""
    try:
        if total <= 0:
            return "█" * length
        
        filled = int((current / total) * length)
        empty = length - filled
        
        return "█" * filled + "░" * empty
    except Exception as e:
        logger.error(f"Error creating progress bar: {e}")
        return "░" * length


def get_subscription_status_text(expiry_timestamp: int) -> str:
    """Get subscription status text without emoji"""
    try:
        time_left = get_time_until_expiry(expiry_timestamp)
        
        if time_left["expired"]:
            return "Истекла"
        elif time_left["days"] <= 1:
            return "Истекает сегодня"
        elif time_left["days"] <= 3:
            return "Истекает скоро"
        elif time_left["days"] <= 7:
            return "Активна"
        else:
            return "Активна"
    except Exception as e:
        logger.error(f"Error getting subscription status: {e}")
        return "Неизвестно"


def format_number(number: int) -> str:
    """Format number with thousands separator"""
    try:
        return f"{number:,}".replace(",", " ")
    except Exception as e:
        logger.error(f"Error formatting number {number}: {e}")
        return str(number)


def generate_referral_link(bot_username: str, user_id: int) -> str:
    """Generate referral link"""
    try:
        return f"https://t.me/{bot_username}?start={user_id}"
    except Exception as e:
        logger.error(f"Error generating referral link: {e}")
        return ""


async def export_keys_to_file(bot: Bot, user_id: int, keys: list) -> str:
    """Export user keys to a text file"""
    try:
        lines = [
            f"ByMeVPN - Экспорт ключей",
            f"Пользователь ID: {user_id}",
            f"Дата экспорта: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
            "=" * 50,
            ""
        ]
        
        for i, key_data in enumerate(keys, 1):
            expiry_status = get_subscription_status_text(key_data["expiry"])
            lines.extend([
                f"Ключ #{i}",
                f"Статус: {expiry_status}",
                f"Создан: {format_expiry_date(key_data['created'])}",
                f"Истекает: {format_expiry_date(key_data['expiry'])}",
                f"Дней: {key_data['days']}",
                f"Примечание: {key_data['remark']}",
                f"Ключ: {key_data['key']}",
                "-" * 30,
                ""
            ])
        
        content = "\n".join(lines)
        file_data = content.encode('utf-8')
        
        return BufferedInputFile(file_data, "bymevpn_keys_export.txt")
        
    except Exception as e:
        logger.error(f"Error exporting keys for user {user_id}: {e}")
        raise


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    parse_mode: str = None,
    reply_markup: InlineKeyboardMarkup = None
) -> bool:
    """Safely send message with error handling"""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
        return True
    except Exception as e:
        logger.error(f"Error sending message to {chat_id}: {e}")
        return False


async def safe_edit_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode: str = None,
    reply_markup: InlineKeyboardMarkup = None
) -> bool:
    """Safely edit message with error handling"""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
        return True
    except Exception as e:
        logger.debug(f"Error editing message {message_id} in {chat_id}: {e}")
        return False


def validate_payment_payload(payload: str) -> dict:
    """Validate and parse payment payload"""
    try:
        parts = payload.split("_")
        if len(parts) < 4:
            return {"valid": False}
        
        return {
            "valid": True,
            "method": parts[0],
            "user_id": int(parts[1]) if parts[1].isdigit() else None,
            "days": int(parts[2]) if parts[2].isdigit() else None,
            "timestamp": int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
        }
    except Exception as e:
        logger.error(f"Error validating payment payload: {e}")
        return {"valid": False}


def get_plan_indicator(plan_type: str) -> str:
    """Get text indicator for plan type"""
    plan_indicators = {
        "personal": "[1]",
        "duo": "[2]", 
        "family": "[5]",
        "trial": "[T]",
        "ref_bonus": "[B]"
    }
    return plan_indicators.get(plan_type, "[K]")


def get_payment_method_indicator(method: str) -> str:
    """Get text indicator for payment method"""
    method_indicators = {
        "stars": "[S]",
        "yookassa": "[Y]",
        "cryptobot": "[C]",
        "rub": "[R]",
        "usd": "[$]",
        "eur": "[E]"
    }
    return method_indicators.get(method.lower(), "[P]")


async def handle_bot_error(bot: Bot, user_id: int, error: Exception, context: str = ""):
    """Handle bot errors and notify user if needed"""
    logger.error(f"Bot error in {context}: {error}", exc_info=True)
    
    error_messages = {
        "payment": "Произошла ошибка при обработке платежа. Пожалуйста, попробуйте еще раз или свяжитесь с поддержкой.",
        "subscription": "Ошибка при выдаче подписки. Напишите в поддержку, мы поможем!",
        "database": "Временная техническая проблема. Попробуйте через несколько минут.",
        "network": "Проблемы с сетью. Проверьте подключение и попробуйте снова.",
        "general": "Что-то пошло не так. Напишите в поддержку, мы всё исправим!"
    }
    
    # Determine error type
    error_type = "general"
    error_str = str(error).lower()
    
    if any(keyword in error_str for keyword in ["payment", "invoice", "stars", "yookassa", "crypto"]):
        error_type = "payment"
    elif any(keyword in error_str for keyword in ["subscription", "key", "client", "xui"]):
        error_type = "subscription"
    elif any(keyword in error_str for keyword in ["database", "db", "sqlite"]):
        error_type = "database"
    elif any(keyword in error_str for keyword in ["network", "connection", "timeout"]):
        error_type = "network"
    
    message = error_messages.get(error_type, error_messages["general"])
    message += f"\n\nТехподдержка: @ByMeVPN_support"
    
    await safe_send_message(bot, user_id, message, parse_mode="HTML")