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
        
        # Track the referral event without claiming a reward was granted.
        # The caller records click_bonus_granted only after 3x-ui confirms the
        # exact expiry update, preventing statistics from overstating rewards.
        await add_referral_event(
            referrer_id=referrer_id,
            referred_id=referred_id,
            event_type="referral_click",
            days_awarded=0,
            description="Referral click tracked; no reward recorded yet",
        )

        logger.info(
            "Referral click tracked: referrer=%d, referred=%d",
            referrer_id, referred_id,
        )
        return True
    except Exception as e:
        logger.error(f"Error processing referral click: {e}")
        return False


async def claim_referral_bonus(bot, referrer_id: int, referred_id: int, bonus_type: str) -> bool:
    """Grant the trial-signup day bonus only after confirmed payment and XUI expiry sync."""
    from constants import REFERRAL_TRIAL_BONUS_DAYS
    from database import (
        get_referrer, get_user_keys, extend_key, set_key_expiry,
        add_referral_event, reserve_referral_bonus, finalize_referral_bonus,
    )
    from xui_client import update_xui_user
    from utils import notify_admins

    if bonus_type != "trial_bonus" or referrer_id <= 0 or referred_id <= 0 or referrer_id == referred_id:
        return False

    # Do not trust a callback/deep-link parameter alone: the database relationship
    # is authoritative for who referred this user.
    if await get_referrer(referred_id) != referrer_id:
        logger.warning("Referral trial bonus rejected: relationship mismatch for user %d", referred_id)
        return False

    if not await reserve_referral_bonus(referrer_id, referred_id, bonus_type):
        logger.info("Referral trial bonus already claimed or reserved for %d -> %d", referrer_id, referred_id)
        return False

    key_id = None
    old_expiry = None
    old_limit = 2
    db_extended = False
    panel_update_started = False
    try:
        keys = await get_user_keys(referrer_id)
        now = int(__import__("time").time())
        active_key = next(
            (key for key in keys if int(key.get("expiry") or 0) > now),
            None,
        )
        if not active_key:
            # No active key means there is nowhere to apply the days. Release the
            # reservation; no bonus is falsely recorded as granted.
            await finalize_referral_bonus(referrer_id, referred_id, success=False)
            try:
                await bot.send_message(
                    referrer_id,
                    "Друг активировал пробный период, но у вас нет активного ключа, "
                    "поэтому бонусные дни пока не начислены. Обратитесь в поддержку после продления подписки.",
                )
            except Exception:
                pass
            return False

        key_id = int(active_key["id"])
        old_expiry = int(active_key.get("expiry") or 0)
        old_limit = int(active_key.get("limit_ip") or 2)
        db_extended = await extend_key(key_id, REFERRAL_TRIAL_BONUS_DAYS)
        if not db_extended:
            await finalize_referral_bonus(referrer_id, referred_id, success=False)
            return False

        updated_key = next(
            (key for key in await get_user_keys(referrer_id) if int(key.get("id") or 0) == key_id),
            None,
        )
        if not updated_key:
            rollback_ok = await set_key_expiry(key_id, old_expiry, old_limit)
            if rollback_ok:
                await finalize_referral_bonus(referrer_id, referred_id, success=False)
            await notify_admins(
                bot,
                f"⚠️ Не удалось проверить ключ для реферального бонуса. "
                f"Пригласивший <code>{referrer_id}</code>, приглашённый <code>{referred_id}</code>; "
                f"откат БД: {'OK' if rollback_ok else 'НЕ ПОДТВЕРЖДЁН'}.",
                parse_mode="HTML",
            )
            return False

        panel_update_started = True
        xui_result = await update_xui_user(
            referrer_id,
            new_expiry_ms=int(updated_key["expiry"]) * 1000,
            limit_ip=old_limit,
        )
        if not xui_result:
            # The panel may have applied the absolute expiry before its response
            # was lost. Keep the reservation blocked until an admin reconciles it.
            rollback_ok = await set_key_expiry(key_id, old_expiry, old_limit)
            await notify_admins(
                bot,
                f"⚠️ Не удалось подтвердить реферальное продление в 3x-ui. "
                f"Пригласивший <code>{referrer_id}</code>, приглашённый <code>{referred_id}</code>; "
                f"откат БД: {'OK' if rollback_ok else 'НЕ ПОДТВЕРЖДЁН'}. "
                "Резерв бонуса оставлен заблокированным до ручной сверки.",
                parse_mode="HTML",
            )
            return False

        # Mark the reservation complete before writing the informational event.
        # If the process crashes now, a retry still cannot extend the key twice.
        await finalize_referral_bonus(referrer_id, referred_id, success=True)
        try:
            await add_referral_event(
                referrer_id=referrer_id,
                referred_id=referred_id,
                event_type=bonus_type,
                days_awarded=REFERRAL_TRIAL_BONUS_DAYS,
                description="Trial payment succeeded; active referrer key expiry confirmed in 3x-ui",
            )
        except Exception:
            logger.exception("Referral bonus granted but event ledger write failed")
            await notify_admins(
                bot,
                f"⚠️ Реферальный бонус применён, но не записан в журнал. "
                f"Пригласивший <code>{referrer_id}</code>, приглашённый <code>{referred_id}</code>.",
                parse_mode="HTML",
            )

        try:
            await bot.send_message(
                referrer_id,
                f"🎁 Друг активировал пробный период — ваш активный ключ продлён на "
                f"{REFERRAL_TRIAL_BONUS_DAYS} дней.",
            )
        except Exception as notify_error:
            logger.warning("Referral trial bonus granted but notification failed (%s)", type(notify_error).__name__)

        logger.info(
            "Referral trial bonus granted: referrer=%d referred=%d days=%d",
            referrer_id, referred_id, REFERRAL_TRIAL_BONUS_DAYS,
        )
        return True
    except Exception as exc:
        logger.error("Referral trial bonus failed (%s)", type(exc).__name__)
        rollback_ok = True
        if db_extended and key_id is not None and old_expiry is not None:
            rollback_ok = await set_key_expiry(key_id, old_expiry, old_limit)
        if panel_update_started:
            # Even if the DB rollback succeeded, the panel may already have
            # applied the target. Do not release the claim for an automatic retry.
            await notify_admins(
                bot,
                f"⚠️ Неоднозначный результат реферального продления. "
                f"Пригласивший <code>{referrer_id}</code>, приглашённый <code>{referred_id}</code>; "
                f"откат БД: {'OK' if rollback_ok else 'НЕ ПОДТВЕРЖДЁН'}. "
                "Резерв бонуса заблокирован до ручной сверки.",
                parse_mode="HTML",
            )
        elif rollback_ok:
            await finalize_referral_bonus(referrer_id, referred_id, success=False)
        else:
            await notify_admins(
                bot,
                f"⚠️ Ошибка реферального бонуса и откат БД не подтверждён. "
                f"Пригласивший <code>{referrer_id}</code>, приглашённый <code>{referred_id}</code>. "
                "Требуется ручная сверка; повторный бонус заблокирован.",
                parse_mode="HTML",
            )
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
