"""Confirmed, audited recovery for a narrow set of subscription discrepancies.

No payment is created, no term is extended, and no existing panel identity is
replaced. Every write requires an administrator confirmation and a fresh check.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
import uuid as uuid_lib
from datetime import datetime
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
_DAY = 86400
_EXPIRY_TOLERANCE = 120


async def _read_inbounds(user_id: int) -> tuple[list[int], dict[int, list[dict]]]:
    from config import XUI_INBOUND_IDS
    from xui_client import _api_call_with_retry, _get_api

    inbound_ids = [int(iid) for iid in XUI_INBOUND_IDS]
    if not inbound_ids:
        raise RuntimeError("no_configured_inbounds")
    api = await _get_api()

    async def read(iid: int):
        inbound = await _api_call_with_retry(api.inbound.get_by_id, iid)
        clients = getattr(inbound, "client_stats", None)
        if clients is None:
            raise RuntimeError("inbound_state_unavailable")
        found = []
        for client in clients:
            if str(getattr(client, "email", "")) != str(int(user_id)):
                continue
            raw_uuid = getattr(client, "uuid", None) or getattr(client, "id", None)
            found.append({
                "uuid": str(raw_uuid or "").strip(),
                "sub_id": str(getattr(client, "sub_id", None) or "").strip(),
                "expiry_ms": int(getattr(client, "expiry_time", 0) or 0),
                "enabled": bool(getattr(client, "enable", False)),
            })
        return iid, found

    results = await asyncio.gather(*(read(iid) for iid in inbound_ids))
    return inbound_ids, dict(results)


async def _paid_term_coverage(key: dict) -> dict:
    """Project only successful, positive-price orders from this key's lifetime."""
    from database import get_user_payments

    key_created = int(key.get("created") or 0)
    payments = await get_user_payments(int(key["user_id"]))
    paid = []
    for payment in payments:
        try:
            amount = int(payment.get("amount") or 0)
            days = int(payment.get("days") or 0)
            created = int(payment.get("created") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            payment.get("status") == "success"
            and amount > 0 and days > 0 and created >= key_created - _DAY
            and created <= int(time.time()) + 300
            and str(payment.get("method") or "").lower() != "trial"
        ):
            paid.append({"created": created, "days": days})
    paid.sort(key=lambda row: row["created"])

    covered_until = key_created
    total_paid_days = 0
    for payment in paid:
        covered_until = max(covered_until, payment["created"])
        covered_until += payment["days"] * _DAY
        total_paid_days += payment["days"]

    expiry = int(key.get("expiry") or 0)
    return {
        "has_paid_orders": bool(paid),
        "payment_count": len(paid),
        "total_paid_days": total_paid_days,
        "covered_until": covered_until,
        "covers_db_expiry": bool(paid) and expiry <= covered_until + _EXPIRY_TOLERANCE,
    }


def _sub_id_from_key(key: dict) -> str | None:
    url = str(key.get("key") or "").strip()
    try:
        parts = urlsplit(url)
        token = parts.path.rstrip("/").split("/")[-1]
    except Exception:
        return None
    return token if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", token) else None


async def assess_key_recovery(key_id: int) -> dict:
    """Fresh read-only assessment. Result never contains UUIDs or URLs."""
    from constants import VALID_DEVICE_LIMITS
    from database import get_key_by_id, get_user_keys

    key = await get_key_by_id(int(key_id))
    if not key:
        return {"eligible": False, "reason": "Запись ключа не найдена."}
    now = int(time.time())
    result = {
        "eligible": False, "key_id": int(key["id"]), "user_id": int(key["user_id"]),
        "db_expiry": int(key.get("expiry") or 0), "plan": None,
    }
    if result["db_expiry"] <= now:
        result["reason"] = "Срок в базе уже истёк; восстанавливать активный доступ нельзя."
        return result

    active_keys = [
        row for row in await get_user_keys(result["user_id"])
        if int(row.get("expiry") or 0) > now
    ]
    if len(active_keys) != 1 or int(active_keys[0].get("id") or 0) != result["key_id"]:
        result["reason"] = "У пользователя несколько активных ключей или ключ изменился; нужна ручная сверка."
        return result

    try:
        client_uuid = str(key.get("uuid") or "").strip()
        uuid_lib.UUID(client_uuid)
    except (ValueError, TypeError, AttributeError):
        result["reason"] = "UUID в базе отсутствует или повреждён; автоматическое восстановление остановлено."
        return result
    sub_id = _sub_id_from_key(key)
    if not sub_id:
        result["reason"] = "Ссылка подписки в базе не прошла проверку; автоматическое восстановление остановлено."
        return result
    devices = int(key.get("limit_ip") or 0)
    if devices not in VALID_DEVICE_LIMITS:
        result["reason"] = "Лимит устройств в базе недопустим; нужна ручная сверка."
        return result

    coverage = await _paid_term_coverage(key)
    result.update({
        "devices": devices,
        "paid_orders": coverage["payment_count"],
        "paid_days": coverage["total_paid_days"],
        "paid_until": coverage["covered_until"],
        "paid_term_verified": coverage["covers_db_expiry"],
        "identity_fingerprint": hashlib.sha256(client_uuid.encode()).hexdigest()[:12],
    })
    if not coverage["covers_db_expiry"]:
        result["reason"] = "Успешные платежи не подтверждают срок, указанный в базе; нужна ручная сверка."
        return result

    try:
        inbound_ids, by_inbound = await _read_inbounds(result["user_id"])
    except Exception as exc:
        logger.warning("Could not read all inbounds for recovery assessment (%s)", type(exc).__name__)
        result["reason"] = "Не удалось проверить все inbounds; панель не изменялась."
        return result

    profiles = [profile for rows in by_inbound.values() for profile in rows]
    result["panel_profiles"] = len(profiles)
    result["inbounds"] = len(inbound_ids)
    if any(len(by_inbound[iid]) > 1 for iid in inbound_ids):
        result["reason"] = "В панели найдены повторяющиеся профили; нужна ручная сверка."
        return result

    if not profiles:
        result.update({"eligible": True, "plan": "restore_missing", "reason": ""})
        return result

    if any(not by_inbound[iid] for iid in inbound_ids):
        result["reason"] = "Профиль есть не на всех inbounds; автоматическое восстановление остановлено."
        return result

    if any(
        profile["uuid"].lower() != client_uuid.lower()
        or profile["sub_id"] != sub_id
        or not profile["enabled"]
        for profile in profiles
    ):
        result["reason"] = "UUID, ссылка или состояние отличаются между панелью и базой; замена запрещена."
        return result

    expiries = [profile["expiry_ms"] // 1000 for profile in profiles]
    if any(expiry > result["db_expiry"] + _EXPIRY_TOLERANCE for expiry in expiries):
        result["reason"] = "В панели есть более поздний срок; автоматическое изменение могло бы сократить доступ."
        return result
    if any(max(expiries) - value > _EXPIRY_TOLERANCE for value in expiries):
        result["reason"] = "Сроки между inbounds различаются; нужна ручная сверка."
        return result
    if max(expiries) >= result["db_expiry"] - _EXPIRY_TOLERANCE:
        result["reason"] = "Сроки уже совпадают в допустимых пределах; изменений не требуется."
        return result

    result.update({"eligible": True, "plan": "restore_expiry", "panel_expiry": expiries[0], "reason": ""})
    return result


def format_recovery_preview(item: dict) -> str:
    if not item.get("key_id"):
        return f"<b>Восстановление не подготовлено</b>\n\n{item.get('reason', 'Запись недоступна.')}"
    expiry = datetime.fromtimestamp(item["db_expiry"]).strftime("%d.%m.%Y %H:%M")
    lines = [
        "<b>Проверка восстановления</b>", "",
        f"Ключ #{item['key_id']} · пользователь <code>{item['user_id']}</code>",
        f"Срок из базы: <b>{expiry}</b>",
        f"Платёжная сверка: {item.get('paid_orders', 0)} успешных оплат · {item.get('paid_days', 0)} оплаченных дней",
    ]
    if item.get("paid_until"):
        covered = datetime.fromtimestamp(item["paid_until"]).strftime("%d.%m.%Y %H:%M")
        lines.append(f"Оплаченный период подтверждает срок до: <b>{covered}</b>")
    if item.get("eligible") and item.get("plan") == "restore_missing":
        lines.extend(["", f"Действие: восстановить отсутствующий профиль на {item['inbounds']} inbounds с тем же UUID и сроком из базы."])
    elif item.get("eligible") and item.get("plan") == "restore_expiry":
        panel_expiry = datetime.fromtimestamp(item["panel_expiry"]).strftime("%d.%m.%Y %H:%M")
        lines.extend(["", f"Действие: установить в 3x-ui срок из базы ({expiry}); сейчас в панели {panel_expiry}. Срок не будет увеличен сверх базы."])
    else:
        lines.extend(["", item.get("reason") or "Автоматическое действие не подготовлено."])
    lines.extend(["", "Перед подтверждением бот повторно сверит оплату, срок и все inbounds. Действие будет записано в журнал администратора."])
    return "\n".join(lines)


async def execute_key_recovery(key_id: int, admin_id: int) -> dict:
    """Apply only a just-revalidated recovery plan; preserve identity and expiry."""
    from database import (
        log_admin_action, release_delivery_lease, try_acquire_delivery_lease,
    )
    from xui_client import (
        Client, _api_call_with_retry, _get_api, _inbound_name,
        delete_xui_user, set_xui_client_limits, update_xui_client_settings,
    )

    first = await assess_key_recovery(key_id)
    if not first.get("eligible"):
        return {"ok": False, "message": first.get("reason", "План восстановления недоступен.")}

    uid = int(first["user_id"])
    token = secrets.token_urlsafe(24)
    try:
        acquired = await try_acquire_delivery_lease(uid, token)
    except Exception as exc:
        logger.warning("Recovery lease unavailable (%s)", type(exc).__name__)
        return {"ok": False, "message": "Не удалось защитить операцию от параллельной выдачи. Повторите сверку позже."}
    if not acquired:
        return {"ok": False, "message": "Для пользователя уже выполняется операция. Повторите сверку позже."}

    try:
        plan = await assess_key_recovery(key_id)
        if not plan.get("eligible") or plan.get("plan") != first.get("plan"):
            return {"ok": False, "message": plan.get("reason") or "Состояние изменилось после preview; обновите сверку."}

        before = (
            f"key_id={key_id}; plan={plan['plan']}; db_expiry={plan['db_expiry']}; "
            f"panel_expiry={plan.get('panel_expiry', 0)}; inbounds={plan.get('inbounds', 0)}; "
            f"paid_orders={plan.get('paid_orders', 0)}; paid_days={plan.get('paid_days', 0)}; "
            f"uuid_fingerprint={plan.get('identity_fingerprint', '')}"
        )
        try:
            await log_admin_action(admin_id, "subscription_recovery_before", before, uid)
        except Exception as exc:
            logger.error("Recovery audit log unavailable (%s); refusing panel change", type(exc).__name__)
            return {"ok": False, "message": "Не удалось сохранить запись аудита. Панель не изменялась."}

        created_inbounds: list[int] = []
        try:
            if plan["plan"] == "restore_expiry":
                verified = await update_xui_client_settings(
                    uid, expiry_ms=int(plan["db_expiry"]) * 1000,
                )
                if not verified:
                    raise RuntimeError("panel_expiry_update_unverified")
            elif plan["plan"] == "restore_missing":
                from database import get_key_by_id
                from config import XUI_INBOUND_IDS
                key = await get_key_by_id(key_id)
                if not key:
                    raise RuntimeError("database_key_disappeared")
                client_uuid = str(key.get("uuid") or "").strip()
                sub_id = _sub_id_from_key(key)
                if not sub_id:
                    raise RuntimeError("subscription_id_invalid")
                api = await _get_api()
                for iid in [int(value) for value in XUI_INBOUND_IDS]:
                    inbound = await _api_call_with_retry(api.inbound.get_by_id, iid)
                    current = getattr(inbound, "client_stats", None)
                    if current is None:
                        raise RuntimeError("inbound_state_unavailable")
                    if any(str(getattr(client, "email", "")) == str(uid) for client in current):
                        raise RuntimeError("panel_state_changed_after_preview")
                    payload = {
                        "email": str(uid), "id": client_uuid, "enable": True,
                        "expiry_time": int(plan["db_expiry"]) * 1000,
                        "limit_ip": 0, "total_gb": 0, "sub_id": sub_id,
                        "remark": _inbound_name(iid, inbound),
                    }
                    try:
                        await _api_call_with_retry(api.client.add, iid, [Client(**payload)])
                    except Exception:
                        # A lost acknowledgement can still mean the add landed.
                        current = await _api_call_with_retry(api.inbound.get_by_id, iid)
                        rows = getattr(current, "client_stats", None)
                        if rows is None or not any(
                            str(getattr(row, "email", "")) == str(uid)
                            and str(getattr(row, "uuid", None) or getattr(row, "id", "")) == client_uuid
                            for row in rows
                        ):
                            raise
                    created_inbounds.append(iid)
                if not await set_xui_client_limits(uid, limit_ip=0, limit_hwid=int(plan["devices"])):
                    raise RuntimeError("device_limit_unverified")
            else:
                raise RuntimeError("unsupported_recovery_plan")

            inbound_ids, after = await _read_inbounds(uid)
            key = await __import__("database").get_key_by_id(key_id)
            target_uuid = str(key.get("uuid") or "").strip() if key else ""
            target_sub_id = _sub_id_from_key(key or {})
            target_expiry = int(plan["db_expiry"]) * 1000
            valid_after = all(
                len(after.get(iid, [])) == 1
                and after[iid][0]["uuid"].lower() == target_uuid.lower()
                and after[iid][0]["sub_id"] == target_sub_id
                and after[iid][0]["expiry_ms"] == target_expiry
                and after[iid][0]["enabled"]
                for iid in inbound_ids
            )
            if plan["plan"] == "restore_missing":
                from xui_client import get_xui_devices
                devices = await get_xui_devices(uid)
                valid_after = valid_after and int(devices.get("limit_hwid") or 0) == int(plan["devices"])
            if not valid_after:
                raise RuntimeError("panel_readback_mismatch")

            await log_admin_action(
                admin_id, "subscription_recovery_complete",
                f"key_id={key_id}; plan={plan['plan']}; expiry={plan['db_expiry']}; verified_inbounds={len(inbound_ids)}; uuid_fingerprint={plan.get('identity_fingerprint', '')}",
                uid,
            )
            return {"ok": True, "user_id": uid, "message": "Восстановление завершено и проверено на всех inbounds. Срок и UUID сохранены."}
        except Exception as exc:
            rollback_ok = True
            if created_inbounds:
                try:
                    api = await _get_api()
                    from database import get_key_by_id
                    key = await get_key_by_id(key_id)
                    client_uuid = str(key.get("uuid") or "").strip() if key else ""
                    for iid in reversed(created_inbounds):
                        try:
                            await _api_call_with_retry(api.client.delete, iid, client_uuid)
                            inbound = await _api_call_with_retry(api.inbound.get_by_id, iid)
                            clients = getattr(inbound, "client_stats", None)
                            if clients is None or any(
                                str(getattr(client, "email", "")) == str(uid)
                                for client in clients
                            ):
                                rollback_ok = False
                        except Exception:
                            rollback_ok = False
                except Exception:
                    rollback_ok = False
            outcome = "rolled_back" if rollback_ok and created_inbounds else "manual_review"
            try:
                await log_admin_action(
                    admin_id, f"subscription_recovery_{outcome}",
                    f"key_id={key_id}; plan={plan['plan']}; error={type(exc).__name__}; rollback_verified={rollback_ok}",
                    uid,
                )
            except Exception:
                logger.exception("Could not log subscription recovery result")
            logger.error("Subscription recovery failed for key %d (%s)", key_id, type(exc).__name__)
            if outcome == "rolled_back":
                return {"ok": False, "message": "Восстановление не прошло проверку; созданные профили удалены и их отсутствие подтверждено. Срок в базе не менялся."}
            if plan["plan"] == "restore_expiry":
                return {"ok": False, "message": "Панель не подтвердила изменение полностью. Срок в базе сохранён; проверьте карточку и журнал администратора."}
            return {"ok": False, "message": "Операция прервана; результат требует ручной проверки в 3x-ui. Срок в базе не менялся."}
    finally:
        try:
            await release_delivery_lease(uid, token)
        except Exception as exc:
            logger.error("Could not release recovery lease (%s)", type(exc).__name__)
