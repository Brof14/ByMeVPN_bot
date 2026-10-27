"""
Автоматический мониторинг платежей ЮKassa без вебхуков.
Проверяет успешные платежи каждые 30 секунд и выдает ключи.
"""
import asyncio
import logging
import base64
import httpx
import time
from typing import List, Dict, Optional
from datetime import datetime, timedelta

from aiogram import Bot
from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, CRYPTO_BOT_TOKEN
from webhook import _process_payment, _process_crypto_invoice
from payments import get_crypto_invoices, get_paid_crypto_invoices

logger = logging.getLogger(__name__)

class PaymentMonitor:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
        self.headers = {"Authorization": f"Basic {self.auth}"}
        self.check_interval = 30  # 30 секунд
        self.running = False
        self.start_time = datetime.utcnow()  # Track when monitor started
        
    async def get_successful_payments(self, minutes_back: int = 5) -> List[Dict]:
        """Получить успешные платежи за последние N минут, включая период до рестарта."""
        started = time.perf_counter()
        from async_utils import perf_metrics
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
                # Do not clamp to process start time: payments made during downtime
                # must be discoverable after a restart.
                cutoff_time = datetime.utcnow() - timedelta(minutes=minutes_back)
                created_at_gte = cutoff_time.isoformat() + 'Z'
                
                url = f"https://api.yookassa.ru/v3/payments?limit=50&status=succeeded&created_at.gte={created_at_gte}"
                resp = await client.get(url, headers=self.headers)
                resp.raise_for_status()
                
                data = resp.json()
                payments = data.get('items', [])
                perf_metrics.observe("yookassa_payment_poll", time.perf_counter() - started)
                return payments
                
        except Exception as error:
            perf_metrics.observe("yookassa_payment_poll", time.perf_counter() - started, True)
            logger.warning(
                "YooKassa payment poll failed (%s): %s",
                type(error).__name__, str(error) or "no error details",
            )
            return []
    
    async def check_and_process_payments(self, minutes_back: int = 5):
        """Проверить и обработать необработанные платежи за заданное окно."""
        try:
            payments = await self.get_successful_payments(minutes_back=minutes_back)

            # The provider time window is only a fast path. Persistent checkout
            # IDs cover payments older than the window and failed deliveries.
            from database import get_all_yookassa_pending
            pending_ids = await get_all_yookassa_pending()
            known_ids = {payment.get("id") for payment in payments}
            payments.extend({"id": payment_id} for payment_id in pending_ids if payment_id not in known_ids)
            
            if not payments:
                return
                
            logger.info("Checking %d recent or locally tracked YooKassa payment(s)", len(payments))
            
            for payment in payments:
                payment_id = payment.get('id')
                if not payment_id:
                    continue
                    
                # Delegate deduplication to the payment ledger. A legacy
                # processed marker alone must not hide an interrupted delivery.
                logger.info("Checking YooKassa payment through recovery-safe handler: %s", payment_id)
                
                # Обрабатываем платеж (функция сама пометит как обработанный)
                await _process_payment(self.bot, payment_id)
                
                logger.info("YooKassa payment check finished: %s", payment_id)
                
        except Exception as e:
            logger.error(f"Error in payment check: {e}")
    
    async def start_monitoring(self):
        """Запустить мониторинг платежей"""
        if self.running:
            logger.warning("Payment monitor already running")
            return
            
        self.running = True
        logger.info("Payment monitor started")
        
        # On startup scan an hour back so payments during downtime are recovered.
        await self.check_and_process_payments(minutes_back=60)
        
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
        self.start_time = datetime.utcnow()  # Track when monitor started

    async def check_and_process_payments(self):
        """Проверить и обработать неучтённые оплаченные инвойсы"""
        try:
            # Keep the latest provider page for legacy invoices, then explicitly
            # reconcile every locally persisted invoice so the 50-item page
            # limit cannot strand an older paid order.
            invoices = await get_paid_crypto_invoices()
            from database import get_all_crypto_pending
            pending_ids = await get_all_crypto_pending()
            invoices_by_id = {
                str(invoice.get("invoice_id")): invoice
                for invoice in invoices if invoice.get("invoice_id") is not None
            }

            pending_ids = [
                invoice_id for invoice_id in pending_ids
                if invoice_id not in invoices_by_id
            ]
            batches = [pending_ids[i:i + 50] for i in range(0, len(pending_ids), 50)]
            semaphore = asyncio.Semaphore(5)

            async def lookup_batch(invoice_ids: list[str]):
                async with semaphore:
                    batch = await get_crypto_invoices(invoice_ids)
                for invoice in batch:
                    if invoice.get("invoice_id") is not None:
                        invoices_by_id[str(invoice["invoice_id"])] = invoice

            await asyncio.gather(*(lookup_batch(batch) for batch in batches))
            invoices = list(invoices_by_id.values())

            if not invoices:
                return

            # Expired invoices can no longer be paid; remove them from the
            # reconciliation queue so old abandoned checkouts do not add work.
            from database import delete_crypto_pending
            for invoice in invoices:
                if invoice.get("status") == "expired" and invoice.get("invoice_id") is not None:
                    await delete_crypto_pending(invoice["invoice_id"])

            logger.info("Crypto monitor: reconciling %d locally tracked invoice(s)", len(invoices))

            for invoice in invoices:
                invoice_id = invoice.get("invoice_id")
                if invoice_id is None:
                    continue

                # Do not skip old paid invoices solely because the process restarted
                # or a legacy marker exists. The invoice handler checks the payment
                # ledger and quarantines ambiguous cases without double-fulfillment.
                logger.info("Checking invoice through recovery-safe handler: %s", invoice_id)
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
