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
from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, CRYPTO_BOT_TOKEN
from database import is_yookassa_processed, mark_yookassa_processed, is_crypto_processed
from webhook import _process_payment, _process_crypto_invoice
from payments import get_paid_crypto_invoices

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
                
                # Проверяем, не обработан ли уже перед вызовом _process_payment
                if await is_yookassa_processed(payment_id):
                    logger.info(f"Payment {payment_id} already processed, skipping")
                    continue
                
                # Обрабатываем платеж (функция сама пометит как обработанный)
                await _process_payment(self.bot, payment_id)
                
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


class CryptoPaymentMonitor:
    """
    Мониторинг платежей Crypto Bot (@send) без вебхуков.

    FIX: previously create_crypto_payment() only created the invoice and
    showed it to the user — nothing ever checked whether it got paid, so
    no key was ever delivered for a crypto payment. This polls CryptoBot's
    getInvoices(status=paid) every 30s, same pattern as PaymentMonitor
    above for YooKassa, and hands off newly-paid invoices to
    webhook._process_crypto_invoice for delivery.
    """

    def __init__(self, bot: Bot):
        self.bot = bot
        self.check_interval = 30  # секунд
        self.running = False

    async def check_and_process_payments(self):
        """Проверить и обработать неучтённые оплаченные инвойсы"""
        try:
            invoices = await get_paid_crypto_invoices()

            if not invoices:
                return

            logger.info(f"Crypto monitor: found {len(invoices)} paid invoices to check")

            for invoice in invoices:
                invoice_id = invoice.get("invoice_id")
                if invoice_id is None:
                    continue

                if await is_crypto_processed(invoice_id):
                    continue

                logger.info(f"Crypto monitor: processing new invoice {invoice_id}")
                await _process_crypto_invoice(self.bot, invoice)

        except Exception as e:
            logger.error(f"Error in crypto payment check: {e}")

    async def start_monitoring(self):
        """Запустить мониторинг платежей"""
        if self.running:
            logger.warning("Crypto payment monitor already running")
            return

        self.running = True
        logger.info("Crypto payment monitor started")

        await self.check_and_process_payments()

        while self.running:
            try:
                await asyncio.sleep(self.check_interval)
                await self.check_and_process_payments()
            except Exception as e:
                logger.error(f"Crypto monitor error: {e}")
                await asyncio.sleep(5)

    def stop_monitoring(self):
        """Остановить мониторинг"""
        self.running = False
        logger.info("Crypto payment monitor stopped")


# Глобальный экземпляр монитора
_monitor_instance: Optional[PaymentMonitor] = None
_crypto_monitor_instance: Optional[CryptoPaymentMonitor] = None

async def start_payment_monitor(bot: Bot) -> None:
    """Запустить мониторинг платежей"""
    global _monitor_instance, _crypto_monitor_instance

    # Не запускать мониторинг, если ЮKassa не настроена (пустые или дефолтные значения)
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.warning(
            "YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY не заданы — "
            "мониторинг платежей ЮKassa отключён."
        )
    elif YOOKASSA_SHOP_ID in ("", "123456") or YOOKASSA_SECRET_KEY.startswith("test_xxx"):
        logger.warning(
            "Обнаружены дефолтные тестовые реквизиты ЮKassa (%s) — "
            "мониторинг платежей отключён. Заполните YOOKASSA_SHOP_ID и "
            "YOOKASSA_SECRET_KEY в .env реальными значениями.",
            YOOKASSA_SHOP_ID,
        )
    else:
        if _monitor_instance is None:
            _monitor_instance = PaymentMonitor(bot)
        asyncio.create_task(_monitor_instance.start_monitoring())
        logger.info("Payment monitoring task started")

    # Crypto Bot monitor — independent of YooKassa config
    if not CRYPTO_BOT_TOKEN:
        logger.warning(
            "CRYPTO_BOT_TOKEN не задан — мониторинг Crypto Bot платежей отключён."
        )
    else:
        if _crypto_monitor_instance is None:
            _crypto_monitor_instance = CryptoPaymentMonitor(bot)
        asyncio.create_task(_crypto_monitor_instance.start_monitoring())
        logger.info("Crypto Bot payment monitoring task started")

async def stop_payment_monitor() -> None:
    """Остановить мониторинг платежей"""
    global _monitor_instance, _crypto_monitor_instance

    if _monitor_instance:
        _monitor_instance.stop_monitoring()
        _monitor_instance = None

    if _crypto_monitor_instance:
        _crypto_monitor_instance.stop_monitoring()
        _crypto_monitor_instance = None
