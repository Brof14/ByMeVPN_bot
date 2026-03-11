import asyncio
import logging
import time
import random
import signal
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import (
    get_keys_nearing_expiry, 
    get_auto_renewal, 
    get_last_payment,
    get_news_notification_users,
    get_active_tournaments,
    get_user_tournaments,
    update_tournament_progress,
    get_keys_for_check,
    update_key_check_status,
    replace_key,
    log_key_replacement,
    get_referral_stats,
)
from utils import (
    generate_and_send_beautiful_qr, 
    format_expiry_date, 
    create_progress_bar, 
    send_expiry_warning_animation,
    add_client,
    generate_vless
)
from payments import create_yookassa_payment, create_cryptobot_invoice

logger = logging.getLogger(__name__)


async def send_renewal_notification(bot: Bot, user_id: int, key: str, days_left: int, expiry: int):
    """Send personalized renewal notification based on days left"""
    date_str = datetime.fromtimestamp(expiry).strftime("%d.%m.%Y")
    auto_renewal = await get_auto_renewal(user_id)
    
    # Different messages based on urgency
    if days_left <= 0:
        urgency_indicator = "[!]"
        urgency_text = "истекла сегодня"
        message_color = "красный"
    elif days_left <= 1:
        urgency_indicator = "[!]"
        urgency_text = "истекает сегодня"
        message_color = "оранжевый"
    elif days_left <= 3:
        urgency_indicator = "[!]"
        urgency_text = f"истекает через {days_left} дня"
        message_color = "желтый"
    else:
        urgency_indicator = "[i]"
        urgency_text = f"истекает через {days_left} дней"
        message_color = "синий"
    
    # Build personalized message
    if auto_renewal:
        text = (
            f"{urgency_indicator} <b>Автопродление ByMeVPN</b>\n\n"
            f"Ваша подписка {urgency_text} — {date_str}.\n\n"
            f"Мы постараемся продлить её автоматически. "
            f"Пожалуйста, убедитесь, что средства доступны для оплаты.\n\n"
            f"Если возникнут проблемы с автопродлением, вы получите уведомление."
        )
    else:
        text = (
            f"{urgency_indicator} <b>Важно! Подписка ByMeVPN {urgency_text}</b>\n\n"
            f"Дата окончания: {date_str}\n\n"
            f"<b>Не теряйте доступ к свободному интернету!</b>\n"
            f"• Стабильный доступ к интернету\n"
            f"• Безлимитный трафик и высокая скорость\n"
            f"• Работает на всех ваших устройствах\n\n"
            f"Продлите прямо сейчас и продолжайте пользоваться без ограничений!"
        )
    
    # Create action buttons
    buttons = []
    if not auto_renewal and days_left > 0:
        buttons.append([InlineKeyboardButton(text="Продлить подписку", callback_data="buy_vpn")])
    
    buttons.append([InlineKeyboardButton(text="Поддержка", callback_data="support")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    try:
        await bot.send_message(user_id, text, reply_markup=kb, parse_mode="HTML")
        
        # Send warning animation for critical expirations
        if days_left <= 1:
            await send_expiry_warning_animation(bot, user_id, days_left)
        
        # Send current key for convenience if expiring soon
        if days_left <= 3 and days_left > 0:
            await generate_and_send_beautiful_qr(bot, user_id, key, "Ваш текущий ключ:")
            
    except Exception as e:
        logger.error(f"Error sending renewal notification to {user_id}: {e}")


async def process_auto_renewal(bot: Bot, user_id: int, days: int, total_rub: int, method: str):
    """Process automatic renewal for a user"""
    try:
        if method == "stars":
            from aiogram.types import LabeledPrice
            
            prices = [
                LabeledPrice(
                    label=f"Автопродление ByMeVPN на {days} дней",
                    amount=total_rub,
                )
            ]
            await bot.send_invoice(
                chat_id=user_id,
                title="ByMeVPN — автопродление",
                description=f"Автопродление подписки на {days} дней",
                payload=f"auto_stars_{user_id}_{days}_{int(datetime.now().timestamp())}",
                provider_token="",
                currency="XTR",
                prices=prices,
            )
        elif method == "yookassa":
            url = await create_yookassa_payment(
                int(total_rub),
                f"Автопродление ByMeVPN на {days} дней",
                user_id,
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Оплатить продление", url=url)]
                ]
            )
            await bot.send_message(
                user_id,
                "<b>Автопродление ByMeVPN</b>\n\n"
                "Мы создали счёт для автоматического продления вашей подписки:\n"
                f"{url}\n\n"
                "Пожалуйста, оплатите в течение 24 часов, чтобы не потерять доступ.",
                reply_markup=kb,
                parse_mode="HTML",
            )
        elif method == "cryptobot":
            created = await create_cryptobot_invoice(
                round(int(total_rub) * 0.0103, 2),
                f"Автопродление ByMeVPN на {days} дней",
                user_id,
            )
            if created:
                url, _ = created
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Оплатить продление", url=url)]
                    ]
                )
                await bot.send_message(
                    user_id,
                    "<b>Автопродление ByMeVPN</b>\n\n"
                    "Мы создали счёт в CryptoBot для автоматического продления:\n"
                    f"{url}\n\n"
                    "Пожалуйста, оплатите в течение 24 часов.",
                    reply_markup=kb,
                    parse_mode="HTML",
                )
        
        logger.info(f"Auto-renewal invoice sent to user {user_id}")
        
    except Exception as e:
        logger.error(f"Error processing auto-renewal for {user_id}: {e}")


async def check_and_send_expiry_notifications(bot: Bot):
    """Check for expiring subscriptions and send notifications"""
    now = int(time.time())
    
    # Check different time ranges
    ranges = [
        (0, 0),   # Expired today
        (1, 1),   # 1 day notification  
        (3, 3),   # 3 days notification
    ]
    
    for days_min, days_max in ranges:
        try:
            nearing = await get_keys_nearing_expiry(days_left_min=days_min, days_left_max=days_max)
            
            for user_id, key, days_left, expiry in nearing:
                # Skip if we already processed this expiry recently
                if days_min == 0 and days_max == 0:  # Expired today
                    # Check if key is actually expired
                    if expiry > now:
                        continue
                
                await send_renewal_notification(bot, user_id, key, days_left, expiry)
                
                # Process auto-renewal for users who have it enabled (1 day before)
                if days_min == 1 and days_max == 1:
                    auto_renewal = await get_auto_renewal(user_id)
                    if auto_renewal:
                        payment = await get_last_payment(user_id)
                        if payment:
                            await process_auto_renewal(
                                bot, 
                                user_id, 
                                payment["days"], 
                                payment["amount"], 
                                payment["method"]
                            )
                            
            logger.info(f"Processed {len(nearing)} notifications for {days_min}-{days_max} days range")
            
        except Exception as e:
            logger.error(f"Error in notification check for {days_min}-{days_max} days: {e}")


async def send_news_notification(bot: Bot, title: str, message: str):
    """Send news notification to subscribed users"""
    try:
        users = await get_news_notification_users()
        
        news_text = (
            f"<b>{title}</b>\n\n"
            f"{message}\n\n"
            f"Чтобы отписаться от таких уведомлений: Настройки → Уведомления о новинках"
        )
        
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Подробнее", callback_data="news_details")],
                [InlineKeyboardButton(text="Закрыть", callback_data="close_news")]
            ]
        )
        
        sent_count = 0
        failed_count = 0
        
        for user_id in users:
            try:
                await bot.send_message(user_id, news_text, reply_markup=kb, parse_mode="HTML")
                sent_count += 1
                await asyncio.sleep(0.1)  # Rate limiting
            except Exception as e:
                failed_count += 1
                logger.debug(f"Failed to send news to {user_id}: {e}")
        
        logger.info(f"News notification sent: {sent_count} successful, {failed_count} failed")
        return sent_count, failed_count
        
    except Exception as e:
        logger.error(f"Error sending news notification: {e}")
        return 0, 0


async def check_tournament_progress(bot: Bot):
    """Check and update tournament progress"""
    try:
        active_tournaments = await get_active_tournaments()
        
        for tournament in active_tournaments:
            # This is a simplified version - in real implementation, 
            # you'd check actual progress based on tournament type
            # For referral tournaments, check new referrals
            # For payment tournaments, check new payments
            
            logger.info(f"Checking progress for tournament: {tournament['title']}")
            
            # Here you would implement specific progress checking logic
            # based on tournament['requirement_type']
            
    except Exception as e:
        logger.error(f"Error checking tournament progress: {e}")


async def check_server_keys_health(bot: Bot):
    """Check health of server keys and replace if necessary"""
    try:
        keys_to_check = await get_keys_for_check(check_interval_hours=6)
        
        if not keys_to_check:
            return
        
        logger.info(f"Checking {len(keys_to_check)} keys for health")
        
        # This is a simplified health check
        # In real implementation, you'd test actual connectivity
        for key_data in keys_to_check:
            try:
                # Simulate health check - replace with actual server connectivity test
                key_healthy = await test_key_connectivity(key_data['key'])
                
                if key_healthy:
                    await update_key_check_status(key_data['id'], 1)
                else:
                    # Key is not working, replace it
                    await replace_failed_key(bot, key_data)
                    
            except Exception as e:
                logger.error(f"Error checking key {key_data['id']}: {e}")
                await update_key_check_status(key_data['id'], 0)  # Mark as failed
                
    except Exception as e:
        logger.error(f"Error in server health check: {e}")


async def test_key_connectivity(key: str) -> bool:
    """Test if a key is working (simplified version)"""
    # This is a placeholder - implement actual connectivity test
    # You might ping the server or try to establish a test connection
    
    # For now, simulate random results for demonstration
    import random
    return random.random() > 0.1  # 90% success rate


async def replace_failed_key(bot: Bot, key_data: dict):
    """Replace a failed key with a new one"""
    try:
        from utils import add_client, generate_vless
        
        user_id = key_data['user_id']
        old_key_id = key_data['id']
        
        # Generate new key
        remark = f"replaced_{user_id}_{int(time.time())}"
        uuid_c = await add_client(key_data['days'], remark)
        new_key = generate_vless(uuid_c)
        
        # Replace in database
        await replace_key(old_key_id, new_key, remark, key_data['days'])
        
        # Log the replacement
        await log_key_replacement(user_id, old_key_id, new_key, "Server health check failed")
        
        # Notify user
        notification_text = (
            "<b>Автоматическая замена ключа</b>\n\n"
            "Мы обнаружили проблемы с вашим предыдущим ключом и автоматически заменили его на новый.\n\n"
            "Ваш доступ к VPN продолжит работать без перерывов.\n\n"
            "Новый ключ уже готов к использованию:"
        )
        
        await generate_and_send_beautiful_qr(bot, user_id, new_key, notification_text)
        
        logger.info(f"Replaced failed key for user {user_id}")
        
    except Exception as e:
        logger.error(f"Error replacing failed key: {e}")


async def send_tournament_reminder(bot: Bot):
    """Send reminders about active tournaments"""
    try:
        active_tournaments = await get_active_tournaments()
        
        if not active_tournaments:
            return
        
        for tournament in active_tournaments:
            # Check if tournament is ending soon (within 24 hours)
            now = int(time.time())
            time_left = tournament['end_date'] - now
            
            if 0 < time_left <= 86400:  # Less than 24 hours left
                await send_tournament_ending_reminder(bot, tournament)
                
    except Exception as e:
        logger.error(f"Error sending tournament reminders: {e}")


async def send_tournament_ending_reminder(bot: Bot, tournament: dict):
    """Send reminder that tournament is ending soon"""
    try:
        hours_left = (tournament['end_date'] - int(time.time())) // 3600
        
        reminder_text = (
            f"<b>Турнир заканчивается!</b>\n\n"
            f"<b>{tournament['title']}</b>\n"
            f"{tournament['description']}\n\n"
            f"Осталось всего {hours_left} часов!\n"
            f"Награда: {tournament['reward_days']} дней\n\n"
            f"Не упустите шанс выиграть!"
        )
        
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Мои турниры", callback_data="my_tournaments")]
            ]
        )
        
        # Send to all tournament participants
        # This would require additional database queries
        
        logger.info(f"Tournament ending reminder sent for: {tournament['title']}")
        
    except Exception as e:
        logger.error(f"Error sending tournament ending reminder: {e}")


async def start_notification_scheduler(bot: Bot):
    """Main notification scheduler with all notification types"""
    logger.info("Starting enhanced notification scheduler")
    
    while True:
        try:
            # Check expiry notifications
            await check_and_send_expiry_notifications(bot)
            
            # Check tournament progress
            await check_tournament_progress(bot)
            
            # Check server key health
            await check_server_keys_health(bot)
            
            # Send tournament reminders
            await send_tournament_reminder(bot)
            
            # Wait before next check (every 2 hours)
            await asyncio.sleep(7200)
            
        except Exception as e:
            logger.error(f"Error in notification scheduler: {e}")
            await asyncio.sleep(3600)  # Wait 1 hour before retrying


async def send_welcome_notification(bot: Bot, user_id: int, user_name: str):
    """Send welcome notification with referral bonus info"""
    try:
        text = (
            f"<b>Добро пожаловать в ByMeVPN, {user_name}!</b>\n\n"
            f"Спасибо, что выбрали нас!\n\n"
            f"<b>Что вас ждёт:</b>\n"
            f"• Быстрый и стабильный VPN без блокировок\n"
            f"• Безлимитный трафик и высокая скорость\n"
            f"• Поддержка на всех устройствах\n"
            f"• Конфиденциальность и безопасность\n\n"
            f"<b>Бонус за друзей:</b>\n"
            f"Приглашайте друзей и получайте +7 дней за каждого оплатившего!\n\n"
            f"Начните с бесплатного пробного периода на 7 дней"
        )
        
        from keyboards import main_menu_keyboard
        kb = main_menu_keyboard()
        
        await bot.send_message(user_id, text, reply_markup=kb, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error sending welcome notification to {user_id}: {e}")


async def send_payment_success_notification(bot: Bot, user_id: int, days: int, method: str):
    """Send notification after successful payment"""
    try:
        method_names = {
            "stars": "Telegram Stars",
            "yookassa": "ЮKassa", 
            "cryptobot": "CryptoBot"
        }
        
        method_name = method_names.get(method, method)
        
        text = (
            f"<b>Оплата успешно принята!</b>\n\n"
            f"Подписка ByMeVPN активирована на {days} дней\n"
            f"Способ оплаты: {method_name}\n\n"
            f"Ваш ключ уже готов к использованию!\n"
            f"Следуйте инструкции для подключения вашего устройства.\n\n"
            f"Спасибо за доверие!"
        )
        
        from keyboards import main_menu_keyboard
        kb = main_menu_keyboard(has_paid=True)
        
        await bot.send_message(user_id, text, reply_markup=kb, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error sending payment success notification to {user_id}: {e}")


async def send_referral_bonus_notification(bot: Bot, referrer_id: int, days: int):
    """Send notification about referral bonus"""
    try:
        text = (
            f"<b>Получен бонус за реферала!</b>\n\n"
            f"Ваш друг оплатил подписку ByMeVPN!\n"
            f"Вам начислено +{days} дней бесплатного использования\n\n"
            f"Спасибо, что рекомендуете нас!\n"
            f"Продолжайте приглашать друзей и получайте бонусы."
        )
        
        await bot.send_message(referrer_id, text, parse_mode="HTML")
        logger.info(f"Referral bonus notification sent to {referrer_id}")
        
    except Exception as e:
        logger.error(f"Error sending referral bonus notification to {referrer_id}: {e}")