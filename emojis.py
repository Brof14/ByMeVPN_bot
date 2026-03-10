"""
Enhanced Emoji System for ByMeVPN Bot
Supports custom emoji IDs, fallback emojis, and themed collections
"""

import logging
from typing import Dict, Optional
from enum import Enum

logger = logging.getLogger(__name__)

class EmojiTheme(Enum):
    """Emoji themes for different UI elements"""
    DEFAULT = "default"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    INFO = "info"
    PREMIUM = "premium"

# Custom emoji IDs (replace with actual IDs from @BotFather)
CUSTOM_EMOJI_IDS = {
    # Main branding
    "logo": "5258093637450866522",  # 🛡️
    "shield": "5359523920120651432",  # 🛡️
    "star": "5436302963117137450",   # ⭐
    "key": "5280950718960781853",    # 🔑
    "rocket": "5258093637450866524", # 🚀
    
    # Status emojis
    "check": "5258093637450866525",  # ✅
    "cross": "5258093637450866534",  # ❌
    "warning": "5258093637450866535", # ⚠️
    "clock": "5258093637450866533",  # ⏰
    
    # Premium/Value
    "crown": "5258093637450866527",  # 👑
    "fire": "5258093637450866528",   # 🔥
    "diamond": "5258093637450866529", # 💎
    "gift": "5258093637450866526",   # 🎁
    
    # Communication
    "bell": "5258093637450866530",   # 🔔
    "phone": "5258093637450866531",  # 📞
    "mail": "5258093637450866532",   # 📧
    
    # Interface
    "gear": "5258093637450866536",   # ⚙️
    "chart": "5258093637450866537",   # 📊
    "users": "5258093637450866532",  # 👥
    "globe": "5258093637450866538",  # 🌐
    
    # Actions
    "arrow_right": "5258093637450866539",  # ➡️
    "arrow_left": "5258093637450866540",   # ⬅️
    "arrow_up": "5258093637450866541",     # ⬆️
    "download": "5258093637450866542",      # ⬇️
    "refresh": "5258093637450866543",      # 🔄
}

# Fallback emojis for compatibility
FALLBACK_EMOJIS = {
    "logo": "🛡️",
    "shield": "🛡️", 
    "star": "⭐",
    "key": "🔑",
    "rocket": "🚀",
    "check": "✅",
    "cross": "❌",
    "warning": "⚠️",
    "clock": "⏰",
    "crown": "👑",
    "fire": "🔥",
    "diamond": "💎",
    "gift": "🎁",
    "bell": "🔔",
    "phone": "📞",
    "mail": "📧",
    "gear": "⚙️",
    "chart": "📊",
    "users": "👥",
    "globe": "🌐",
    "arrow_right": "➡️",
    "arrow_left": "⬅️",
    "arrow_up": "⬆️",
    "download": "⬇️",
    "refresh": "🔄",
}

# Themed emoji collections
THEMED_EMOJIS = {
    EmojiTheme.DEFAULT: {
        "welcome": "logo",
        "menu": "star",
        "settings": "gear",
        "help": "phone",
        "back": "arrow_left",
    },
    EmojiTheme.SUCCESS: {
        "main": "check",
        "celebration": "gift",
        "complete": "diamond",
    },
    EmojiTheme.WARNING: {
        "main": "warning",
        "attention": "bell",
        "urgent": "clock",
    },
    EmojiTheme.ERROR: {
        "main": "cross",
        "critical": "warning",
        "fix": "gear",
    },
    EmojiTheme.INFO: {
        "main": "bell",
        "details": "chart",
        "learn": "globe",
    },
    EmojiTheme.PREMIUM: {
        "main": "crown",
        "value": "diamond",
        "exclusive": "fire",
        "reward": "gift",
    }
}

class EmojiManager:
    """Enhanced emoji management system"""
    
    def __init__(self, use_custom: bool = False):  # Temporarily disable custom emojis
        self.use_custom = use_custom
        self._validate_emojis()
    
    def _validate_emojis(self):
        """Validate emoji configuration"""
        required_emojis = ["logo", "star", "check", "warning", "cross"]
        for emoji in required_emojis:
            if emoji not in CUSTOM_EMOJI_IDS:
                logger.warning(f"Missing custom emoji ID: {emoji}")
            if emoji not in FALLBACK_EMOJIS:
                logger.error(f"Missing fallback emoji: {emoji}")
    
    def get_emoji(self, emoji_type: str, theme: EmojiTheme = EmojiTheme.DEFAULT, 
                  fallback_to_default: bool = True) -> str:
        """
        Get emoji by type with theme support
        
        Args:
            emoji_type: Type of emoji (e.g., 'logo', 'check')
            theme: Emoji theme for context
            fallback_to_default: Whether to fallback to default theme if not found
            
        Returns:
            Formatted emoji string
        """
        try:
            # Try themed emoji first
            if theme != EmojiTheme.DEFAULT:
                themed_emoji = THEMED_EMOJIS.get(theme, {}).get(emoji_type)
                if themed_emoji:
                    emoji_type = themed_emoji
            
            # Get the actual emoji
            if self.use_custom and emoji_type in CUSTOM_EMOJI_IDS:
                # Use correct format for custom emoji
                return f"<tg-emoji emoji-id=\"{CUSTOM_EMOJI_IDS[emoji_type]}\"></tg-emoji>"
            elif emoji_type in FALLBACK_EMOJIS:
                return FALLBACK_EMOJIS[emoji_type]
            elif fallback_to_default:
                return FALLBACK_EMOJIS.get("star", "⭐")
            else:
                return ""
                
        except Exception as e:
            logger.error(f"Error getting emoji {emoji_type}: {e}")
            return FALLBACK_EMOJIS.get("star", "⭐")
    
    def get_button_emoji(self, action: str, theme: EmojiTheme = EmojiTheme.DEFAULT) -> str:
        """Get emoji specifically for buttons"""
        button_mappings = {
            "buy": "star",
            "start": "rocket",
            "settings": "gear",
            "help": "phone",
            "back": "arrow_left",
            "next": "arrow_right",
            "refresh": "refresh",
            "download": "download",
            "profile": "users",
            "keys": "key",
            "payment": "diamond",
            "support": "phone",
            "about": "info",
        }
        
        emoji_type = button_mappings.get(action, "star")
        return self.get_emoji(emoji_type, theme)
    
    def get_status_emoji(self, status: str) -> str:
        """Get emoji for status indicators"""
        status_mappings = {
            "active": "check",
            "expired": "cross",
            "pending": "clock",
            "warning": "warning",
            "premium": "crown",
            "trial": "gift",
            "error": "cross",
            "success": "check",
            "loading": "refresh",
        }
        
        emoji_type = status_mappings.get(status, "star")
        
        # Determine theme based on status
        if status in ["active", "success"]:
            theme = EmojiTheme.SUCCESS
        elif status in ["expired", "error"]:
            theme = EmojiTheme.ERROR
        elif status in ["pending", "warning"]:
            theme = EmojiTheme.WARNING
        elif status in ["premium"]:
            theme = EmojiTheme.PREMIUM
        else:
            theme = EmojiTheme.DEFAULT
            
        return self.get_emoji(emoji_type, theme)
    
    def format_text_with_emojis(self, text: str, theme: EmojiTheme = EmojiTheme.DEFAULT) -> str:
        """
        Replace emoji placeholders in text with actual emojis
        
        Example: "Welcome {logo} to {star} VPN" -> "Welcome 🛡️ to ⭐ VPN"
        """
        import re
        
        # Replace {emoji_name} patterns
        def replace_placeholder(match):
            emoji_name = match.group(1)
            return self.get_emoji(emoji_name, theme)
        
        # Pattern to match {emoji_name}
        pattern = r'\{([^}]+)\}'
        return re.sub(pattern, replace_placeholder, text)
    
    def create_progress_bar_emoji(self, current: int, total: int, length: int = 10) -> str:
        """Create a visual progress bar using emojis"""
        try:
            if total <= 0:
                return self.get_emoji("check", EmojiTheme.SUCCESS) * length
            
            filled = int((current / total) * length)
            empty = length - filled
            
            filled_emoji = self.get_emoji("diamond", EmojiTheme.PREMIUM)
            empty_emoji = "▫️"
            
            return f"{filled_emoji * filled}{empty_emoji * empty}"
        except Exception as e:
            logger.error(f"Error creating progress bar: {e}")
            return "▫️" * length

# Global emoji manager instance
emoji_manager = EmojiManager()

# Convenience functions for backward compatibility
def get_emoji(emoji_type: str, theme: EmojiTheme = EmojiTheme.DEFAULT) -> str:
    """Get emoji - convenience function"""
    return emoji_manager.get_emoji(emoji_type, theme)

def get_button_emoji(action: str, theme: EmojiTheme = EmojiTheme.DEFAULT) -> str:
    """Get button emoji - convenience function"""
    return emoji_manager.get_button_emoji(action, theme)

def get_status_emoji(status: str) -> str:
    """Get status emoji - convenience function"""
    return emoji_manager.get_status_emoji(status)

def format_text_with_emojis(text: str, theme: EmojiTheme = EmojiTheme.DEFAULT) -> str:
    """Format text with emojis - convenience function"""
    return emoji_manager.format_text_with_emojis(text, theme)

def create_progress_bar_emoji(current: int, total: int, length: int = 10) -> str:
    """Create progress bar - convenience function"""
    return emoji_manager.create_progress_bar_emoji(current, total, length)