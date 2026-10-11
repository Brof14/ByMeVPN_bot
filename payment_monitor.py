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
        self.start_time = datetime.utcnow()  # Track when monitor started
        self.retry_cooldown_seconds = 300
        self._last_attempt: dict[str, float] = {}
        
    async def get_successful_payments(self, minutes_back: int = 5) -> List[Dict]:
        """Получить успешные платежи за последние N минут, включая период до рестарта."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Do not clamp to process start time: payments made during downtime
                # must be discoverable after a restart. Follow the provider cursor so
                # a burst of >50 payments cannot silently truncate recovery.
                cutoff_time = datetime.utcnow() - timedelta(minutes=minutes_back)
                created_at_gte = cutoff_time.isoformat() + "Z"
                url = "https://api.yookassa.ru/v3/payments"
                params = {
                    "limit": 100,
                    "status": "succeeded",
                    "created_at.gte": created_at_gte,
                }
                items: list[Dict] = []
                seen_cursors: set[str] = set()
                max_pages = 100

                for _page in range(max_pages):
                    resp = await client.get(url, headers=self.headers, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    items.extend(data.get("items", []))

                    cursor = data.get("next_cursor")
                    if not cursor:
                        return items
                    cursor = str(cursor)
                    if cursor in seen_cursors:
                        logger.error("YooKassa pagination cursor repeated; stopping recovery scan safely")
                        break
                    seen_cursors.add(cursor)
                    params = {
                        "limit": 100,
                        "status": "succeeded",
                        "created_at.gte": created_at_gte,
                        "cursor": cursor,
                    }

                logger.error(
                    "YooKassa recovery scan reached its pagination safety limit; "
                    "some payments may remain for the next scan"
                )
                return items
                
        except Exception as e:
            logger.error(f"Error fetching payments: {e}")
            return []
    
    async def _get_incomplete_ledger_payment_ids(self) -> list[str]:
        """Discover stale failed/interrupted orders outside the recent provider window."""
        try:
            from database import get_db
            db = await get_db()
            stale_before = int(datetime.utcnow().timestamp()) - 600
            cur = await db.execute(
                "SELECT provider_payment_id FROM payments "
                "WHERE provider='yookassa' AND status IN ('failed','processing') "
                "AND created<=? AND provider_payment_id IS NOT NULL "
                "ORDER BY created ASC LIMIT 500",
                (stale_before,),
            )
            rows = await cur.fetchall()
            return [str(row[0]) for row in rows if row and row[0]]
        except Exception as exc:
            logger.error("Could not read incomplete YooKassa payments from ledger: %s", exc)
            return []

    async def check_and_process_payments(self, minutes_back: int = 5):
        """Reconcile recent provider payments and retry failed ledger rows safely."""
        try:
            recent_payments = await self.get_successful_payments(minutes_back=minutes_back)
            payment_ids: dict[str, None] = {}
            from database import is_yookassa_payment_known
            for payment in recent_payments:
                payment_id = payment.get("id")
                if not payment_id:
                    continue
                metadata = payment.get("metadata") if isinstance(payment.get("metadata"), dict) else {}
                description = payment.get("description")
                legacy_byme_order = isinstance(description, str) and description.startswith("ByMeVPN")
                if metadata.get("service") == "bymevpn" or legacy_byme_order:
                    payment_ids[str(payment_id)] = None
                elif await is_yookassa_payment_known(str(payment_id)):
                    payment_ids[str(payment_id)] = None
                else:
                    logger.debug("Skipping unrelated successful YooKassa payment %s", payment_id)

            # The provider time window is a discovery mechanism, not the source of
            # truth for retries. Failed ledger rows may be much older than the window.
            for payment_id in await self._get_incomplete_ledger_payment_ids():
                payment_ids.setdefault(payment_id, None)

            if not payment_ids:
                return

            now = asyncio.get_running_loop().time()
            # Bound memory use and prevent a broken provider/panel from causing
            # the same failed order to be retried on every 30-second poll.
            self._last_attempt = {
                payment_id: attempted_at
                for payment_id, attempted_at in self._last_attempt.items()
                if now - attempted_at < self.retry_cooldown_seconds * 2
            }

            logger.info("Found %d YooKassa payments to reconcile", len(payment_ids))
            for payment_id in payment_ids:
                last_attempt = self._last_attempt.get(payment_id)
                if last_attempt is not None and now - last_attempt < self.retry_cooldown_seconds:
                    continue
                self._last_attempt[payment_id] = now
                logger.info("Reconciling YooKassa payment %s through recovery-safe handler", payment_id)
                await _process_payment(self.bot, payment_id)

        except Exception as e:
            logger.error("Error in payment reconciliation: %s", e)

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
            invoices = await get_paid_crypto_invoices()

            if not invoices:
                return

            logger.info(f"Crypto monitor: found {len(invoices)} paid invoices to check")

            for invoice in invoices:
                invoice_id = invoice.get("invoice_id")
                if invoice_id is None:
                    continue

                # Crypto Pay lists all paid invoices for the merchant token. Only
                # process invoices associated with this bot's local order/ledger.
                from database import is_crypto_invoice_known
                if not await is_crypto_invoice_known(invoice_id):
                    logger.debug("Skipping unrelated paid Crypto Pay invoice %s", invoice_id)
                    continue
                logger.info("Reconciling known paid Crypto Pay invoice %s", invoice_id)
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
_monitor_tasks: list[asyncio.Task] = []

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
        task = asyncio.create_task(_monitor_instance.start_monitoring(), name="yookassa-payment-monitor")
        _monitor_tasks.append(task)
        logger.info("Payment monitoring task started")

    # Crypto Bot monitor — independent of YooKassa config
    if not CRYPTO_BOT_TOKEN:
        logger.warning(
            "CRYPTO_BOT_TOKEN не задан — мониторинг Crypto Bot платежей отключён."
        )
    else:
        if _crypto_monitor_instance is None:
            _crypto_monitor_instance = CryptoPaymentMonitor(bot)
        task = asyncio.create_task(_crypto_monitor_instance.start_monitoring(), name="crypto-payment-monitor")
        _monitor_tasks.append(task)
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

    tasks = list(_monitor_tasks)
    _monitor_tasks.clear()
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
