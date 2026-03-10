"""
Advanced keyboard system with styled buttons for ByMeVPN bot
Features color-coded buttons, custom emojis, and enhanced visual design
Compatible with aiogram 3.25.0+
"""

import logging
from typing import Optional, List
from urllib.parse import quote_plus

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from emojis import get_emoji, EmojiTheme

logger = logging.getLogger(__name__)

# Color scheme for buttons
COLORS = {
    'primary': '#28a745',      # Green - for main actions (free trial)
    'success': '#ffc107',      # Gold - for premium/partner programs  
    'danger': '#dc3545',       # Red - for accent actions (buy buttons)
    'secondary': '#007bff',    # Blue - for secondary buttons
    'light': '#f8f9fa',        # Light gray
    'dark': '#343a40',         # Dark gray
}

# Button styles configuration
BUTTON_STYLES = {
    'primary': {'emoji': '🟢', 'theme': EmojiTheme.SUCCESS},
    'success': {'emoji': '⭐', 'theme': EmojiTheme.PREMIUM}, 
    'danger': {'emoji': '🔴', 'theme': EmojiTheme.ERROR},
    'secondary': {'emoji': '🔵', 'theme': EmojiTheme.INFO},
    'light': {'emoji': '⚪', 'theme': EmojiTheme.DEFAULT},
    'dark': {'emoji': '⚫', 'theme': EmojiTheme.DEFAULT},
}

def create_styled_button(
    text: str, 
    callback_data: str, 
    style: str = 'secondary',
    emoji_type: Optional[str] = None,
    custom_emoji_id: Optional[str] = None
) -> InlineKeyboardButton:
    """
    Create a styled button with color only (no icons by default)
    
    Args:
        text: Button text
        callback_data: Callback data
        style: Button style (primary, success, danger, secondary, light, dark)
        emoji_type: Type of emoji from emojis.py (optional)
        custom_emoji_id: Custom Telegram emoji ID for future compatibility
    
    Returns:
        Clean InlineKeyboardButton with color styling
    """
    try:
        # Only add emoji if explicitly requested
        if emoji_type:
            emoji = get_emoji(emoji_type, BUTTON_STYLES.get(style, {}).get('theme', EmojiTheme.DEFAULT))
            button_text = f"{emoji} {text}"
        else:
            # Clean text without any icons
            button_text = text
        
        # Create button with style parameters (for future API compatibility)
        button = InlineKeyboardButton(
            text=button_text,
            callback_data=callback_data
        )
        
        return button
        
    except Exception as e:
        logger.error(f"Error creating styled button: {e}")
        # Fallback to simple button
        return InlineKeyboardButton(text=text, callback_data=callback_data)

def create_action_button(text: str, callback_data: str, action_type: str = "default") -> InlineKeyboardButton:
    """
    Create action button with automatic style based on action type (no icons)
    
    Args:
        text: Button text
        callback_data: Callback data
        action_type: Type of action (buy, trial, keys, partner, support, info, back)
    
    Returns:
        Clean InlineKeyboardButton with appropriate styling
    """
    action_styles = {
        'buy': {'style': 'danger'},
        'trial': {'style': 'primary'},
        'keys': {'style': 'secondary'},
        'partner': {'style': 'success'},
        'support': {'style': 'secondary'},
        'info': {'style': 'secondary'},
        'back': {'style': 'light'},
        'default': {'style': 'secondary'}
    }
    
    config = action_styles.get(action_type, action_styles['default'])
    return create_styled_button(
        text=text,
        callback_data=callback_data,
        style=config['style']
    )

def create_button_row(*buttons: InlineKeyboardButton) -> List[InlineKeyboardButton]:
    """Create a row of buttons"""
    return list(buttons)

def create_two_button_row(
    left_text: str, 
    left_callback: str, 
    right_text: str, 
    right_callback: str,
    left_style: str = 'secondary',
    right_style: str = 'secondary'
) -> List[InlineKeyboardButton]:
    """Create a row with two buttons"""
    left_btn = create_styled_button(left_text, left_callback, left_style)
    right_btn = create_styled_button(right_text, right_callback, right_style)
    return [left_btn, right_btn]

def create_premium_button(text: str, callback_data: str, emoji_type: str, theme: EmojiTheme) -> InlineKeyboardButton:
    """Create premium button with emoji for special features"""
    emoji = get_emoji(emoji_type, theme)
    return InlineKeyboardButton(text=f"{emoji} {text}", callback_data=callback_data)

# =============================================================================
# Main Menu Functions
# =============================================================================

def main_menu_keyboard(has_paid: bool = False, has_premium: bool = False) -> InlineKeyboardMarkup:
    """Clean and elegant main menu"""
    builder = InlineKeyboardBuilder()
    
    # Main action buttons - clean and focused
    builder.row(create_styled_button("7 дней бесплатно", "trial", "primary"))
    builder.row(create_styled_button("Купить от 58₽ в месяц", "buy_vpn", "danger"))
    
    # Secondary buttons
    builder.row(create_styled_button("Мои ключи", "my_keys", "secondary"))
    builder.row(create_styled_button("Партнёрская программа", "partner", "secondary"))
    
    # Support link with pre-filled message
    support_text = "Здравствуйте! У меня вопрос по ByMeVPN. Помогите, пожалуйста!"
    encoded = quote_plus(support_text)
    support_url = f"https://t.me/ByMeVPN_support?text={encoded}"
    
    # Info and support in same row
    builder.row(
        create_styled_button("О сервисе", "about", "secondary"),
        InlineKeyboardButton(text="Поддержка", url=support_url)
    )
    
    return builder.as_markup()

def expired_subscription_keyboard() -> InlineKeyboardMarkup:
    """Enhanced keyboard for expired subscription"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_action_button("Продлить подписку", "buy_vpn", "buy"))
    builder.row(create_action_button("Мои ключи", "my_keys", "keys"))
    builder.row(create_action_button("Поддержка", "support", "support"))
    builder.row(create_action_button("Назад", "back_to_menu", "back"))
    
    return builder.as_markup()

# =============================================================================
# Purchase Flow Functions
# =============================================================================

def plan_type_keyboard() -> InlineKeyboardMarkup:
    """Clean tariff selection keyboard"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_styled_button("Персональный (1 устройство) — 79 ₽", "type_personal", "secondary"))
    builder.row(create_styled_button("Для двоих — 129 ₽", "type_duo", "success"))
    builder.row(create_styled_button("Семья (5 устройств) — 199 ₽", "type_family", "primary"))
    builder.row(create_action_button("Назад", "back_to_menu", "back"))
    
    return builder.as_markup()

def period_keyboard() -> InlineKeyboardMarkup:
    """Clean period selection keyboard"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_styled_button("1 месяц", "period_1", "secondary"))
    builder.row(create_styled_button("6 месяцев (−18%)", "period_6", "success"))
    builder.row(create_styled_button("12 месяцев (−27%)", "period_12", "primary"))
    builder.row(create_action_button("Назад", "back_to_menu", "back"))
    
    return builder.as_markup()

def payment_methods_keyboard(price: str, auto_renewal: bool = False) -> InlineKeyboardMarkup:
    """Clean payment methods keyboard"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_styled_button(f"Telegram Stars — {price}", "pay_stars", "primary"))
    builder.row(create_styled_button(f"ЮKassa — {price}", "pay_yookassa", "secondary"))
    builder.row(create_styled_button(f"CryptoBot — {price}", "pay_cryptobot", "success"))
    
    # Auto-renewal toggle
    renewal_status = "вкл" if auto_renewal else "выкл"
    builder.row(create_styled_button(f"Автопродление: {renewal_status}", "toggle_auto_renewal", "secondary"))
    
    builder.row(create_action_button("Назад", "back_to_menu", "back"))
    
    return builder.as_markup()

# =============================================================================
# Support and Instructions Functions
# =============================================================================

def instructions_keyboard() -> InlineKeyboardMarkup:
    """Clean platform selection keyboard (without routers)"""
    builder = InlineKeyboardBuilder()
    
    platforms = [
        ("iOS / iPadOS", "os_ios"),
        ("Android", "os_android"),
        ("Windows", "os_windows"),
        ("macOS", "os_macos"),
        ("Linux", "os_linux")
    ]
    
    for name, callback in platforms:
        builder.row(create_styled_button(name, callback, "secondary"))
    
    builder.row(create_action_button("Назад", "back_to_menu", "back"))
    
    return builder.as_markup()

def legal_menu_keyboard() -> InlineKeyboardMarkup:
    """Clean legal documents keyboard with support link"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_styled_button("Документ оферты", "legal_offer", "secondary"))
    builder.row(create_styled_button("Политика конфиденциальности", "legal_privacy", "secondary"))
    builder.row(create_styled_button("Соглашение о регулярных платежах", "legal_recurrent", "secondary"))
    
    # Support link with pre-filled message
    support_text = "Здравствуйте! У меня вопрос по ByMeVPN. Помогите, пожалуйста!"
    encoded = quote_plus(support_text)
    support_url = f"https://t.me/ByMeVPN_support?text={encoded}"
    
    builder.row(
        create_action_button("Назад", "back_to_menu", "back"),
        InlineKeyboardButton(text="Поддержка", url=support_url)
    )
    
    return builder.as_markup()

# =============================================================================
# Additional Functions
# =============================================================================

def about_keyboard() -> InlineKeyboardMarkup:
    """Clean about section keyboard"""
    builder = InlineKeyboardBuilder()
    builder.row(create_action_button("Назад", "back_to_menu", "back"))
    return builder.as_markup()

def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Enhanced back to menu keyboard"""
    builder = InlineKeyboardBuilder()
    builder.row(create_action_button("Назад в главное меню", "back_to_menu", "back"))
    return builder.as_markup()

def referral_keyboard(referral_link: str) -> InlineKeyboardMarkup:
    """Enhanced referral program keyboard"""
    builder = InlineKeyboardBuilder()
    
    builder.row(InlineKeyboardButton(text="🔗 Поделиться ссылкой", switch_inline_query=referral_link))
    builder.row(create_action_button("Мои рефералы", "my_referrals", "keys"))
    builder.row(create_action_button("Назад", "back_to_menu", "back"))
    
    return builder.as_markup()

def referral_details_keyboard() -> InlineKeyboardMarkup:
    """Enhanced referral details navigation keyboard"""
    builder = InlineKeyboardBuilder()
    builder.row(create_action_button("Назад к программе", "partner", "partner"))
    builder.row(create_action_button("В главное меню", "back_to_menu", "back"))
    return builder.as_markup()

def instruction_navigation_keyboard() -> InlineKeyboardMarkup:
    """Enhanced navigation keyboard for instruction pages"""
    builder = InlineKeyboardBuilder()
    builder.row(create_action_button("Назад к платформам", "instructions", "info"))
    builder.row(create_action_button("В главное меню", "back_to_menu", "back"))
    return builder.as_markup()

def tournaments_keyboard() -> InlineKeyboardMarkup:
    """Clean tournaments keyboard"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_styled_button("Текущие турниры", "active_tournaments", "success"))
    builder.row(create_styled_button("Мои турниры", "my_tournaments", "primary"))
    builder.row(create_action_button("Назад", "back_to_menu", "back"))
    
    return builder.as_markup()

def tournament_details_keyboard() -> InlineKeyboardMarkup:
    """Enhanced tournament details keyboard"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_action_button("Участвовать", "join_tournament", "trial"))
    builder.row(create_action_button("Назад к турнирам", "tournaments", "info"))
    builder.row(create_action_button("В главное меню", "back_to_menu", "back"))
    
    return builder.as_markup()

def settings_keyboard() -> InlineKeyboardMarkup:
    """Clean settings keyboard"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_styled_button("Уведомления о новинках", "toggle_news", "primary"))
    builder.row(create_styled_button("Экспортировать ключи", "export_keys", "success"))
    builder.row(create_styled_button("Автопродление", "auto_renewal", "secondary"))
    builder.row(create_action_button("Назад", "back_to_menu", "back"))
    
    return builder.as_markup()

def admin_extended_keyboard() -> InlineKeyboardMarkup:
    """Clean admin keyboard"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_styled_button("Статистика", "admin_stats", "primary"))
    builder.row(create_styled_button("Рассылка", "admin_broadcast", "success"))
    builder.row(create_styled_button("Поиск пользователя", "admin_find_user", "secondary"))
    builder.row(create_action_button("Назад", "back_to_menu", "back"))
    
    return builder.as_markup()

# =============================================================================
# Legal Document Functions
# =============================================================================

def legal_download_keyboard(document_type: str, back_callback: str = "back_to_legal") -> InlineKeyboardMarkup:
    """Keyboard for downloading legal documents"""
    builder = InlineKeyboardBuilder()
    
    download_callbacks = {
        "offer": "legal_offer_download",
        "privacy": "legal_privacy_download", 
        "recurrent": "legal_recurrent_download"
    }
    
    download_texts = {
        "offer": "📄 Скачать договор .txt",
        "privacy": "📄 Скачать политику .txt",
        "recurrent": "📄 Скачать соглашение .txt"
    }
    
    callback = download_callbacks.get(document_type, "legal_download")
    text = download_texts.get(document_type, "📄 Скачать документ")
    
    builder.row(InlineKeyboardButton(text=text, callback_data=callback))
    builder.row(
        create_action_button("Назад к документам", back_callback, "back"),
        create_action_button("Поддержка", "support", "support")
    )
    
    return builder.as_markup()

# =============================================================================
# User Profile Functions
# =============================================================================

def user_profile_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for user profile actions"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_premium_button("Мои ключи", "my_keys", "keys", EmojiTheme.DEFAULT))
    builder.row(create_premium_button("История платежей", "payment_history", "chart", EmojiTheme.INFO))
    builder.row(create_premium_button("Настройки", "settings", "gear", EmojiTheme.INFO))
    builder.row(create_premium_button("Назад в меню", "back_to_menu", "back", EmojiTheme.DEFAULT))
    
    return builder.as_markup()

def user_settings_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for user settings"""
    builder = InlineKeyboardBuilder()
    
    builder.row(create_styled_button("Уведомления о новинках", "toggle_news", "primary"))
    builder.row(create_styled_button("Экспортировать ключи", "export_keys", "success"))
    builder.row(create_styled_button("Мои рефералы", "my_referrals", "secondary"))
    builder.row(create_action_button("Назад в профиль", "my_account", "back"))
    
    return builder.as_markup()

# =============================================================================
# Legacy Compatibility Functions
# =============================================================================

def main_menu_keyboard_legacy() -> InlineKeyboardMarkup:
    """Legacy compatibility function"""
    return main_menu_keyboard()

def expired_subscription_keyboard_legacy() -> InlineKeyboardMarkup:
    """Legacy compatibility function"""
    return expired_subscription_keyboard()

def plan_type_keyboard_legacy() -> InlineKeyboardMarkup:
    """Legacy compatibility function"""
    return plan_type_keyboard()

def period_keyboard_legacy() -> InlineKeyboardMarkup:
    """Legacy compatibility function"""
    return period_keyboard()

def payment_methods_keyboard_legacy(price: str) -> InlineKeyboardMarkup:
    """Legacy compatibility function"""
    return payment_methods_keyboard(price)

def instructions_keyboard_legacy() -> InlineKeyboardMarkup:
    """Legacy compatibility function"""
    return instructions_keyboard()

def legal_menu_keyboard_legacy() -> InlineKeyboardMarkup:
    """Legacy compatibility function"""
    return legal_menu_keyboard()

def about_keyboard_legacy() -> InlineKeyboardMarkup:
    """Legacy compatibility function"""
    return about_keyboard()

def back_to_menu_keyboard_legacy() -> InlineKeyboardMarkup:
    """Legacy compatibility function"""
    return back_to_menu_keyboard()