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
         InlineKeyboardButton(text="📈 Расширенная", callback_data="admin_stats_ext")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="👥 Список пользователей", callback_data="admin_users:0")],
        [InlineKeyboardButton(text="🔍 Найти пользователя", callback_data="admin_search")],
        [InlineKeyboardButton(text="📥 Экспорт CSV", callback_data="admin_export_csv")],
    ])


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_menu")]
    ])


def _user_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗝 Ключи", callback_data=f"admin_user_keys:{user_id}"),
         InlineKeyboardButton(text="💳 Платежи", callback_data=f"admin_user_pay:{user_id}")],
        [InlineKeyboardButton(text="🔄 Сбросить пробник", callback_data=f"admin_reset_trial:{user_id}")],
        [InlineKeyboardButton(text="✉️ Написать", callback_data=f"admin_pm:{user_id}")],
        [InlineKeyboardButton(text="🗑 Удалить юзера", callback_data=f"admin_del_user:{user_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")],
    ])


def _keys_kb(keys: list, user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for k in keys:
        kid = k["id"]
        remark = (k.get("remark") or f"#{kid}")[:20]
        rows.append([
            InlineKeyboardButton(text=f"✏️ {remark}", callback_data=f"admin_edit_key:{kid}"),
            InlineKeyboardButton(text="🗑", callback_data=f"admin_del_key:{kid}:{user_id}"),
        ])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_user:{user_id}")])
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
        "<b>Админ-панель ByMeVPN</b>\nВыберите действие:",
        parse_mode="HTML", reply_markup=_main_kb()
    )


@router.callback_query(F.data == "admin_menu")
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    await state.clear()
    await callback.message.edit_text(
        "<b>Админ-панель ByMeVPN</b>\nВыберите действие:",
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
        f"👥 Всего пользователей: {s['total_users']}\n"
        f"✅ Активных подписок: {s['active_users']}\n\n"
        "💰 <b>Доходы:</b>\n"
        f"  Сегодня: {s['today_revenue']} ₽\n"
        f"  За неделю: {s['week_revenue']} ₽\n"
        f"  За месяц: {s['month_revenue']} ₽\n\n"
        f"🤝 Реферальных бонусов выдано: {s['total_referrals']}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "admin_stats_ext")
async def cb_stats_ext(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    s = await get_extended_stats()

    top_text = ""
    if s["top_refs"]:
        top_text = "\n👑 <b>Топ рефераторов:</b>\n"
        for i, r in enumerate(s["top_refs"], 1):
            top_text += f"  {i}. ID {r['user_id']} — {r['count']} платных рефералов\n"

    text = (
        "📈 <b>Расширенная статистика</b>\n\n"
        "👤 <b>Новые пользователи:</b>\n"
        f"  За 24ч: {s['new_day']}\n"
        f"  За неделю: {s['new_week']}\n"
        f"  За месяц: {s['new_month']}\n\n"
        "🔑 <b>Активные подписки по срокам:</b>\n"
        f"  1 месяц: {s['active_1m']}\n"
        f"  6 месяцев: {s['active_6m']}\n"
        f"  12 месяцев: {s['active_12m']}\n"
        f"  24 месяца: {s['active_24m']}\n"
        f"{top_text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats_ext")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")],
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
    text = (
        f"👤 <b>Пользователь {user_id}</b>\n\n"
        f"📅 Регистрация: {reg}\n"
        f"🔑 Ключей всего: {user['total_keys']}\n"
        f"✅ Активных: {user['active_keys']}\n"
        f"🆓 Пробник использован: {'Да' if user['trial_used'] else 'Нет'}\n"
        f"👥 Реферер: {user['referrer_id'] or '—'}\n"
        f"💰 Оплачено всего: {user['total_paid']} ₽"
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
    await callback.answer(f"✅ Пробник пользователя {uid} сброшен.", show_alert=True)


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
