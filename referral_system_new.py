"""
Referral System Module
Handles referral tracking, bonuses, and statistics.
"""

import logging
import time

from database import log_referral_click

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
    from database import get_referrer, has_referral_event, get_user_keys, add_referral_event, complete_signup_bonus_retry
    from subscription import deliver_key
    from constants import DEFAULT_DEVICE_LIMIT
    from constants import REFERRAL_SIGNUP_BONUS_DAYS

    if await get_referrer(referred_id) != referrer_id:
        return False
    if await has_referral_event(referrer_id, referred_id, "registration_bonus"):
        await complete_signup_bonus_retry(referrer_id, referred_id)
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
        await complete_signup_bonus_retry(referrer_id, referred_id)
        return True
    # Another worker may have recorded the same reward while delivery ran.
    recorded = await has_referral_event(referrer_id, referred_id, "registration_bonus")
    if recorded:
        await complete_signup_bonus_retry(referrer_id, referred_id)
    return recorded


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


# ---------------------------------------------------------------------------
# Canonical referral reward for the first successful subscription payment
# ---------------------------------------------------------------------------

async def award_referral_for_payment(
    user_id: int,
    payment_ref: str,
    amount: int = 0,
    is_trial: bool = False,
    bot=None,
) -> bool:
    """
    The ONE place where a payment may award a referral reward.

    Guarantees:
    - any first verified successful subscription payment (>0), including 1 ₽ trial,
      creates at most one reward per referred customer;
    - duplicate webhooks / retries / renewals → no second reward.
    - callers must supply the settled positive provider amount.

    All payment handlers (YooKassa webhook, CryptoBot monitor, Telegram Stars)
    must call this instead of implementing their own bonus logic.
    """
    if amount <= 0 or not payment_ref:
        logger.warning("Ignoring unverified or zero-value referral payment for user %d", user_id)
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
            description=f"First settled subscription payment; payment={payment_ref}",
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
    retry_before_ms = time.time_ns() // 1_000_000 - 60_000
    for reward in await get_pending_referral_payment_rewards(limit=limit, retry_before_ms=retry_before_ms):
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
                    "🎉 <b>Друг совершил первую оплату!</b>\n\n"
                    f"Бонус <b>+{REFERRAL_FIRST_PAYMENT_BONUS_DAYS} дней ByMeVPN</b> уже автоматически начислен на ваш ключ.",
                    parse_mode="HTML",
                )
            except Exception:
                logger.info("Paid referral reward delivered; confirmation message failed for %s", referrer_id)
        except Exception:
            logger.exception("Automatic paid referral bonus failed: referrer=%s referred=%s", referrer_id, referred_id)
    return delivered_count
