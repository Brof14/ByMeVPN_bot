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
_failure_count: dict[str, int] = {}
_ALERT_COOLDOWN_SECONDS = 30 * 60


def _should_alert(name: str, now: float) -> bool:
    """Rate-limit admin alerts while keeping failures visible in server logs."""
    if now - _last_alert.get(name, float("-inf")) < _ALERT_COOLDOWN_SECONDS:
        return False
    _last_alert[name] = now
    return True


def _safe_error_type(error: Exception) -> str:
    """Expose only a simple exception class name, never its possibly sensitive message."""
    name = type(error).__name__
    return name if name.isascii() and name.replace("_", "").isalnum() else "Exception"


async def _supervise(name: str, factory: Callable[[], Coroutine[Any, Any, Any]], bot=None) -> None:
    delay = 1.0
    while True:
        try:
            await factory()
            raise RuntimeError("worker exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures = _failure_count.get(name, 0) + 1
            _failure_count[name] = failures
            logger.exception(
                "Background worker %s stopped; restarting in %.1fs (failure %d, %s)",
                name, delay, failures, _safe_error_type(exc),
            )
            now = time.monotonic()
            if bot is not None and ADMIN_ID and _should_alert(name, now):
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ Фоновая задача <code>{name}</code> завершилась с ошибкой "
                        f"<code>{_safe_error_type(exc)}</code> (сбой №{failures}). "
                        f"Перезапуск через {int(delay)} с; подробности — в логах.",
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
