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

from constants import REFERRAL_CLICK_BONUS_DAYS, REFERRAL_CASH_REWARD_RUB

logger = logging.getLogger(__name__)


async def process_referral_click(referrer_id: int, referred_id: int, user_agent: str = None, ip_address: str = None) -> bool:
    """
    Process a referral click when a user clicks on a referral link.
    """
    try:
        # Log the click
        click_id = await log_referral_click(referrer_id, user_agent, ip_address)
        
        # The click reward is separate from the cash reward for first paid conversion.
        await add_referral_event(
            referrer_id=referrer_id,
            referred_id=referred_id,
            event_type="trial_pending",
            days_awarded=REFERRAL_CLICK_BONUS_DAYS,
            description="Referral link click bonus",
        )
        
        logger.info(
            "Referral click tracked: referrer=%d, referred=%d, days=%d",
            referrer_id, referred_id, REFERRAL_CLICK_BONUS_DAYS,
        )
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


# ---------------------------------------------------------------------------
# Canonical referral reward for a qualifying payment (single path)
# ---------------------------------------------------------------------------

async def award_referral_for_payment(
    user_id: int,
    payment_ref: str,
    amount: int = REFERRAL_CASH_REWARD_RUB,
    is_trial: bool = False,
    bot=None,
) -> bool:
    """
    The ONE place where a payment may award a referral reward.

    Guarantees:
    - one qualifying payment → at most one reward: referral_earnings has
      UNIQUE(referrer_id, referred_id) and add_referral_earning is idempotent;
    - intro-trial payments (1 ₽) are never a paid conversion → no reward;
    - duplicate webhooks / retries / renewals → no second reward.

    All payment handlers (YooKassa webhook, CryptoBot monitor, Telegram Stars)
    must call this instead of implementing their own bonus logic.
    """
    if is_trial:
        logger.info(
            "Referral reward skipped: intro-trial payment for user %d is not a paid conversion",
            user_id,
        )
        return False

    try:
        from database import get_referrer, add_referral_earning

        referrer_id = await get_referrer(user_id)
        if not referrer_id:
            return False

        bonus_added = await add_referral_earning(referrer_id, user_id, amount, payment_ref)
        if not bonus_added:
            logger.info(
                "Referral reward already exists for referrer %d / referred %d (payment %s)",
                referrer_id, user_id, payment_ref,
            )
            return False

        logger.info(
            "Referral reward +%d₽ awarded: referrer=%d, referred=%d, payment=%s",
            amount, referrer_id, user_id, payment_ref,
        )

        if bot is not None:
            try:
                await bot.send_message(
                    referrer_id,
                    "🎉 <b>Поздравляем!</b>\n\n"
                    "Ваш приглашённый оформил платную подписку.\n"
                    f"Начислено: +{amount} ₽\n"
                    "Текущий баланс обновлён в партнёрской программе.",
                    parse_mode="HTML",
                )
            except Exception as notify_error:
                logger.error("Failed to notify referrer %d: %s", referrer_id, notify_error)
        return True
    except Exception as e:
        logger.error("Error awarding referral reward for user %d: %s", user_id, e)
        return False
