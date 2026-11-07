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
        
        logger.info("Referral click processed: referrer=%d, referred=%d", referrer_id, referred_id)
        return True
    except Exception as e:
        logger.error(f"Error processing referral click: {e}")
        return False


async def award_referral_signup_bonus(bot, referrer_id: int, referred_id: int) -> bool:
    """Grant the existing +15-day signup reward with a retry-safe target expiry."""
    from database import get_referrer, has_referral_event, get_user_keys, add_referral_event
    from subscription import deliver_key
    from constants import DEFAULT_DEVICE_LIMIT
    from constants import REFERRAL_SIGNUP_BONUS_DAYS

    if await get_referrer(referred_id) != referrer_id:
        return False
    if await has_referral_event(referrer_id, referred_id, "registration_bonus"):
        return True

    keys = await get_user_keys(referrer_id)
    latest_key = max(keys, key=lambda item: item.get("expiry", 0)) if keys else None
    devices = latest_key.get("limit_ip", DEFAULT_DEVICE_LIMIT) if latest_key else DEFAULT_DEVICE_LIMIT
    delivered = await deliver_key(
        bot=bot, user_id=referrer_id, chat_id=referrer_id,
        config_name=f"ByMeVPN_{referrer_id}", days=REFERRAL_SIGNUP_BONUS_DAYS, limit_ip=devices,
        # amount=0 prevents a payment record; the deterministic intent makes
        # retries safe if the panel succeeded but the DB/event response timed out.
        is_paid=True, amount=0, method="ref_signup_bonus",
        payload=f"ref_signup_{referrer_id}_{referred_id}", extend_existing=True,
    )
    if not delivered:
        return False
    inserted = await add_referral_event(
        referrer_id=referrer_id, referred_id=referred_id,
        event_type="registration_bonus", days_awarded=REFERRAL_SIGNUP_BONUS_DAYS,
        description="New user registered via referral link",
    )
    if inserted:
        return True
    # Another worker may have recorded the same reward while delivery ran.
    return await has_referral_event(referrer_id, referred_id, "registration_bonus")


async def retry_pending_signup_bonuses(bot, referrer_id: int) -> int:
    """Retry outstanding signup rewards when the referrer opens their bonus page."""
    from database import get_referrals_missing_signup_bonus
    pending = await get_referrals_missing_signup_bonus(referrer_id)
    completed = 0
    for referred_id in pending:
        try:
            completed += bool(await award_referral_signup_bonus(bot, referrer_id, referred_id))
        except Exception:
            logger.exception("Could not retry referral signup bonus for referrer %d", referrer_id)
    return len(pending) - completed


async def process_pending_referral_signup_rewards(bot, limit: int = 10) -> int:
    """Retry a bounded batch of newly registered signup rewards automatically."""
    from database import get_pending_signup_bonus_retries, mark_signup_bonus_retry_attempt

    completed = 0
    for referrer_id, referred_id in await get_pending_signup_bonus_retries(limit=limit):
        try:
            await mark_signup_bonus_retry_attempt(referrer_id, referred_id)
            completed += bool(await award_referral_signup_bonus(bot, referrer_id, referred_id))
        except Exception:
            logger.exception("Signup bonus retry failed: referrer=%s referred=%s", referrer_id, referred_id)
    return completed


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
        from constants import REFERRAL_FIRST_PAYMENT_BONUS_DAYS
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
        
        bonus_days = REFERRAL_FIRST_PAYMENT_BONUS_DAYS
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


# ---------------------------------------------------------------------------
# Canonical referral reward for a qualifying payment (single path)
# ---------------------------------------------------------------------------

async def award_referral_for_payment(
    user_id: int,
    payment_ref: str,
    amount: int = 50,
    is_trial: bool = False,
    bot=None,
) -> bool:
    """
    The ONE place where a payment may award a referral reward.

    Guarantees:
    - one qualifying payment → at most one reward through ref_bonus_claims;
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
        from database import get_referrer, record_referral_payment_reward
        from constants import REFERRAL_FIRST_PAYMENT_BONUS_DAYS

        referrer_id = await get_referrer(user_id)
        if not referrer_id:
            return False

        bonus_added = await record_referral_payment_reward(referrer_id, user_id, payment_ref)
        # Repair the reporting event on every retry: the reward insert and the
        # event are separate commits, so a crash can occur between them.
        from database import add_referral_event
        await add_referral_event(
            referrer_id=referrer_id,
            referred_id=user_id,
            event_type="payment_bonus",
            days_awarded=REFERRAL_FIRST_PAYMENT_BONUS_DAYS,
            description=f"First paid subscription; payment={payment_ref}",
        )
        if not bonus_added:
            logger.info(
                "Referral reward already exists for referrer %d / referred %d (payment %s)",
                referrer_id, user_id, payment_ref,
            )
            if bot is not None:
                await process_pending_referral_payment_rewards(bot)
            return False

        logger.info(
            "Referral reward +%d days recorded: referrer=%d, referred=%d, payment=%s",
            REFERRAL_FIRST_PAYMENT_BONUS_DAYS, referrer_id, user_id, payment_ref,
        )

        if bot is not None:
            await process_pending_referral_payment_rewards(bot)
        return True
    except Exception as e:
        logger.error("Error awarding referral reward for user %d: %s", user_id, e)
        return False


async def process_pending_referral_payment_rewards(bot, limit: int = 100) -> int:
    """Automatically grant +30 days for paid referrals, retrying safely after outages."""
    from database import (
        get_pending_referral_payment_rewards, get_user_keys, mark_ref_bonus_claimed,
        add_referral_event, mark_referral_payment_reward_attempt,
    )
    from subscription import deliver_key
    from constants import DEFAULT_DEVICE_LIMIT, REFERRAL_FIRST_PAYMENT_BONUS_DAYS

    delivered_count = 0
    for reward in await get_pending_referral_payment_rewards(limit=limit):
        referrer_id = int(reward["referrer_id"])
        referred_id = int(reward["referred_id"])
        try:
            await mark_referral_payment_reward_attempt(referrer_id, referred_id)
            await add_referral_event(
                referrer_id=referrer_id, referred_id=referred_id,
                event_type="payment_bonus", days_awarded=REFERRAL_FIRST_PAYMENT_BONUS_DAYS,
                description=f"First paid subscription; payment={reward['payment_ref']}",
            )
            keys = await get_user_keys(referrer_id)
            latest = max(keys, key=lambda key: key.get("expiry", 0)) if keys else None
            success = await deliver_key(
                bot=bot, user_id=referrer_id, chat_id=referrer_id,
                config_name=f"ByMeVPN_{referrer_id}", days=REFERRAL_FIRST_PAYMENT_BONUS_DAYS,
                limit_ip=latest.get("limit_ip", DEFAULT_DEVICE_LIMIT) if latest else DEFAULT_DEVICE_LIMIT,
                # A fixed provisioning intent prevents duplicate extensions on retry.
                # amount=0 ensures this bonus does not become a payment record.
                is_paid=True, amount=0, method="ref_bonus",
                payload=f"ref_bonus_{referrer_id}_{referred_id}", extend_existing=True,
            )
            if not success:
                logger.warning("Paid referral bonus remains pending: referrer=%s referred=%s", referrer_id, referred_id)
                continue
            await mark_ref_bonus_claimed(referrer_id, referred_id)
            delivered_count += 1
            try:
                await bot.send_message(
                    referrer_id,
                    "🎉 <b>Ваш друг оформил первую платную подписку!</b>\n\n"
                    f"Бонус <b>+{REFERRAL_FIRST_PAYMENT_BONUS_DAYS} дней ByMeVPN</b> уже автоматически начислен на ваш ключ.",
                    parse_mode="HTML",
                )
            except Exception:
                logger.info("Paid referral reward delivered; confirmation message failed for %s", referrer_id)
        except Exception:
            logger.exception("Automatic paid referral bonus failed: referrer=%s referred=%s", referrer_id, referred_id)
    return delivered_count
