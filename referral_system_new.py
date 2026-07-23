"""
Referral System Module
Handles referral tracking, bonuses, and statistics.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from database import (
    add_referral_event, get_referral_events, get_referral_stats,
    ensure_referral_balance, add_referral_earning, get_referral_balance,
    log_referral_click, get_referral_clicks_count
)

logger = logging.getLogger(__name__)


async def process_referral_click(referrer_id: int, referred_id: int, user_agent: str = None, ip_address: str = None) -> bool:
    """
    Process a referral click when a user clicks on a referral link.
    """
    try:
        # Log the click
        click_id = await log_referral_click(referrer_id, user_agent, ip_address)
        
        # Add referral event for the click with 15 days bonus
        await add_referral_event(
            referrer_id=referrer_id,
            referred_id=referred_id,
            event_type="trial_pending",
            days_awarded=15
        )
        
        logger.info(f"Referral click processed: referrer={referrer_id}, referred={referred_id}, days_awarded=15")
        return True
    except Exception as e:
        logger.error(f"Error processing referral click: {e}")
        return False


async def claim_referral_bonus(bot, referrer_id: int, referred_id: int, bonus_type: str) -> bool:
    """
    Claim a referral bonus for the referrer.
    """
    try:
        # Check if the bonus has already been claimed
        events = await get_referral_events(referrer_id, limit=50)
        for event in events:
            if event["referred_id"] == referred_id and event["event_type"] == bonus_type:
                if event["days_awarded"] > 0:
                    logger.info(f"Bonus already claimed: referrer={referrer_id}, referred={referred_id}")
                    return False
        
        # Award the bonus
        days_to_award = 5 if bonus_type == "trial_bonus" else 0
        
        await add_referral_event(
            referrer_id=referrer_id,
            referred_id=referred_id,
            event_type=bonus_type,
            days_awarded=days_to_award
        )
        
        # Ensure referrer has balance
        await ensure_referral_balance(referrer_id)
        
        logger.info(f"Referral bonus claimed: referrer={referrer_id}, referred={referred_id}, type={bonus_type}")
        return True
    except Exception as e:
        logger.error(f"Error claiming referral bonus: {e}")
        return False


async def get_referral_stats_with_free_days(user_id: int) -> Dict[str, Any]:
    """
    Get referral statistics including free days awarded.
    """
    try:
        from database import get_referral_events, get_referral_balance, get_referral_clicks_count
        
        # Get referral events to count free days
        events = await get_referral_events(user_id, limit=100)
        free_days = sum(event["days_awarded"] for event in events)
        
        # Get referral balance
        balance = await get_referral_balance(user_id)
        
        # Count total referrals (unique referred users)
        total_referrals = len(set(event["referred_id"] for event in events))
        
        # Count paid referrals (those with payment_bonus events)
        paid_referrals = len(set(
            event["referred_id"] for event in events 
            if event["event_type"] == "payment_bonus"
        ))
        
        # Get click count
        click_count = await get_referral_clicks_count(user_id)
        
        stats = {
            "total": total_referrals,
            "paid": paid_referrals,
            "free_days": free_days,
            "total_earned": balance.get("total_earned", 0),
            "balance": balance.get("balance", 0),
            "clicks": click_count
        }
        
        return stats
    except Exception as e:
        logger.error(f"Error getting referral stats: {e}")
        return {
            "total": 0,
            "paid": 0,
            "free_days": 0,
            "total_earned": 0,
            "balance": 0,
            "clicks": 0
        }


async def validate_referral_link(referral_code: str) -> Optional[int]:
    """
    Validate a referral code and return the referrer ID.
    """
    try:
        # The referral code should be the user ID
        referrer_id = int(referral_code)
        return referrer_id
    except (ValueError, TypeError):
        logger.error(f"Invalid referral code: {referral_code}")
        return None


async def get_referral_link(user_id: int, bot_username: str) -> str:
    """
    Generate a referral link for the user.
    """
    return f"https://t.me/{bot_username}?start={user_id}"


async def process_payment_referral_bonus(user_id: int, amount: int, bot) -> bool:
    """
    Process referral bonus when a referred user makes a payment.
    Awards 30 days to the referrer for the first payment.
    """
    try:
        from database import get_referrer, get_referral_events, get_user_keys, extend_key
        import time
        
        # Get referrer for this user
        referrer_id = await get_referrer(user_id)
        if not referrer_id:
            logger.info(f"No referrer found for user {user_id}")
            return False
        
        # Check if this is the first payment bonus for this referral
        events = await get_referral_events(referrer_id, limit=100)
        for event in events:
            if event["referred_id"] == user_id and event["event_type"] == "payment_bonus":
                logger.info(f"Payment bonus already claimed for referrer {referrer_id}, referred {user_id}")
                return False
        
        # Award 30 days to referrer's key
        bonus_days = 30
        referrer_keys = await get_user_keys(referrer_id)
        current_time = int(time.time())
        
        if referrer_keys:
            # Extend the first active or recently expired key
            extendable_keys = [k for k in referrer_keys if k.get("expiry", 0) > current_time - 7*86400]
            if extendable_keys:
                extendable_keys.sort(key=lambda k: k.get("expiry", 0), reverse=True)
                key_to_extend = extendable_keys[0]
                key_id = key_to_extend["id"]
                
                success = await extend_key(key_id, bonus_days)
                if success:
                    logger.info(f"Extended referrer {referrer_id} key {key_id} by {bonus_days} days")
                else:
                    logger.warning(f"Failed to extend referrer {referrer_id} key {key_id}")
            else:
                # No extendable keys - create new one or notify
                logger.info(f"Referrer {referrer_id} has no extendable keys")
        else:
            # Referrer has no keys at all
            logger.info(f"Referrer {referrer_id} has no keys")
        
        # Log the payment bonus event
        await add_referral_event(
            referrer_id=referrer_id,
            referred_id=user_id,
            event_type="payment_bonus",
            days_awarded=bonus_days
        )
        
        # Notify referrer
        try:
            await bot.send_message(
                referrer_id,
                f"💰 <b>Бонус за оплату реферала!</b>\n\n"
                f"Ваш реферал оплатил подписку\n"
                f"� Ваш ключ продлён на {bonus_days} дней\n"
                f"💚 Продолжайте приглашать друзей!",
                parse_mode="HTML"
            )
        except Exception as notify_error:
            logger.error("Failed to notify referrer about payment bonus: %s", notify_error)
        
        logger.info(f"Payment bonus awarded: referrer={referrer_id}, referred={user_id}, days={bonus_days}")
        return True
    except Exception as e:
        logger.error("Error processing payment referral bonus: %s", e)
        return False
