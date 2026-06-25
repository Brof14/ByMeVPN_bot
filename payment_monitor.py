"""
Автоматический мониторинг платежей ЮKassa без вебхуков.
Проверяет успешные платежи каждые 30 секунд и выдает ключи.
"""
import asyncio
import logging
import base64
import httpx
from typing import List, Dict, Optional
from datetime import datetime, timedelta

from aiogram import Bot
from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY
from database import is_yookassa_processed, mark_yookassa_processed
from webhook import _process_payment

logger = logging.getLogger(__name__)

class PaymentMonitor:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
        self.headers = {"Authorization": f"Basic {self.auth}"}
        self.check_interval = 30  # 30 секунд
        self.running = False
        
    async def get_successful_payments(self, minutes_back: int = 5) -> List[Dict]:
        """Получить успешные платежи за последние N минут"""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Получаем платежи за последние 5 минут
                created_at_gte = (datetime.utcnow() - timedelta(minutes=minutes_back)).isoformat() + 'Z'
                
                url = f"https://api.yookassa.ru/v3/payments?limit=50&status=succeeded&created_at.gte={created_at_gte}"
                resp = await client.get(url, headers=self.headers)
                resp.raise_for_status()
                
                data = resp.json()
                return data.get('items', [])
                
        except Exception as e:
            logger.error(f"Error fetching payments: {e}")
            return []
    
    async def check_and_process_payments(self):
        """Проверить и обработать необработанные платежи"""
        try:
            payments = await self.get_successful_payments()
            
            if not payments:
                return
                
            logger.info(f"Found {len(payments)} successful payments to check")
            
            for payment in payments:
                payment_id = payment.get('id')
                if not payment_id:
                    continue
                    
                # Проверяем, не обработан ли уже платеж
                if await is_yookassa_processed(payment_id):
                    continue
                    
                logger.info(f"Processing new payment: {payment_id}")
                
                # Обрабатываем платеж
                await _process_payment(self.bot, payment_id)
                
                # Помечаем как обработанный
                await mark_yookassa_processed(payment_id)
                
                logger.info(f"Payment {payment_id} processed successfully")
                
        except Exception as e:
            logger.error(f"Error in payment check: {e}")
    
    async def start_monitoring(self):
        """Запустить мониторинг платежей"""
        if self.running:
            logger.warning("Payment monitor already running")
            return
            
        self.running = True
        logger.info("Payment monitor started")
        
        # Сначала обрабатываем все необработанные платежи за последний час
        await self.check_and_process_payments()
        
        while self.running:
            try:
                await asyncio.sleep(self.check_interval)
                await self.check_and_process_payments()
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                await asyncio.sleep(5)  # Ждем 5 секунд перед повторной попыткой
    
    def stop_monitoring(self):
        """Остановить мониторинг"""
        self.running = False
        logger.info("Payment monitor stopped")

# Глобальный экземпляр монитора
_monitor_instance: Optional[PaymentMonitor] = None

async def start_payment_monitor(bot: Bot) -> None:
    """Запустить мониторинг платежей"""
    global _monitor_instance
    
    if _monitor_instance is None:
        _monitor_instance = PaymentMonitor(bot)
    
    # Запускаем в фоновом режиме
    asyncio.create_task(_monitor_instance.start_monitoring())
    logger.info("Payment monitoring task started")

async def stop_payment_monitor() -> None:
    """Остановить мониторинг платежей"""
    global _monitor_instance
    
    if _monitor_instance:
        _monitor_instance.stop_monitoring()
        _monitor_instance = None
