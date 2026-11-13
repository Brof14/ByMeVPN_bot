"""Read-only comparison of subscription records with 3x-ui state."""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


async def reconcile_subscriptions() -> dict:
    """Compare active DB keys with panel clients. Never changes either system."""
    import database
    from xui_client import get_xui_client_details

    db = await database.get_db()
    cur = await db.execute(
        "SELECT user_id, id, uuid, expiry, limit_ip FROM keys "
        "WHERE expiry > ? ORDER BY user_id, expiry DESC",
        (int(time.time()),),
    )
    rows = await cur.fetchall()
    by_user: dict[int, list[dict]] = {}
    for row in rows:
        by_user.setdefault(int(row[0]), []).append({
            "id": int(row[1]), "uuid": str(row[2] or "").strip(),
            "expiry": int(row[3] or 0), "limit_ip": int(row[4] or 0),
        })

    semaphore = asyncio.Semaphore(8)
    async def fetch(uid: int):
        async with semaphore:
            try:
                return uid, await get_xui_client_details(uid, strict=True), None
            except Exception as exc:
                return uid, None, type(exc).__name__

    states = await asyncio.gather(*(fetch(uid) for uid in by_user))
    unavailable = []
    discrepancies = []
    checked = 0
    for uid, panel, error in states:
        active = by_user[uid][0] if by_user[uid] else None
        if error:
            unavailable.append(uid)
            continue
        if not active:
            continue
        checked += 1
        if not panel:
            discrepancies.append({"user_id": uid, "key_id": active["id"], "kind": "missing_in_panel"})
            continue
        panel_uuid = str(panel.get("uuid") or "").strip().lower()
        db_uuid = active["uuid"].lower()
        panel_expiry = int(panel.get("expiry_time") or 0) // 1000
        if not db_uuid or panel_uuid != db_uuid or abs(panel_expiry - active["expiry"]) > 120:
            discrepancies.append({
                "user_id": uid, "key_id": active["id"], "kind": "attributes_differ",
                "uuid_match": bool(db_uuid and panel_uuid == db_uuid),
                "expiry_delta": panel_expiry - active["expiry"],
            })
    return {
        "checked": checked, "db_active": len(by_user),
        "discrepancies": discrepancies, "unavailable": unavailable,
        "complete": not unavailable,
    }


def format_report(report: dict) -> str:
    issues = report["discrepancies"]
    lines = ["<b>Сверка подписок · только чтение</b>", "",
             f"Активных записей в базе: {report['db_active']}",
             f"Проверено в 3x-ui: {report['checked']}",
             f"Расхождений: {len(issues)}"]
    if report["unavailable"]:
        lines.append(f"Не удалось проверить: {len(report['unavailable'])} аккаунтов (состояние панели неизвестно).")
    for issue in issues[:12]:
        kind = "клиент отсутствует в панели" if issue["kind"] == "missing_in_panel" else "UUID или срок отличаются"
        lines.append(f"• ID {issue['user_id']} · ключ #{issue['key_id']}: {kind}")
    if len(issues) > 12:
        lines.append(f"Ещё записей: {len(issues) - 12}")
    if issues:
        lines.extend(["", "Безопасно: открыть ключ пользователя и сверить срок/UUID в базе и 3x-ui; восстановление запускать только после подтверждения оплаченного срока. Автоизменений не выполнялось."])
    elif not report["unavailable"]:
        lines.extend(["", "Расхождений не найдено."])
    return "\n".join(lines)


def report_keyboard(report: dict):
    """Offer safe navigation to each affected key; actions never mutate data."""
    from aiogram.types import InlineKeyboardMarkup
    from bot_style import themed_button as InlineKeyboardButton

    rows = [
        [InlineKeyboardButton(
            text=f"Проверить восстановление · ключ #{issue['key_id']}",
            callback_data=f"admin_recovery_preview:{issue['key_id']}",
        )]
        for issue in report["discrepancies"][:8]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def reconciliation_scheduler(bot) -> None:
    """Run a read-only audit every six hours and report changes to admins."""
    from config import ADMIN_IDS
    previous_signature = None
    while True:
        await asyncio.sleep(120)
        try:
            report = await reconcile_subscriptions()
            signature = tuple(sorted((item["user_id"], item["key_id"], item["kind"])
                                     for item in report["discrepancies"]))
            if (report["discrepancies"] or report["unavailable"]) and signature != previous_signature:
                for admin_id in ADMIN_IDS:
                    await bot.send_message(
                        admin_id, format_report(report), parse_mode="HTML",
                        reply_markup=report_keyboard(report),
                    )
            previous_signature = signature
        except Exception as exc:
            logger.warning("Subscription reconciliation failed (%s)", type(exc).__name__)
        await asyncio.sleep(6 * 60 * 60 - 120)
