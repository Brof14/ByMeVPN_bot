import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_ID
from database import (
    get_user_keys,
    get_user_payments,
    has_used_trial,
    get_admin_stats,
)
from utils import format_expiry_date
from states import AdminStates

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command(commands=["admin"]))
async def admin_panel(message: Message, state: FSMContext):
    """Админ-панель"""
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📨 Рассылка всем пользователям", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🔍 Поиск пользователя по ID", callback_data="admin_find_user")],
        ]
    )
    await message.answer("<b>Админ‑панель byMeVPN</b>\nВыберите действие:", reply_markup=kb)


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    """Статистика сервиса"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await callback.answer()
    stats = await get_admin_stats()
    lines = [
        "<b>📊 Статистика сервиса</b>",
        f"👥 Пользователей в базе: <b>{stats['total_users']}</b>",
        f"✅ Активных подписок: <b>{stats['active_users']}</b>",
        f"💰 Суммарный объём платежей (условно): <b>{stats['total_revenue']}</b>",
    ]
    if stats["popular_plans"]:
        lines.append("\n🔥 Популярные планы (дней: кол-во):")
        for days, cnt in stats["popular_plans"]:
            lines.append(f"• {days} дней — {cnt} оплат")
    await callback.message.answer("\n".join(lines))


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    """Начало рассылки"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.broadcast)
    await callback.message.answer(
        "✉️ <b>Рассылка</b>\n\n"
        "Отправьте текст сообщения, которое нужно разослать всем пользователям.\n"
        "Поддерживается HTML‑форматирование."
    )


@router.message(AdminStates.broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    """Отправка рассылки"""
    if message.from_user.id != ADMIN_ID:
        return
    text = message.html_text
    await state.clear()
    await message.answer("🚀 Запускаю рассылку...")

    import aiosqlite
    from database import DB_FILE

    sent = 0
    failed = 0
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        for (uid,) in rows:
            try:
                await message.bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
    await message.answer(f"Готово.\n✅ Отправлено: {sent}\n⚠️ Ошибок: {failed}")


@router.callback_query(F.data == "admin_find_user")
async def admin_find_user_start(callback: CallbackQuery, state: FSMContext):
    """Начало поиска пользователя"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.search_user)
    await callback.message.answer("🔍 Введите Telegram ID пользователя:")


@router.message(AdminStates.search_user)
async def admin_find_user_handle(message: Message, state: FSMContext):
    """Обработка поиска пользователя"""
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    keys = await get_user_keys(uid)
    payments = await get_user_payments(uid)
    has_trial = await has_used_trial(uid)

    lines = [f"<b>Пользователь {uid}</b>"]
    lines.append(f"🔑 Ключей: {len(keys)}")
    if keys:
        last_expiry = max(k["expiry"] for k in keys)
        lines.append(
            "Последний ключ истекает: "
            f"{datetime.fromtimestamp(last_expiry).strftime('%d.%m.%Y %H:%M')}"
        )
    lines.append(f"🎁 Пробный период использован: {'Да' if has_trial else 'Нет'}")
    lines.append(f"💰 Платежей: {len(payments)}")
    if payments:
        last = payments[0]
        lines.append(
            f"Последний платёж: {last['amount']} {last['currency']} за {last['days']} дней "
            f"({datetime.fromtimestamp(last['created']).strftime('%d.%m.%Y %H:%M')})"
        )

    await message.answer("\n".join(lines))