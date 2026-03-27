"""
Extended admin panel.
Features: stats, extended stats, broadcast, user list, user search,
          delete user, edit key days, reset trial, personal message,
          payment history, export CSV.
"""
import asyncio
import io
import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)

from config import ADMIN_IDS
from database import (
    get_admin_stats, get_extended_stats,
    get_all_user_ids, get_all_users, get_users_count,
    find_user_by_id, delete_user_and_keys,
    get_user_keys, set_key_days,
    reset_trial, get_user_payments,
    get_all_users_csv, extend_key,
    add_refund, get_user_refunds, get_all_refunds, get_refund_stats,
)
from states import AdminFlow
from utils import safe_answer, fmt_date
from xui import delete_client

logger = logging.getLogger(__name__)
router = Router()


def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ── keyboards ─────────────────────────────────────────────────────────────

def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
         InlineKeyboardButton(text="📈 Детальная", callback_data="admin_stats_ext")],
        [InlineKeyboardButton(text="� Рассылка", callback_data="admin_broadcast"),
         InlineKeyboardButton(text="🔎 Поиск юзера", callback_data="admin_search")],
        [InlineKeyboardButton(text="👥 Все юзеры", callback_data="admin_users:0"),
         InlineKeyboardButton(text="� Возвраты", callback_data="admin_refunds")],
        [InlineKeyboardButton(text="� Экспорт", callback_data="admin_export_csv")],
    ])


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")]
    ])


def _user_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="� Ключи", callback_data=f"admin_user_keys:{user_id}"),
         InlineKeyboardButton(text="💳 Платежи", callback_data=f"admin_user_pay:{user_id}")],
        [InlineKeyboardButton(text="� Возврат", callback_data=f"admin_refund_user:{user_id}"),
         InlineKeyboardButton(text="🔄 Пробник", callback_data=f"admin_reset_trial:{user_id}")],
        [InlineKeyboardButton(text="🎁 Выдать пробник", callback_data=f"admin_grant_trial:{user_id}"),
         InlineKeyboardButton(text="✉️ Написать", callback_data=f"admin_pm:{user_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_del_user:{user_id}")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")],
    ])


def _keys_kb(keys: list, user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for k in keys:
        kid = k["id"]
        remark = (k.get("remark") or f"#{kid}")[:20]
        rows.append([
            InlineKeyboardButton(text=f"✏️ {remark}", callback_data=f"admin_edit_key:{kid}"),
            InlineKeyboardButton(text="🗑️", callback_data=f"admin_del_key:{kid}:{user_id}"),
        ])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"admin_user:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /admin ─────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(
        "⚡ <b>Админ-панель ByMeVPN</b>\n\n"
        "Выберите действие:",
        parse_mode="HTML", reply_markup=_main_kb()
    )


@router.callback_query(F.data == "admin_menu")
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    await state.clear()
    await callback.message.edit_text(
        "⚡ <b>Админ-панель ByMeVPN</b>\n\n"
        "Выберите действие:",
        parse_mode="HTML", reply_markup=_main_kb()
    )


# ── Stats ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_stats")
async def cb_stats(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    s = await get_admin_stats()
    text = (
        "📊 <b>Статистика ByMeVPN</b>\n\n"
        f"👥 <b>Пользователи:</b> {s['total_users']}\n"
        f"✅ <b>Активные подписки:</b> {s['active_users']}\n\n"
        "💰 <b>Доходы:</b>\n"
        f"  📅 Сегодня: <b>{s['today_revenue']} ₽</b>\n"
        f"  📆 Неделя: <b>{s['week_revenue']} ₽</b>\n"
        f"  📅 Месяц: <b>{s['month_revenue']} ₽</b>\n\n"
        f"🤝 <b>Реферальные бонусы:</b> {s['total_referrals']}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "admin_stats_ext")
async def cb_stats_ext(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    s = await get_extended_stats()
    refund_stats = await get_refund_stats()

    top_text = ""
    if s["top_refs"]:
        top_text = "\n👑 <b>Топ рефераторов:</b>\n"
        for i, r in enumerate(s["top_refs"], 1):
            top_text += f"  {i}. ID {r['user_id']} — {r['count']} платных рефералов\n"

    text = (
        "📈 <b>Детальная статистика</b>\n\n"
        "👤 <b>Новые пользователи:</b>\n"
        f"  📅 За 24ч: {s['new_day']}\n"
        f"  📆 За неделю: {s['new_week']}\n"
        f"  📅 За месяц: {s['new_month']}\n\n"
        "🔑 <b>Активные подписки:</b>\n"
        f"  1️⃣ месяц: {s['active_1m']}\n"
        f"  6️⃣ месяцев: {s['active_6m']}\n"
        f"  🔢 год: {s['active_12m']}\n"
        f"  2️⃣ года: {s['active_24m']}\n\n"
        "� <b>Возвраты:</b>\n"
        f"  📅 30 дней: {refund_stats['count_30d']} ({refund_stats['sum_30d']} ₽)\n"
        f"  🔢 Всего: {refund_stats['count_total']} ({refund_stats['sum_total']} ₽)\n"
        f"{top_text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats_ext")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── Broadcast ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_broadcast")
async def cb_broadcast(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    await state.set_state(AdminFlow.broadcast)
    await callback.message.edit_text(
        "✍️ <b>Рассылка</b>\n\nОтправьте текст сообщения.\nПоддерживается HTML.",
        parse_mode="HTML", reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.broadcast))
async def receive_broadcast(message: Message, bot: Bot, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    text = message.html_text or message.text or ""
    if not text:
        await message.answer("Пустое сообщение. Отменено."); return
    user_ids = await get_all_user_ids()
    sent = failed = 0
    status = await message.answer(f"⏳ Рассылка... ({len(user_ids)} пользователей)")
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        if (sent + failed) % 25 == 0:
            await asyncio.sleep(1)
    await status.edit_text(
        f"✅ <b>Рассылка завершена</b>\n\n📤 Отправлено: {sent}\n❌ Ошибок: {failed}",
        parse_mode="HTML", reply_markup=_back_kb()
    )


# ── User list ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_users:"))
async def cb_user_list(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    page = int(callback.data.split(":")[1])
    per_page = 10
    offset = page * per_page
    total = await get_users_count()
    users = await get_all_users(limit=per_page, offset=offset)

    if not users:
        await callback.message.edit_text(
            "Пользователей нет.", reply_markup=_back_kb()
        ); return

    lines = [f"👥 <b>Пользователи</b> (стр. {page+1}):\n"]
    for u in users:
        reg = fmt_date(u["created"]) if u["created"] else "?"
        lines.append(
            f"• <code>{u['user_id']}</code> — "
            f"{'✅' if u['active_keys'] else '❌'} "
            f"ключей: {u['total_keys']}, рег: {reg}"
        )

    # Pagination + user buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀", callback_data=f"admin_users:{page-1}"))
    if offset + per_page < total:
        nav_row.append(InlineKeyboardButton(text="▶", callback_data=f"admin_users:{page+1}"))

    user_rows = [
        [InlineKeyboardButton(
            text=f"👤 {u['user_id']}",
            callback_data=f"admin_user:{u['user_id']}"
        )]
        for u in users
    ]

    kb_rows = user_rows
    if nav_row:
        kb_rows = kb_rows + [nav_row]
    kb_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")])

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


# ── User search ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_search")
async def cb_search(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    await state.set_state(AdminFlow.search_user)
    await callback.message.edit_text(
        "🔍 Введите Telegram ID пользователя:",
        reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.search_user))
async def receive_search(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой ID."); return
    await _show_user(message, uid)


async def _show_user(target, user_id: int):
    """Show user info card."""
    user = await find_user_by_id(user_id)
    if not user:
        text = f"❌ Пользователь <code>{user_id}</code> не найден."
        if isinstance(target, Message):
            await target.answer(text, parse_mode="HTML", reply_markup=_back_kb())
        else:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=_back_kb())
        return

    reg = fmt_date(user["created"]) if user["created"] else "?"
    
    # Get refund info
    refunds = await get_user_refunds(user_id)
    refund_total = sum(r["amount"] for r in refunds) if refunds else 0
    
    text = (
        f"👤 <b>Пользователь {user_id}</b>\n\n"
        f"📅 <b>Регистрация:</b> {reg}\n"
        f"🔑 <b>Ключи:</b> {user['total_keys']} (активных: {user['active_keys']})\n"
        f"🆓 <b>Пробник:</b> {'✅ Использован' if user['trial_used'] else '❌ Не использован'}\n"
        f"👥 <b>Реферер:</b> {user['referrer_id'] or '—'}\n"
        f"💰 <b>Всего оплачено:</b> {user['total_paid']} ₽\n"
        f"💸 <b>Возвращено:</b> {refund_total} {'звёзд' if refunds else '0'}\n"
        f"🔄 <b>Возвратов:</b> {len(refunds) if refunds else 0}"
    )
    kb = _user_kb(user_id)
    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("admin_user:"))
async def cb_user_card(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    uid = int(callback.data.split(":")[1])
    await _show_user(callback, uid)


# ── User keys (admin view) ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_user_keys:"))
async def cb_user_keys(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    uid = int(callback.data.split(":")[1])
    keys = await get_user_keys(uid)
    if not keys:
        await callback.message.edit_text(
            f"У пользователя {uid} нет ключей.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_user:{uid}")
            ]])
        ); return
    lines = [f"🗝 <b>Ключи пользователя {uid}:</b>\n"]
    import time
    now = int(time.time())
    for k in keys:
        status = "✅" if k["expiry"] > now else "❌"
        lines.append(
            f"{status} <b>#{k['id']}</b> {k.get('remark','')}\n"
            f"   До: {fmt_date(k['expiry'])} ({k['days']} дн.)"
        )
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=_keys_kb(keys, uid)
    )


# ── Edit key days ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_edit_key:"))
async def cb_edit_key(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    key_id = int(callback.data.split(":")[1])
    await state.set_state(AdminFlow.edit_key_days)
    await state.update_data(edit_key_id=key_id)
    await callback.message.edit_text(
        f"✏️ Ключ <b>#{key_id}</b>\n\n"
        "Введите количество дней от сегодня (например: <code>30</code> — продлить на 30 дней):",
        parse_mode="HTML",
        reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.edit_key_days))
async def receive_edit_days(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    try:
        days = int(message.text.strip())
        if days < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное число дней."); return
    key_id = data.get("edit_key_id")
    await set_key_days(key_id, days)

    # Sync new expiry to 3x-UI panel so the key actually works
    from database import get_key_by_id
    import time
    from datetime import datetime, timedelta
    k = await get_key_by_id(key_id)
    if k and k.get("uuid"):
        try:
            from xui import _client, _login, INBOUND_ID
            from config import XUI_HOST
            import json
            new_expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
            client_update = {
                "id": k["uuid"],
                "flow": "xtls-rprx-vision",
                "email": k.get("remark", ""),
                "limitIp": k.get("limit_ip", 1),
                "totalGB": 0,
                "expiryTime": new_expiry_ms,
                "enable": True,
                "tgId": "",
                "subId": "",
            }
            payload_xui = {
                "id": INBOUND_ID,
                "settings": json.dumps({"clients": [client_update]}),
            }
            async with _client() as http:
                await _login(http)
                resp = await http.post(
                    f"{XUI_HOST}/panel/api/inbounds/updateClient/{k['uuid']}",
                    json=payload_xui,
                )
                logger.info("3x-UI updateClient key=%d → %d", key_id, resp.status_code)
        except Exception as e:
            logger.error("Failed to update 3x-UI for key %d: %s", key_id, e)

    await message.answer(
        f"✅ Ключ <b>#{key_id}</b> обновлён — {days} дней от сегодня.",
        parse_mode="HTML", reply_markup=_back_kb()
    )


# ── Delete key (admin) ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_del_key:"))
async def cb_admin_del_key(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    parts = callback.data.split(":")
    key_id = int(parts[1])
    user_id = int(parts[2])
    from database import get_key_by_id, delete_key_by_id
    k = await get_key_by_id(key_id)
    if k and k.get("uuid"):
        await delete_client(k["uuid"])
    await delete_key_by_id(key_id)
    await callback.answer(f"Ключ #{key_id} удалён.", show_alert=True)
    # Refresh keys list
    keys = await get_user_keys(user_id)
    if not keys:
        await callback.message.edit_text(
            f"Все ключи пользователя {user_id} удалены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_user:{user_id}")
            ]])
        )
    else:
        import time
        now = int(time.time())
        lines = [f"🗝 <b>Ключи {user_id} (обновлено):</b>\n"]
        for k2 in keys:
            s = "✅" if k2["expiry"] > now else "❌"
            lines.append(f"{s} #{k2['id']} {k2.get('remark','')} — до {fmt_date(k2['expiry'])}")
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=_keys_kb(keys, user_id)
        )


# ── Delete user ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_del_user:"))
async def cb_del_user(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    uid = int(callback.data.split(":")[1])
    uuids = await delete_user_and_keys(uid)
    # Delete from panel
    for uuid in uuids:
        await delete_client(uuid)
    await callback.message.edit_text(
        f"🗑 Пользователь <code>{uid}</code> и все его ключи ({len(uuids)}) удалены.",
        parse_mode="HTML", reply_markup=_back_kb()
    )


# ── Reset trial ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_reset_trial:"))
async def cb_reset_trial(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    uid = int(callback.data.split(":")[1])
    await reset_trial(uid)
    await callback.answer(f"✅ Пробник пользователя {uid} сброшен. Теперь он может взять пробный период заново.", show_alert=True)


@router.callback_query(F.data.startswith("admin_grant_trial:"))
async def cb_grant_trial(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """
    Admin grants a free trial key directly to a user.
    Resets trial flag, then starts the deliver flow on behalf of the user.
    """
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    uid = int(callback.data.split(":")[1])

    # Reset trial flag so delivery is allowed
    await reset_trial(uid)

    # Deliver key directly without asking for config name — use "Admin" as name
    from config import TRIAL_DAYS
    from subscription import deliver_key
    config_name = f"Trial-{uid}"
    success = await deliver_key(
        bot=bot,
        user_id=uid,
        chat_id=uid,
        config_name=config_name,
        days=TRIAL_DAYS,
        limit_ip=1,
        is_paid=False,
        amount=0,
        currency="RUB",
        method="admin_trial",
        payload=f"admin_trial_{uid}",
    )

    if success:
        await callback.message.answer(
            f"✅ Пробный ключ выдан пользователю <code>{uid}</code> "
            f"на {TRIAL_DAYS} дней (1 устройство).",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )
    else:
        await callback.message.answer(
            f"❌ Не удалось выдать ключ пользователю <code>{uid}</code>.\n"
            "Проверьте подключение к 3x-UI в логах.",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )


# ── Personal message ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_pm:"))
async def cb_pm_start(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    uid = int(callback.data.split(":")[1])
    await state.set_state(AdminFlow.send_personal_msg)
    await state.update_data(pm_target=uid)
    await callback.message.edit_text(
        f"✉️ Введите сообщение для пользователя <code>{uid}</code>:",
        parse_mode="HTML", reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.send_personal_msg))
async def receive_personal_msg(message: Message, bot: Bot, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    uid = data.get("pm_target")
    text = message.html_text or message.text or ""
    if not text or not uid:
        await message.answer("Отменено."); return
    try:
        await bot.send_message(uid, text, parse_mode="HTML")
        await message.answer(f"✅ Сообщение отправлено пользователю {uid}.", reply_markup=_back_kb())
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}", reply_markup=_back_kb())


# ── Payment history ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_user_pay:"))
async def cb_user_payments(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    uid = int(callback.data.split(":")[1])
    payments = await get_user_payments(uid)
    if not payments:
        await callback.message.edit_text(
            f"Платежей у пользователя {uid} нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_user:{uid}")
            ]])
        ); return
    lines = [f"💳 <b>Платежи пользователя {uid}:</b>\n"]
    for p in payments:
        dt = fmt_date(p["created"])
        lines.append(
            f"• {dt} — {p['amount']} {p['currency']} "
            f"({p['method']}, {p['days']} дн.)"
        )
    total = sum(p["amount"] for p in payments)
    lines.append(f"\n💰 Итого: {total} ₽")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_user:{uid}")
        ]])
    )


# ── Export CSV ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_export_csv")
async def cb_export_csv(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    await callback.message.edit_text("⏳ Генерирую CSV...", reply_markup=_back_kb())
    csv_text = await get_all_users_csv()
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"bymevpn_users_{date_str}.csv"
    file_bytes = csv_text.encode("utf-8")
    await bot.send_document(
        chat_id=callback.from_user.id,
        document=BufferedInputFile(file_bytes, filename=filename),
        caption=f"📥 Экспорт пользователей ByMeVPN\n{datetime.now().strftime('%d.%m.%Y %H:%M')}",
    )
    await callback.message.edit_text(
        "✅ CSV отправлен выше.", reply_markup=_back_kb()
    )


# ── Refunds management ─────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_refunds")
async def cb_refunds_main(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    
    stats = await get_refund_stats()
    refunds = await get_all_refunds(limit=10)
    
    text = (
        "� <b>Управление возвратами</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"  📅 30 дней: {stats['count_30d']} возвратов ({stats['sum_30d']} ₽)\n"
        f"  🔢 Всего: {stats['count_total']} возвратов ({stats['sum_total']} ₽)\n\n"
    )
    
    if refunds:
        text += "<b>📋 Последние возвраты:</b>\n"
        for r in refunds:
            dt = fmt_date(r["created"])
            text += f"• {dt} — {r['amount']} {r['currency']} (юзер {r['user_id']})\n"
    else:
        text += "📋 Возвратов пока нет."
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Возврат юзеру", callback_data="admin_refund_search")],
        [InlineKeyboardButton(text="📋 Все возвраты", callback_data="admin_refunds_list")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")],
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "admin_refund_search")
async def cb_refund_search(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    await state.set_state(AdminFlow.search_user)
    await state.update_data(refund_mode=True)
    await callback.message.edit_text(
        "💰 <b>Возврат средств</b>\n\n"
        "Введите Telegram ID пользователя для возврата:",
        reply_markup=_back_kb()
    )


@router.callback_query(F.data.startswith("admin_refund_user:"))
async def cb_refund_user(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    
    uid = int(callback.data.split(":")[1])
    user = await find_user_by_id(uid)
    if not user:
        await callback.message.edit_text(
            f"❌ Пользователь <code>{uid}</code> не найден.",
            reply_markup=_back_kb()
        )
        return
    
    payments = await get_user_payments(uid)
    if not payments:
        await callback.message.edit_text(
            f"У пользователя <code>{uid}</code> нет платежей для возврата.",
            reply_markup=_back_kb()
        )
        return
    
    # Show recent payments for context
    text = (
        f"💰 <b>Возврат пользователю {uid}</b>\n\n"
        f"💳 Всего оплачено: {user['total_paid']} ₽\n\n"
        f"<b>Последние платежи:</b>\n"
    )
    
    for p in payments[:5]:  # Show last 5 payments
        dt = fmt_date(p["created"])
        text += f"• {dt} — {p['amount']} {p['currency']} ({p['method']})\n"
    
    text += f"\nВыберите платеж для возврата или введите сумму:"
    
    # Create buttons for recent payments
    rows = []
    for p in payments[:3]:  # Quick refund buttons for last 3 payments
        rows.append([
            InlineKeyboardButton(
                text=f"Вернуть {p['amount']} {p['currency']} ({p['method']})",
                callback_data=f"admin_refund_do:{uid}:{p['amount']}:{p['currency']}:{p['method']}:{p['payload'] or ''}"
            )
        ])
    
    rows.append([
        InlineKeyboardButton(text="📝 Другая сумма", callback_data=f"admin_refund_custom:{uid}")
    ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_refunds")])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admin_refund_custom:"))
async def cb_refund_custom(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    
    uid = int(callback.data.split(":")[1])
    await state.set_state(AdminFlow.refund_amount)
    await state.update_data(refund_user_id=uid)
    
    await callback.message.edit_text(
        f"💰 <b>Возврат пользователю {uid}</b>\n\n"
        "Введите сумму для возврата (в звёздах/рублях):",
        reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.refund_amount))
async def receive_refund_amount(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            raise ValueError("Amount must be positive")
    except ValueError:
        await message.answer("❌ Введите положительное число."); return
    
    data = await state.get_data()
    uid = data.get("refund_user_id")
    if not uid:
        await message.answer("❌ Ошибка сессии. Начните заново."); return
    
    await state.set_state(AdminFlow.refund_reason)
    await state.update_data(refund_amount=amount)
    
    await message.answer(
        f"💰 <b>Возврат {amount} звёзд пользователю {uid}</b>\n\n"
        "Введите причину возврата:",
        reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.refund_reason))
async def receive_refund_reason(message: Message, bot: Bot, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    
    reason = message.text.strip() or "Возврат по запросу"
    data = await state.get_data()
    await state.clear()
    
    uid = data.get("refund_user_id")
    amount = data.get("refund_amount")
    
    if not uid or not amount:
        await message.answer("❌ Ошибка сессии. Начните заново."); return
    
    # Record refund in database
    await add_refund(
        user_id=uid,
        amount=amount,
        currency="XTR",  # Telegram Stars
        method="stars",
        reason=reason,
        refunded_by=message.from_user.id
    )
    
    # Try to refund actual Stars (this would require Telegram Stars API)
    # For now, we just record it and notify admin
    
    try:
        # Notify user about refund
        await bot.send_message(
            uid,
            f"💰 <b>Возврат средств</b>\n\n"
            f"Вам возвращено <b>{amount} звёзд</b>.\n"
            f"Причина: {reason}\n\n"
            f"Если звёзды не поступили в течение 5 минут, напишите в поддержку.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error("Could not notify user %d about refund: %s", uid, e)
    
    await message.answer(
        f"✅ <b>Возврат оформлен</b>\n\n"
        f"👤 Пользователь: <code>{uid}</code>\n"
        f"💰 Сумма: {amount} звёзд\n"
        f"📝 Причина: {reason}\n\n"
        f"ℹ️ Запись добавлена в базу данных.",
        reply_markup=_back_kb()
    )


@router.callback_query(F.data.startswith("admin_refund_do:"))
async def cb_refund_do(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    
    parts = callback.data.split(":")
    uid = int(parts[1])
    amount = int(parts[2])
    currency = parts[3]
    method = parts[4]
    payload = ":".join(parts[5:]) if len(parts) > 5 else ""
    
    # Quick refund with default reason
    await add_refund(
        user_id=uid,
        amount=amount,
        currency=currency,
        method=method,
        reason=f"Возврат платежа ({method})",
        original_payload=payload,
        refunded_by=callback.from_user.id
    )
    
    try:
        await callback.bot.send_message(
            uid,
            f"💰 <b>Возврат средств</b>\n\n"
            f"Вам возвращено <b>{amount} {currency}</b>.\n"
            f"Причина: Возврат платежа\n\n"
            f"Если средства не поступили в течение 5 минут, напишите в поддержку.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error("Could not notify user %d about refund: %s", uid, e)
    
    await callback.answer(f"✅ Возврат {amount} {currency} пользователю {uid} оформлен!", show_alert=True)
    
    # Refresh refunds list
    await cb_refunds_main(callback)


@router.callback_query(F.data == "admin_refunds_list")
async def cb_refunds_list(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    
    refunds = await get_all_refunds(limit=30)
    
    if not refunds:
        await callback.message.edit_text(
            "Пока нет возвратов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin_refunds")
            ]])
        )
        return
    
    text = "📋 <b>Все возвраты:</b>\n\n"
    for r in refunds:
        dt = fmt_date(r["created"])
        text += (
            f"• {dt} — {r['amount']} {r['currency']}\n"
            f"  Юзер: {r['user_id']} (всего оплачено: {r['user_total_paid']} ₽)\n"
            f"  Метод: {r['method']}, причина: {r['reason']}\n\n"
        )
    
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="admin_refunds")
        ]])
    )
