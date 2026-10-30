"""Named, restartable asyncio workers with bounded retry backoff."""
import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from config import ADMIN_ID

logger = logging.getLogger(__name__)
_tasks: dict[str, asyncio.Task] = {}
_last_alert: dict[str, float] = {}


async def _supervise(name: str, factory: Callable[[], Coroutine[Any, Any, Any]], bot=None) -> None:
    delay = 1.0
    while True:
        try:
            await factory()
            raise RuntimeError("worker exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background worker %s stopped; restarting in %.1fs", name, delay)
            now = time.monotonic()
            if bot is not None and ADMIN_ID and now - _last_alert.get(name, 0) >= 300:
                _last_alert[name] = now
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ Фоновая задача <code>{name}</code> завершилась с ошибкой. Перезапуск через {int(delay)} с; подробности — в логах.",
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.warning("Could not notify admin about background worker %s", name)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
        else:
            delay = 1.0


def spawn_supervised(
    name: str, factory: Callable[[], Coroutine[Any, Any, Any]], bot=None,
) -> asyncio.Task:
    """Start one named worker; repeated registration returns the live task."""
    current = _tasks.get(name)
    if current is not None and not current.done():
        return current
    task = asyncio.create_task(_supervise(name, factory, bot), name=f"supervisor:{name}")
    _tasks[name] = task
    return task


async def cancel_background_tasks() -> None:
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
