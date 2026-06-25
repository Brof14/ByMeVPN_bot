"""
Асинхронные утилиты для максимальной производительности бота.
Пулы соединений, батчинг, конкурентные операции.
"""
import asyncio
import logging
from typing import List, Any, Callable, Optional
from concurrent.futures import ThreadPoolExecutor
import functools

logger = logging.getLogger(__name__)

# Глобальный пул потоков для CPU-интенсивных операций
_thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vpnbot")

# Семафоры для ограничения конкурентных операций
_db_semaphore = asyncio.Semaphore(10)  # Максимум 10 одновременных БД операций
_xui_semaphore = asyncio.Semaphore(5)   # Максимум 5 одновременных XUI запросов


def run_in_thread(func: Callable) -> Callable:
    """Декоратор для выполнения CPU-интенсивных операций в отдельном потоке."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_thread_pool, func, *args, **kwargs)
    return wrapper


async def batch_execute(tasks: List[Callable], max_concurrent: int = 10) -> List[Any]:
    """
    Выполнить список задач батчами с ограничением конкурентности.
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def limited_task(task):
        async with semaphore:
            return await task()
    
    # Создаем ограниченные задачи
    limited_tasks = [limited_task(task) for task in tasks]
    
    # Выполняем все конкурентно
    return await asyncio.gather(*limited_tasks, return_exceptions=True)


async def safe_execute_with_timeout(coro, timeout: float = 5.0, default=None):
    """
    Безопасно выполнить корутину с таймаутом.
    Возвращает default при истечении таймаута.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Operation timed out after {timeout}s")
        return default
    except Exception as e:
        logger.error(f"Operation failed: {e}")
        return default


class DatabasePool:
    """Пул соединений с базой данных для максимальной производительности."""
    
    def __init__(self, max_connections: int = 10):
        self.max_connections = max_connections
        self._pool = []
        self._semaphore = asyncio.Semaphore(max_connections)
        self._lock = asyncio.Lock()
    
    async def get_connection(self):
        """Получить соединение из пула."""
        async with self._semaphore:
            if self._pool:
                return self._pool.pop()
            return None
    
    async def return_connection(self, conn):
        """Вернуть соединение в пул."""
        async with self._lock:
            if len(self._pool) < self.max_connections:
                self._pool.append(conn)
            else:
                await conn.close()
    
    async def execute_with_pool(self, query: str, params: tuple = ()):
        """Выполнить запрос с использованием пула соединений."""
        async with _db_semaphore:
            from database import get_db
            db = await get_db()
            try:
                if params:
                    cursor = await db.execute(query, params)
                else:
                    cursor = await db.execute(query)
                await db.commit()
                return cursor
            except Exception as e:
                logger.error(f"Database error: {e}")
                raise
            finally:
                pass  # Соединение управляется глобально


# Глобальный пул БД
db_pool = DatabasePool()


async def gather_with_exceptions(*tasks):
    """
    Выполнить задачи и вернуть результаты, включая исключения.
    """
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Логируем исключения, но не прерываем выполнение
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"Task {i} failed: {result}")
    
    return results


class PerformanceMonitor:
    """Монитор производительности для оптимизации."""
    
    def __init__(self):
        self._times = {}
    
    def start_timing(self, name: str):
        """Начать замер времени."""
        self._times[name] = asyncio.get_event_loop().time()
    
    def end_timing(self, name: str) -> float:
        """Закончить замер времени и вернуть длительность."""
        if name in self._times:
            duration = asyncio.get_event_loop().time() - self._times[name]
            if duration > 0.1:  # Логируем только медленные операции
                logger.warning(f"Slow operation: {name} took {duration:.3f}s")
            del self._times[name]
            return duration
        return 0.0


# Глобальный монитор
perf_monitor = PerformanceMonitor()


def monitor_performance(name: str):
    """Декоратор для мониторинга производительности."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            perf_monitor.start_timing(name)
            try:
                result = await func(*args, **kwargs)
                return result
            finally:
                perf_monitor.end_timing(name)
        return wrapper
    return decorator


# Предзагруженные данные для мгновенных ответов
_preloaded_data = {
    'tariffs': None,
    'support_url': None,
    'admin_ids': None,
}

async def preload_static_data():
    """Предзагрузить статические данные для мгновенных ответов."""
    from constants import PRICE_CONFIG, PERIOD_LABELS
    from config import ADMIN_IDS
    from keyboards import _SUPPORT_URL
    
    _preloaded_data['tariffs'] = PRICE_CONFIG
    _preloaded_data['period_labels'] = PERIOD_LABELS
    _preloaded_data['support_url'] = _SUPPORT_URL
    _preloaded_data['admin_ids'] = ADMIN_IDS
    
    logger.info("Static data preloaded for instant responses")


def get_preloaded(key: str, default=None):
    """Получить предзагруженные данные."""
    return _preloaded_data.get(key, default)
