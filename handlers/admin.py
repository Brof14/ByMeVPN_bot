Warning: truncated output (original token count: 52608)
Total output lines: 4600

"""
Extended admin panel.
Features: stats, extended stats, broadcast, user list, user search,
          delete user, edit key days, reset trial, personal message,
          payment history, export CSV.
"""
import asyncio
from html import escape
import io
import logging
import math
import time
from datetime import datetime
from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    BufferedInputFile,
)

from config import ADMIN_IDS, XUI_URL, XUI_USERNAME
from database import (
    get_admin_stats, get_extended_stats,
    get_all_users, get_users_count, get_all_users_paginated,
    find_user_by_id, delete_user_and_keys,
    get_user_keys, set_key_days, update_key_remark,
    set_trial_used, reset_trial_for_user, get_user_payments, get_payment_by_id,
    get_all_users_csv, extend_key,
    add_refund, get_user_refunds, get_all_refunds, get_refund_stats,
    get_payment_stats,
    get_all_keys_paginated, get_keys_count,
    create_promo_code, get_all_promo_codes, delete_promo_code, validate_promo_code,
    has_trial_used, get_db,
    get_key_errors, get_key_errors_count, delete_key_error, get_user_key_errors,
    log_admin_action, get_admin_logs,
    ban_user, unban_user, add_manual_days,
    create_giveaway, get_active_giveaways, get_giveaway_by_id,
    set_giveaway_status,
    create_broadcast_campaign,
    get_conversion_funnel,
    get_performance_summary,
)
from states import AdminFlow
from utils import safe_answer
from constants import format_timestamp as fmt_date, format_days_left as fmt_days_left, MAX_GRANT_DAYS
from xui_client import delete_xui_user
from bot_style import themed_button as InlineKeyboardButton

logger = logging.getLogger(__name__)
router = Router()


def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


async def _fill_missing_usernames(bot: Bot, users: list[dict]) -> None:
    """Backfill usernames for older rows when Telegram still exposes the chat."""
    from database import ensure_user

    async def refresh(user: dict) -> None:
        if user.get("username"):
            return
        try:
            chat = await bot.get_chat(user["user_id"])
            username = getattr(chat, "username", None)
            if username:
                user["username"] = username
                await ensure_user(user["user_id"], username)
        except Exception:
            # Some users have no public username or Telegram no longer exposes
            # the private chat; the numeric ID remains the reliable identifier.
            return

    await asyncio.gather(*(refresh(user) for user in users))


# ── keyboards ─────────────────────────────────────────────────────────────

_ADMIN_HOME_TEXT = "<b>ByMeVPN · Управление</b>\n\nВыберите раздел."


def _main_kb() -> InlineKeyboardMarkup:
    """Compact dashboard with common destinations available immediately."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обзор", callback_data="admin_stats"),
         InlineKeyboardButton(text="Найти пользователя", callback_data="admin_search")],
        [InlineKeyboardButton(text="Пользователи и подписки", callback_data="admin_clients_menu"),
         InlineKeyboardButton(text="Платежи и финансы", callback_data="admin_finance_menu")],
        [InlineKeyboardButton(text="Маркетинг", callback_data="admin_growth"),
         InlineKeyboardButton(text="Система", callback_data="admin_ops_menu")],
    ])


def _clients_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пользователи", callback_data="admin_users:0"),
         InlineKeyboardButton(text="Подписки", callback_data="admin_all_keys:0")],
        [InlineKeyboardButton(text="Найти пользователя", callback_data="admin_search")],
        [InlineKeyboardButton(text="Экспорт пользователей", callback_data="admin_export_users_csv")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="admin_menu")],
    ])


def _finances_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Платежи", callback_data="admin_payments"),
         InlineKeyboardButton(text="Возвраты", callback_data="admin_refunds")],
        [InlineKeyboardButton(text="Финансовый отчёт", callback_data="admin_finance")],
        [InlineKeyboardButton(text="Проблемные платежи", callback_data="admin_payment_incidents")],
        [InlineKeyboardButton(text="Экспорт данных", callback_data="admin_export_csv")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="admin_menu")],
    ])


@router.callback_query(F.data == "admin_churn_feedback")
async def cb_admin_churn_feedback(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    from database import get_churn_feedback_summary
    try:
        summary = await get_churn_feedback_summary(90)
    except Exception as exc:
        logger.warning("Churn feedback report unavailable (%s)", type(exc).__name__)
        await callback.message.answer("Отчёт пока недоступен.")
        return
    labels = {
        "connection": "Не подключается",
        "price": "Цена",
        "not_needed": "Нет потребности",
        "payment": "Проблемы оплаты",
        "other": "Другое",
    }
    lines = ["<b>Причины ухода · 90 дней</b>", "",
             "Добровольные ответы, не репрезентативны для всех клиентов."]
    for key, count in sorted(summary.items(), key=lambda item: -item[1]):
        lines.append(f"{labels.get(key, 'Другое')}: {count}")
    if not summary:
        lines.append("Нет ответов.")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_growth_kb(),
    )


@router.callback_query(F.data == "admin_source_revenue")
async def cb_admin_source_revenue(callback: CallbackQuery):
    """Show measured revenue without pretending it is profit or ROAS."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    from acquisition_revenue import source_revenue_snapshot
    try:
        cohorts = await source_revenue_snapshot(days=30)
    except Exception as exc:
        logger.warning("Source revenue report unavailable (%s)", type(exc).__name__)
        await callback.message.answer("Не удалось получить статистику источников.")
        return
    lines = ["<b>Источники привлечения · 30 дней</b>", "",
             "Выручка валовая, без комиссий, возвратов, VPS и рекламных затрат.",
             "Telegram Stars не пересчитываются в рубли."]
    for name, row in list(cohorts["by_source"].items())[:15]:
        gross = row["gross_by_currency"]
        money = ", ".join(
            f"{amount} {escape(currency)}" for currency, amount in sorted(gross.items())
        ) or "0"
        lines.append(
            f"\n<b>{escape(name)}</b>: пользователей {row['started']}; "
            f"подключились {row['connected']}; оплатили {row['paid']}; "
            f"валовая выручка {money}"
        )
    if not cohorts["by_source"]:
        lines.append("\nНет событий за выбранный период.")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=_growth_kb(),
    )


@router.callback_query(F.data == "admin_payment_incidents")
async def cb_admin_payment_incidents(callback: CallbackQuery):
    """Read-only reconciliation queue; never silently grant/refund twice."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    from billing_incidents import read_billing_incidents
    try:
        incidents = await read_billing_incidents()
    except Exception as exc:
        logger.error("Billing incident query failed (%s)", type(exc).__name__)
        await callback.message.answer("Не удалось получить список платежей для сверки.")
        return
    lines = ["<b>Проблемные платежи</b>", "",
             "Данные для сверки; повторная выдача и возврат не выполняются автоматически."]
    buttons = []
    for entry in incidents:
        reason = escape(str(entry["status"]))
        provider = escape(str(entry["provider"]))
        invoice_id = escape(str(entry["invoice_id"]))
        lines.append(f"\n{provider} · <code>{invoice_id}</code> · {reason}")
        uid = entry.get("user_id")
        if uid:
            buttons.append([InlineKeyboardButton(
                text=f"Клиент {int(uid)}", callback_data=f"admin_user:{int(uid)}"
            )])
    if not incidents:
        lines.append("\nВ журнале не найдено операций, ожидающих сверки.")
    buttons.append([InlineKeyboardButton(text="← К финансам", callback_data="admin_finance_menu")])
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def _growth_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Промокоды", callback_data="admin_promo_codes"),
         InlineKeyboardButton(text="Розыгрыши", callback_data="admin_giveaways")],
        [InlineKeyboardButton(text="Рефералы", callback_data="admin_referrals"),
         InlineKeyboardButton(text="Реклама", callback_data="ad_admin_list")],
        [InlineKeyboardButton(text="Воронка", callback_data="admin_funnel"),
         InlineKeyboardButton(text="Оплаты / конверсия", callback_data="admin_checkout_funnel")],
        [InlineKeyboardButton(text="Органический рост", callback_data="admin_referral_funnel")],
        [InlineKeyboardButton(text="Выручка по источникам", callback_data="admin_source_revenue")],
        [InlineKeyboardButton(text="Причины ухода", callback_data="admin_churn_feedback")],
        [InlineKeyboardButton(text="Проверка рефералов", callback_data="admin_referral_risk")],
        [InlineKeyboardButton(text="Рассылки", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="admin_menu")],
    ])


def _operations_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сверить подписки", callback_data="admin_subscription_reconcile")],
        [InlineKeyboardButton(text="Статус системы", callback_data="admin_system"),
         InlineKeyboardButton(text="Ошибки", callback_data="admin_key_errors:0")],
        [InlineKeyboardButton(text="Логи", callback_data="admin_logs:0"),
         InlineKeyboardButton(text="Очистка ключей", callback_data="admin_cleanup")],
        [InlineKeyboardButton(text="Пробник всем", callback_data="admin_mass_trial"),
         InlineKeyboardButton(text="Дни всем", callback_data="admin_mass_days")],
        [InlineKeyboardButton(text="Настройки", callback_data="admin_settings")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="admin_menu")],
    ])


@router.callback_query(F.data == "admin_subscription_reconcile")
async def cb_subscription_reconcile(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback, "Проверяю базу и 3x-ui…")
    from subscription_reconciliation import reconcile_subscriptions, format_report
    try:
        report = await reconcile_subscriptions()
        await callback.message.answer(format_report(report), parse_mode="HTML")
    except Exception as exc:
        logger.warning("Admin subscription reconciliation failed (%s)", type(exc).__name__)
        await callback.message.answer("Сверку завершить не удалось. Данные не изменены.")


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")]
    ])


@router.callback_query(F.data == "admin_clients_menu")
async def cb_admin_clients_menu(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    await callback.message.edit_text(
        "<b>Пользователи и подписки</b>\n\nВыберите действие.",
        parse_mode="HTML", reply_markup=_clients_kb(),
    )


@router.callback_query(F.data == "admin_finance_menu")
async def cb_admin_finance_menu(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    await callback.message.edit_text(
        "<b>Платежи и финансы</b>\n\nВыберите действие.",
        parse_mode="HTML", reply_markup=_finances_kb(),
    )


@router.callback_query(F.data == "admin_ops_menu")
async def cb_admin_ops_menu(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    await callback.message.edit_text(
        "<b>Система</b>\n\nСостояние инфраструктуры и массовые операции.",
        parse_mode="HTML", reply_markup=_operations_kb(),
    )


@router.callback_query(F.data == "admin_growth")
async def cb_admin_growth(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    await callback.message.edit_text(
        "<b>Маркетинг</b>\n\n"
        "Управление промокодами, розыгрышами, приглашениями и рекламными заявками:",
        parse_mode="HTML", reply_markup=_growth_kb(),
    )


@router.callback_query(F.data == "admin_funnel")
async def cb_admin_funnel(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    funnel = await get_conversion_funnel(30)
    totals = funnel["totals"]
    lines = [
        "📈 <b>Воронка за последние 30 дней</b>",
        "Число пользователей, впервые пришедших за период, и достигнутые ими этапы:",
        "",
    ]
    for name, label in (("started", "Пришли"), ("trial", "Запустили пробный тариф"),
                        ("paid", "Оплатили подписку"), ("connected", "Подключились"),
                        ("renewed", "Продлили")):
        lines.append(f"{label}: <b>{totals.get(name, 0)}</b>")
    sources = funnel.get("by_source", {})
    if sources:
        lines.extend(["", "<b>По источникам:</b>"])
        for source, values in sorted(sources.items(), key=lambda item: item[1].get("started", 0), reverse=True)[:8]:
            lines.append(
                f"• {escape(str(source))}: {values.get('started', 0)} → {values.get('trial', 0)} → "
                f"{values.get('paid', 0)} → {values.get('connected', 0)} → {values.get('renewed', 0)}"
            )
    lines.extend(["", "Этапы подключения считаются по первому подтверждённому признаку активности в 3x-ui."])
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_growth_kb())


@router.callback_query(F.data == "admin_checkout_funnel")
async def cb_admin_checkout_funnel(callback: CallbackQuery):
    """Verified provider payments only; never blend currencies or infer profit."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    from database import get_checkout_conversion_snapshot
    report = await get_checkout_conversion_snapshot(30)
    text = [
        "<b>Покупки новых клиентов · 30 дней</b>",
        "",
        f"Зарегистрировались: <b>{report['registered']}</b>",
        f"Открыли тарифы: <b>{report['views']}</b>",
        f"Выбрали тариф: <b>{report['selections']}</b>",
        f"Перешли к оплате: <b>{report['started']}</b>",
        f"Успешно оплатили: <b>{report['paid']}</b>",
        f"Из открывших тарифы оплатили: <b>{report['paid_from_views']}</b>",
    ]
    pct = report["purchase_pct"]
    text.append(
        f"Оплаты / открывшие тарифы: <b>{pct:.1f}%</b>"
        if pct is not None else "Оплаты / открывшие тарифы: <b>нет данных</b>"
    )
    if report["by_currency"]:
        text.extend(["", "<b>Подтверждённые суммы по валютам:</b>"])
        for currency, values in report["by_currency"].items():
            text.append(
                f"{values['gross_amount']} {escape(currency)} · {values['payments']} операций"
            )
    text.extend([
        "", "Когорта определяется датой регистрации; оплата могла быть позднее.",
        "Суммы — валовые подтверждённые платежи, не прибыль. "
        "Комиссии, возвраты, расходы на VPS и стоимость бонусов не вычтены.",
    ])
    await callback.message.edit_text(
        "\n".join(text), parse_mode="HTML", reply_markup=_growth_kb(),
    )


@router.callback_query(F.data == "admin_referral_funnel")
async def cb_admin_referral_funnel(callback: CallbackQuery):
    """Cohort-specific invitation conversions, not inflated revenue estimates."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    from growth_metrics import referral_growth_snapshot
    stats = await referral_growth_snapshot(days=30)
    text = (
        "<b>Органический рост · 30 дней</b>\n\n"
        f"Приглашено: <b>{stats['invited']}</b>\n"
        f"Приглашали: <b>{stats['advocates']}</b> пользователей\n"
        f"С первой успешной оплатой: <b>{stats['paying']}</b>\n"
        f"Без подтверждённой оплаты: <b>{stats['unconverted']}</b>\n"
        f"Конверсия: <b>{stats['conversion_pct']:.1f}%</b>\n"
        f"Начислено или ожидает начисления: <b>{stats['bonus_days']} бонусных дней</b>\n\n"
        "Когорта — пользователи, приглашённые за 30 дней. "
        "Оплаты и бонусы учитываются на момент отчёта.\n"
        "Бонусные дни — не денежная прибыль и не стоимость привлечения."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_growth_kb())


@router.callback_query(F.data == "admin_referral_risk")
async def cb_admin_referral_risk(callback: CallbackQuery):
    """Show signals for manual review, not accusations or automatic sanctions."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    from growth_metrics import referral_risk_snapshot
    report = await referral_risk_snapshot(days=1, min_invites=5)
    text = [
        "<b>Рефералы: сигналы для проверки · 24 часа</b>",
        "",
        "<b>Пригласили от 5 аккаунтов за сутки:</b>",
    ]
    if report["suspicious_accounts"]:
        for row in report["suspicious_accounts"]:
            text.append(
                f"ID <code>{row['referrer_id']}</code> · "
                f"{row['invitations']} приглашений"
            )
    else:
        text.append("Нет таких пользователей за выбранные сутки.")
    text.extend([
        "", "Это только индикаторы необычной активности, не доказательство нарушений. "
        "Начисления не блокируются автоматически.",
        "Бонусные дни не равны себестоимости и не позволяют вычислить прибыль без затрат VPS, "
        "эквайринга, поддержки и трафика.",
    ])
    await callback.message.edit_text(
        "\n".join(text), parse_mode="HTML", reply_markup=_growth_kb(),
    )


def _broadcast_controls() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="admin_broadcast_send"),
         InlineKeyboardButton(text="✖️ Отмена", callback_data="admin_broadcast_cancel")],
    ])


async def _broadcast_recipients(audience: str) -> list[int]:
    now = int(datetime.now().timestamp())
    db = await get_db()
    if audience == "all":
        query = "SELECT user_id FROM users WHERE COALESCE(is_banned,0)=0 ORDER BY user_id"
        params = ()
    elif audience == "active":
        query = (
            "SELECT DISTINCT u.user_id FROM users u JOIN keys k ON k.user_id=u.user_id "
            "WHERE k.expiry>? AND COALESCE(u.is_banned,0)=0 ORDER BY u.user_id"
        )
        params = (now,)
    else:
        query = (
            "SELECT u.user_id FROM users u WHERE COALESCE(u.is_banned,0)=0 "
            "AND NOT EXISTS (SELECT 1 FROM keys k WHERE k.user_id=u.user_id AND k.expiry>?) "
            "ORDER BY u.user_id"
        )
        params = (now,)
    cur = await db.execute(query, params)
    return [int(row[0]) for row in await cur.fetchall()]


def _parse_broadcast_buttons(raw: str) -> list[list[InlineKeyboardButton]]:
    if raw.strip() in {"", "-", "нет", "без кнопок"}:
        return []

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) > 8:
        raise ValueError("Можно добавить не больше 8 кнопок — по одной на строку.")

    rows = []
    for line in lines:
        if "|" not in line:
            raise ValueError("Формат строки: название кнопки | https://ссылка")
        label, url = (part.strip() for part in line.split("|", 1))
        parsed = urlsplit(url)
        if not label or len(label) > 64 or parsed.scheme not in {"https", "http", "tg"} or not parsed.netloc:
            raise ValueError("Проверьте название (до 64 знаков) и ссылку HTTPS/TG в каждой строке.")
        rows.append([InlineKeyboardButton(text=label, url=url)])
    return rows


def _format_refund_breakdown(stats: dict, key: str) -> str:
    """Format recorded refunds without combining incompatible currencies."""
    values = stats.get(key, {}) or {}
    if not values:
        return "нет записей"
    return "; ".join(
        f"{escape(str(currency))}: {item.get('sum', 0)} ({item.get('count', 0)} шт.)"
        for currency, item in sorted(values.items())
    )


def _format_currency_totals(values: dict) -> str:
    values = values or {}
    if not values:
        return "нет данных"
    return "; ".join(
        f"{amount} {'₽' if currency == 'RUB' else 'звёзд' if currency == 'XTR' else escape(str(currency))}"
        for currency, amount in sorted(values.items())
    )


def _user_kb(user_id: int, is_banned: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        # Row 1: View info
        [InlineKeyboardButton(text="🔑 Ключи", callback_data=f"admin_user_keys:{user_id}"),
         InlineKeyboardButton(text="💳 Платежи", callback_data=f"admin_user_pay:{user_id}")],
        # Row 2: Subscription changes
        [InlineKeyboardButton(text="⏰ Продлить", callback_data=f"admin_extend_sub:{user_id}"),
         InlineKeyboardButton(text="➕ Дни", callback_data=f"admin_add_days:{user_id}")],
        # Row 3: Access and support
        [InlineKeyboardButton(text="🎁 Пробник", callback_data=f"admin_grant_trial:{user_id}"),
         InlineKeyboardButton(text="🆕 Выдать ключ", callback_data=f"admin_grant_key:{user_id}")],
        [InlineKeyboardButton(text="Диагностика", callback_data=f"admin_client_diagnostics:{user_id}")],
        [InlineKeyboardButton(text="✉️ Написать", callback_data=f"admin_pm:{user_id}"),
         InlineKeyboardButton(text="🔄 Сброс пробника", callback_data=f"admin_reset_trial:{user_id}")],
        # Finance and moderation
        [InlineKeyboardButton(text="💰 Возврат", callback_data=f"admin_refund_user:{user_id}"),
         InlineKeyboardButton(
             text="✅ Разбанить" if is_banned else "🚫 Забанить",
             callback_data=f"admin_unban_user:{user_id}" if is_banned else f"admin_ban_user:{user_id}",
         )],
        # Destructive action and navigation
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_del_user:{user_id}"),
         InlineKeyboardButton(text="🔙 К клиентам", callback_data="admin_users:0")],
        [InlineKeyboardButton(
            text="🏠 Главное меню", callback_data="admin_menu",
        )],
    ])


def _keys_kb(keys: list, user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for k in keys:
        kid = k["id"]
        remark = (k.get("remark") or f"#{kid}")[:20]
        status = "🟢" if k.get("expiry", 0) > int(__import__('time').time()) else "🔴"
        rows.append([
            InlineKeyboardButton(text=f"{status} {remark}", callback_data=f"admin_key_detail:{kid}:{user_id}"),
            InlineKeyboardButton(text="✏️", callback_data=f"admin_edit_key:{kid}"),
            InlineKeyboardButton(text="🗑️", callback_data=f"admin_del_key_confirm:{kid}:{user_id}"),
        ])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"admin_user:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _key_detail_kb(key_id: int, user_id: int, is_active: bool) -> InlineKeyboardMarkup:
    """Keyboard for key detail view with quick actions."""
    rows = [
        # Quick extend actions
        [
            InlineKeyboardButton(text="➕ +7 дней", callback_data=f"admin_key_add_days:{key_id}:{user_id}:7"),
            InlineKeyboardButton(text="➕ +30 дней", callback_data=f"admin_key_add_days:{key_id}:{user_id}:30"),
            InlineKeyboardButton(text="➕ +90 дней", callback_data=f"admin_key_add_days:{key_id}:{user_id}:90"),
        ],
        # Edit the term and key name
        [
            InlineKeyboardButton(text="✏️ Указать срок", callback_data=f"admin_edit_key:{key_id}"),
            InlineKeyboardButton(text="🏷 Переименовать", callback_data=f"admin_rename_key:{key_id}"),
        ],
        [InlineKeyboardButton(text="🗑️ Удалить ключ", callback_data=f"admin_del_key_confirm:{key_id}:{user_id}")],
        # Navigation
        [
            InlineKeyboardButton(text="🔙 К списку ключей", callback_data=f"admin_user_keys:{user_id}"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_menu"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /admin ─────────────────────────────────────────────────────────────────

@router.message(F.text.startswith("/admin"))
async def cmd_admin(message: Message, state: FSMContext):
    logger.info(f"Admin command received from user {message.from_user.id}")
    if not _is_admin(message.from_user.id):
        logger.warning(f"Unauthorized admin attempt from user {message.from_user.id}")
        return
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(_ADMIN_HOME_TEXT, parse_mode="HTML", reply_markup=_main_kb())


@router.message(F.text.startswith("/give_trial_all"))
async def cmd_give_trial_all(message: Message):
    """Retire the old unconfirmed bulk-grant command in favor of the guarded flow."""
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "Эта команда отключена, чтобы случайно не раздать доступ всей базе. "
        "Откройте /admin → «Пробник всем»: там можно проверить условия и подтвердить выдачу пользователям без активного ключа."
    )


@router.callback_query(F.data == "admin_menu")
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    await state.clear()
    await callback.message.edit_text(_ADMIN_HOME_TEXT, parse_mode="HTML", reply_markup=_main_kb())


# ── Stats ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_stats")
async def cb_stats(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    try:
        s = await get_admin_stats()
        text = (
            "⚡ <b>ByMeVPN — Статистика</b>\n"
            "━━━━━━━━━━━━━━━\n\n"
            "👥 <b>Пользователи:</b>\n"
            f"  📊 Всего: <b>{s['total_users']}</b>\n"
            f"  ✅ Активные: <b>{s['active_users']}</b>\n"
            f"  📈 Конверсия: <b>{(s['active_users']/max(s['total_users'],1)*100):.1f}%</b>\n\n"
            "💰 <b>Доходы:</b>\n"
            f"  📅 Сегодня: <b>{s['today_revenue']} ₽</b>\n"
            f"  📆 Неделя: <b>{s['week_revenue']} ₽</b>\n"
            f"  📅 Месяц: <b>{s['month_revenue']} ₽</b>\n\n"
            f"🤝 <b>Рефералов:</b> <code>{s['total_referrals']}</code>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats"),
             InlineKeyboardButton(text="📈 Подробнее", callback_data="admin_stats_ext")],
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")],
        ])
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error("Error updating admin stats: %s", e)
        try:
            await callback.answer("❌ Ошибка обновления", show_alert=True)
        except:
            pass


@router.callback_query(F.data == "admin_stats_ext")
async def cb_stats_ext(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    try:
        s = await get_extended_stats()
        refund_stats = await get_refund_stats()
        from database import get_conversion_funnel
        funnel = await get_conversion_funnel(30)
        f = funnel["totals"]
        source_rows = sorted(funnel["by_source"].items(), key=lambda item: item[1]["started"], reverse=True)[:5]
        source_text = "\n".join(
            f"  {escape(source)}: {counts['started']} / {counts['trial']} / {counts['paid']} / {counts['connected']} / {counts['renewed']}"
            for source, counts in source_rows
        ) or "  Пока нет данных"

        top_text = ""
        if s["top_refs"]:
            top_text = "\n👑 <b>Топ рефераторов:</b>\n"
            for i, r in enumerate(s["top_refs"], 1):
                top_text += f"  {i}. ID {r['user_id']} — {r['count']} платных рефералов\n"

        device_dist = s.get("device_dist", {2: 0, 5: 0, 10: 0})

        text = (
            "📈 <b>Детальная статистика ByMeVPN</b>\n\n"
            "👤 <b>Пользователи:</b>\n"
            f"  📅 За 24ч: {s['new_day']}\n"
            f"  📆 За неделю: {s['new_week']}\n"
            f"  📅 За месяц: {s['new_month']}\n"
            f"  📊 Конверсия Trial → Оплата: <b>{s.get('conversion_trial_pct', 0)}%</b> ({s.get('paid_users', 0)} / {s.get('trial_users', 0)})\n"
            f"  🌐 Общая конверсия в оплату: <b>{s.get('conversion_overall_pct', 0)}%</b>\n\n"
            f"🧭 <b>Воронка за {funnel['days']} дней</b>\n"
            f"Пришли: <b>{f['started']}</b> → начали пробный тариф: <b>{f['trial']}</b> → оплатили: <b>{f['paid']}</b> → подключились: <b>{f['connected']}</b> → продлили: <b>{f['renewed']}</b>\n"
            "По источникам (пришли / пробный / оплата / подключились / продлили):\n"
            f"{source_text}\n"
            "Считаются пользователи, впервые запустившие бота после включения этой аналитики.\n\n"
            "📱 <b>Устройства (активные ключи):</b>\n"
            f"  📱 2 устройства: <b>{device_dist.get(2, 0)}</b>\n"
            f"  📱 5 устройств: <b>{device_dist.get(5, 0)}</b>\n"
            f"  📱 10 устройств: <b>{device_dist.get(10, 0)}</b>\n\n"
            "💳 <b>Шлюзы и стабильность:</b>\n"
            f"  ⏳ YooKassa Pending: {s.get('pending_yk', 0)}\n"
            f"  ⏳ CryptoBot Pending: {s.get('pending_crypto', 0)}\n"
            f"  ⚠️ Ошибки provisioning: {s.get('key_errors_count', 0)}\n\n"
            "💰 <b>Возвраты:</b>\n"
            f"  📅 30 дней: {refund_stats['count_30d']} — {_format_refund_breakdown(refund_stats, 'by_currency_30d')}\n"
            f"  🔢 Всего: {refund_stats['count_total']} — {_format_refund_breakdown(refund_stats, 'by_currency_total')}\n"
            f"{top_text}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats_ext")],
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")],
        ])
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error("Error updating admin extended stats: %s", e)
        try:
            await callback.answer("❌ Ошибка обновления", show_alert=True)
        except:
            pass


# ── Broadcast ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_broadcast")
async def cb_broadcast(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    await state.clear()
    await callback.message.edit_text(
        "📢 <b>Новая рассылка</b>\n\nВыберите аудиторию:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Все пользователи", callback_data="admin_broadcast_audience:all")],
            [InlineKeyboardButton(text="✅ С активной подпиской", callback_data="admin_broadcast_audience:active")],
            [InlineKeyboardButton(text="🌱 Без активной подписки", callback_data="admin_broadcast_audience:inactive")],
            [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_menu")],
        ]),
    )


@router.callback_query(F.data.startswith("admin_broadcast_audience:"))
async def cb_broadcast_audience(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    audience = callback.data.split(":", 1)[1]
    if audience not in {"all", "active", "inactive"}:
        await safe_answer(callback, "Неизвестная аудитория.", alert=True); return
    await safe_answer(callback)
    recipients = await _broadcast_recipients(audience)
    audience_names = {"all": "все пользователи", "active": "активные подписчики", "inactive": "пользователи без активной подписки"}
    await state.clear()
    await state.update_data(broadcast_audience=audience)
    await state.set_state(AdminFlow.broadcast_content)
    await callback.message.edit_text(
        f"📢 <b>Аудитория:</b> {audience_names[audience]} — <b>{len(recipients)}</b> получателей.\n\n"
        "Отправьте сообщение для рассылки: текст, фото, видео, GIF, файл, аудио или голосовое.\n"
        "Подпись, форматирование и premium-эмодзи сохранятся. Для отмены отправьте /cancel.",
        parse_mode="HTML", reply_markup=_back_kb(),
    )


@router.message(StateFilter(AdminFlow.broadcast_content))
async def receive_broadcast_content(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    if (message.text or "").strip().lower() in {"/cancel", "отмена"}:
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=_main_kb())
        return
    has_content = bool(
        message.text or message.photo or message.video or message.animation or message.document
        or message.audio or message.voice or message.video_note or message.sticker
        or message.poll or message.contact or message.location
    )
    if not has_content:
        await message.answer("Не удалось распознать сообщение. Отправьте текст или поддерживаемое медиа.")
        return
    await state.update_data(
        broadcast_source_chat_id=message.chat.id,
        broadcast_source_message_id=message.message_id,
    )
    await state.set_state(AdminFlow.broadcast_buttons)
    await message.answer(
        "🔗 <b>Кнопки под сообщением</b>\n\n"
        "Добавьте ссылочную кнопку: задайте её текст, ссылку и цвет. Можно добавить до 8 кнопок.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="admin_broadcast_add_button")],
            [InlineKeyboardButton(text="Без кнопок", callback_data="admin_broadcast_no_buttons")],
            [InlineKeyboardButton(text="Отмена", callback_data="admin_broadcast_cancel")],
        ]),
    )


@router.message(StateFilter(AdminFlow.broadcast_buttons))
async def receive_broadcast_buttons(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    if (message.text or "").strip().lower() in {"/cancel", "отмена"}:
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=_main_kb())
        return
    await message.answer("Используйте кнопки под предыдущим сообщением, чтобы добавить ссылку и выбрать цвет.")


async def _broadcast_show_preview(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    button_rows = data.get("broadcast_button_rows", [])
    audience = data.get("broadcast_audience", "all")
    recipients = await _broadcast_recipients(audience)
    markup = InlineKeyboardMarkup(inline_keyboard=button_rows) if button_rows else None
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=data["broadcast_source_chat_id"],
            message_id=data["broadcast_source_message_id"],
            reply_markup=markup,
        )
    except Exception:
        logger.exception("Broadcast preview copy failed for admin %d", chat_id)
        await bot.send_message(chat_id, "❌ Не получилось показать предпросмотр. Попробуйте отправить исходное сообщение ещё раз.")
        await state.set_state(AdminFlow.broadcast_content)
        return

    audience_names = {"all": "все пользователи", "active": "активные подписчики", "inactive": "пользователи без активной подписки"}
    await state.set_state(AdminFlow.broadcast_confirm)
    await bot.send_message(
        chat_id,
        f"👆 Предпросмотр рассылки выше.\n\n"
        f"Получатели: <b>{len(recipients)}</b> — {audience_names.get(audience, 'все пользователи')}.\n"
        f"Кнопок: <b>{len(button_rows)}</b>.\n\nОтправить сейчас?",
        parse_mode="HTML", reply_markup=_broadcast_controls(),
    )


@router.callback_query(F.data == "admin_broadcast_add_button")
async def cb_broadcast_add_button(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    if await state.get_state() != AdminFlow.broadcast_buttons.state:
        await safe_answer(callback, "Сначала подготовьте сообщение.", alert=True); return
    data = await state.get_data()
    if len(data.get("broadcast_button_rows", [])) >= 8:
        await safe_answer(callback, "Можно добавить не больше 8 кнопок.", alert=True); return
    await safe_answer(callback)
    await state.set_state(AdminFlow.broadcast_button_label)
    await callback.message.answer("Введите текст кнопки (до 64 символов). Для отмены рассылки — /cancel.")


@router.message(StateFilter(AdminFlow.broadcast_button_label))
async def receive_broadcast_button_label(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    if (message.text or "").strip().lower() in {"/cancel", "отмена"}:
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=_main_kb())
        return
    label = (message.text or "").strip()
    if not label or len(label) > 64:
        await message.answer("Текст кнопки должен содержать от 1 до 64 символов. Попробуйте ещё раз.")
        return
    await state.update_data(broadcast_new_button_label=label)
    await state.set_state(AdminFlow.broadcast_button_url)
    await message.answer("Отправьте ссылку, которая откроется при нажатии. Используйте полный адрес, начинающийся с https://")


@router.message(StateFilter(AdminFlow.broadcast_button_url))
async def receive_broadcast_button_url(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    if (message.text or "").strip().lower() in {"/cancel", "отмена"}:
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=_main_kb())
        return
    url = (message.text or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        await message.answer("Ссылка должна быть корректным HTTPS-адресом, например https://example.com")
        return
    await state.update_data(broadcast_new_button_url=url)
    await state.set_state(AdminFlow.broadcast_button_color)
    await message.answer(
        "Выберите цвет кнопки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔵 Синий", callback_data="admin_broadcast_color:primary", style="primary")],
            [InlineKeyboardButton(text="🟢 Зелёный", callback_data="admin_broadcast_color:success", style="success")],
            [InlineKeyboardButton(text="🔴 Красный", callback_data="admin_broadcast_color:danger", style="danger")],
            [InlineKeyboardButton(text="Отмена", callback_data="admin_broadcast_cancel")],
        ]),
    )


@router.callback_query(F.data.startswith("admin_broadcast_color:"))
async def cb_broadcast_color(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    if await state.get_state() != AdminFlow.broadcast_button_color.state:
        await safe_answer(callback, "Выбор цвета устарел.", alert=True); return
    style = callback.data.split(":", 1)[1]
    if style not in {"primary", "success", "danger"}:
        await safe_answer(callback, "Неизвестный цвет.", alert=True); return
    data = await state.get_data()
    button = InlineKeyboardButton(
        text=data["broadcast_new_button_label"],
        url=data["broadcast_new_button_url"],
        style=style,
    )
    rows = list(data.get("broadcast_button_rows", []))
    rows.append([button.model_dump(exclude_none=True)])
    await state.update_data(broadcast_button_rows=rows)
    await state.set_state(AdminFlow.broadcast_buttons)
    await safe_answer(callback, "Цвет кнопки сохранён")
    await callback.message.answer(
        f"Кнопка добавлена. Всего: {len(rows)} из 8.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            *([[InlineKeyboardButton(text="➕ Добавить ещё", callback_data="admin_broadcast_add_button")]] if len(rows) < 8 else []),
            [InlineKeyboardButton(text="👀 Предпросмотр", callback_data="admin_broadcast_buttons_done")],
            [InlineKeyboardButton(text="Отмена", callback_data="admin_broadcast_cancel")],
        ]),
    )


@router.callback_query(F.data == "admin_broadcast_no_buttons")
async def cb_broadcast_no_buttons(callback: CallbackQuery, bot: Bot, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    if await state.get_state() != AdminFlow.broadcast_buttons.state:
        await safe_answer(callback, "Создание рассылки устарело.", alert=True); return
    await state.update_data(broadcast_button_rows=[])
    await safe_answer(callback)
    await _broadcast_show_preview(bot, callback.message.chat.id, state)


@router.callback_query(F.data == "admin_broadcast_buttons_done")
async def cb_broadcast_buttons_done(callback: CallbackQuery, bot: Bot, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    if await state.get_state() != AdminFlow.broadcast_buttons.state:
        await safe_answer(callback, "Создание рассылки устарело.", alert=True); return
    await safe_answer(callback)
    await _broadcast_show_preview(bot, callback.message.chat.id, state)


@router.callback_query(F.data == "admin_broadcast_cancel")
async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback, "Рассылка отменена.")
    await state.clear()
    await callback.message.edit_text("Рассылка отменена.", reply_markup=_main_kb())


@router.callback_query(F.data == "admin_broadcast_send")
async def cb_broadcast_send(callback: CallbackQuery, bot: Bot, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    data = await state.get_data()
    if await state.get_state() != AdminFlow.broadcast_confirm.state or not data.get("broadcast_source_message_id"):
        await safe_answer(callback, "Черновик рассылки устарел. Создайте её заново.", alert=True)
        await state.clear()
        return
    await safe_answer(callback)
    recipients = await _broadcast_recipients(data.get("broadcast_audience", "all"))
    button_rows = data.get("broadcast_button_rows", [])
    campaign_id = await create_broadcast_campaign(
        source_chat_id=data["broadcast_source_chat_id"],
        source_message_id=data["broadcast_source_message_id"],
        buttons=button_rows,
        audience=data.get("broadcast_audience", "all"),
        recipients=recipients,
    )
    await state.clear()
    await callback.message.edit_text(
        f"📬 <b>Рассылка #{campaign_id} поставлена в очередь</b>\n\n"
        f"Получателей: {len(recipients)}. Сообщение будет отправлено в фоне; бот продолжит рассылку, даже если перезапустится.\n"
        "Итог придёт сюда отдельным сообщением.",
        parse_mode="HTML", reply_markup=_back_kb(),
    )
    await log_admin_action(
        admin_id=callback.from_user.id,
        action_type="broadcast_started",
        action_details=f"campaign={campaign_id}; audience={data.get('broadcast_audience', 'all')}; recipients={len(recipients)}; buttons={len(button_rows)}",
    )


# ── User list ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_users:"))
async def cb_user_list(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    try:
        page = int(callback.data.split(":")[1])
        per_page = 10
        offset = page * per_page
        total = await get_users_count()
        users = await get_all_users_paginated(limit=per_page, offset=offset)
        await _fill_missing_usernames(callback.bot, users)

        if not users:
            await callback.message.edit_text(
                "Пользователей нет.", reply_markup=_back_kb()
            ); return

        lines = [f"👥 <b>Пользователи</b> (стр. {page+1}):\n"]
        for u in users:
            reg = fmt_date(u["created"]) if u["created"] else "?"
            lines.append(
                f"• <code>{u['user_id']}</code> — "
                f"@{escape(u['username']) if u.get('username') else 'нет username'} — "
                f"{'✅' if u.get('active_keys', 0) > 0 else '❌'} "
                f"ключей: {u.get('total_keys', 0)}, рег: {reg}"
            )

        # Pagination + user buttons
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀", callback_data=f"admin_users:{page-1}"))
        if offset + per_page < total:
            nav_row.append(InlineKeyboardButton(text="▶", callback_data=f"admin_users:{page+1}"))

        user_rows = [
            [InlineKeyboardButton(
                text=f"👤 @{u['username']}" if u.get('username') else f"👤 {u['user_id']}",
                callback_data=f"admin_user:{u['user_id']}"
            )]
            for u in users
        ]

        kb_rows = user_rows
        if nav_row:
            kb_rows = kb_rows + [nav_row]
        kb_rows.append([InlineKeyboardButton(text="🔍 Найти по ID или username", callback_data="admin_search")])
        kb_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_menu")])

        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
        )
    except Exception as e:
        logger.error("Error in admin user list: %s", e)
        try:
            await callback.answer("❌ Ошибка загрузки", show_alert=True)
        except:
            pass


# ── Extended User list with Keys ───────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_users_ext:"))
async def cb_user_list_ext(callback: CallbackQuery):
    """Extended user list showing keys info inline."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    page = int(callback.data.split(":")[1])
    per_page = 5  # Fewer users per page since we show keys
    offset = page * per_page
    total = await get_users_count()
    users = await get_all_users_paginated(limit=per_page, offset=offset)
    await _fill_missing_usernames(callback.bot, users)

    if not users:
        await callback.message.edit_text(
            "Пользователей нет.", reply_markup=_back_kb()
        ); return

    import time
    now = int(time.time())
    lines = [f"📋 <b>Пользователи + Ключи</b> (стр. {page+1}):\n"]

    for u in users:
        uid = u['user_id']
        keys = await get_user_keys(uid)

        # User header
        reg = fmt_date(u["created"]) if u["created"] else "?"
        lines.append(
            f"\n👤 <code>{uid}</code> | @{escape(u['username']) if u.get('username') else 'нет username'} | "
            f"{'✅' if u.get('active_keys', 0) > 0 else '❌'} "
            f"Ключей: {len(keys)} | Рег: {reg}"
        )

        # Show keys inline
        if keys:
            for k in keys:
                status = "✅" if k["expiry"] > now else "❌"
                days_left = (k["expiry"] - now) // 86400
                key_name = k.get('remark', f"#{k['id']}")[:15]
                lines.append(
                    f"   {status} <b>{key_name}</b> — {days_left}дн. "
                    f"(до {fmt_date(k['expiry'])[:10]})"
                )
        else:
            lines.append("   🚫 Нет ключей")

    # Pagination
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀", callback_data=f"admin_users_ext:{page-1}"))
    if offset + per_page < total:
        nav_row.append(InlineKeyboardButton(text="▶", callback_data=f"admin_users_ext:{page+1}"))

    kb_rows = [
        [InlineKeyboardButton(text="🔍 Поиск по ID или username", callback_data="admin_search")],
    ]
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([
        InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu"),
        InlineKeyboardButton(text="👥 Обычный список", callback_data="admin_users:0")
    ])

    text = "\n".join(lines)
    if len(text) > 4000:  # Telegram limit
        text = text[:3990] + "\n... (обрезано)"

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


# ── All Keys View ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_all_keys:"))
async def cb_all_keys(callback: CallbackQuery):
    """Show all keys from all users with pagination."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    try:
        page = int(callback.data.split(":")[1])
        per_page = 10
        offset = page * per_page
        total = await get_keys_count()
        keys = await get_all_keys_paginated(limit=per_page, offset=offset)

        if not keys:
            await callback.message.edit_text(
                "🗝 <b>Все ключи</b>\n\n"
                "📭 Ключей пока нет.\n\n"
                "🔙 Вернуться в меню для создания ключей или просмотра пользователей.",
                parse_mode="HTML", reply_markup=_back_kb()
            ); return

        import time
        now = int(time.time())
        lines = [f"🗝 <b>Все ключи</b> (стр. {page+1} из {(total//per_page)+1}):\n"]
        lines.append(f"📊 Всего ключей: {total}\n")

        for k in keys:
            status = "✅" if k["is_active"] else "❌"
            days_left = (k["expiry"] - now) // 86400 if k["expiry"] > now else 0
            total_paid = k.get("total_paid") or 0
            paid = "💰" if total_paid > 0 else "🆓"
            key_name = k.get('remark', f"#{k['id']}")[:12]

            lines.append(
                f"{status} <b>{key_name}</b> | 👤 {k['user_id']} | "
                f"{paid} | {days_left}дн. | 📅 {fmt_date(k['expiry'])[:10]}"
            )

        # Pagination
        nav_row = []
        if page > 0:
            nav_row.app…22608 tokens truncated…y_id = int(callback.data.split(":")[1])
    await state.set_state(AdminFlow.edit_key_name)
    await state.update_data(edit_key_id=key_id)
    
    await callback.message.edit_text(
        f"🏷️ <b>Переименование ключа #{key_id}</b>\n\n"
        "Введите новое название для ключа:\n"
        "• Будет добавлено 🇺🇸 автоматически\n"
        "• Например: <code>Мой VPN</code> станет <code>🇺🇸 Мой VPN</code>\n\n"
        "Или отправьте <code>-</code> для автоматического имени",
        parse_mode="HTML", reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.edit_key_name))
async def receive_key_name(message: Message, state: FSMContext):
    """Receive new key name and update in database and 3x-UI."""
    if not _is_admin(message.from_user.id):
        return
    
    data = await state.get_data()
    key_id = data.get("edit_key_id")
    
    if not key_id:
        await message.answer("❌ Ошибка сессии. Начните заново.")
        await state.clear()
        return
    
    # Get new name and ensure USA emoji
    new_name = message.text.strip()
    if new_name == "-":
        new_name = f"USA Ключ #{key_id}"
    elif not new_name.startswith("🇺🇸"):
        new_name = f"🇺🇸 {new_name}"
    
    # Update in database
    success_db = await update_key_remark(key_id, new_name)
    
    # Update in 3x-UI panel
    from database import get_key_by_id
    key = await get_key_by_id(key_id)
    # 3x-ui: skip panel update of client name

    await state.clear()

    if success_db:
        await message.answer(
            f"✅ <b>Ключ переименован!</b>\n\n"
            f"📛 Новое название: <code>{new_name}</code>\n"
            f"🗄️ База данных: обновлена\n\n"
            f"Ключ работает с новым названием!",
            parse_mode="HTML", reply_markup=_back_kb()
        )
    else:
        await message.answer(
            "❌ Не удалось переименовать ключ. Попробуйте позже.",
            reply_markup=_back_kb()
        )


# ── Grant Key to User ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin_grant_key:"))
async def cb_grant_key(callback: CallbackQuery, state: FSMContext):
    """Start granting a key to specific user."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    
    user_id = int(callback.data.split(":")[1])
    await state.set_state(AdminFlow.grant_key_days)
    await state.update_data(grant_key_user_id=user_id)
    
    await callback.message.edit_text(
        f"🆕 <b>Выдача ключа пользователю {user_id}</b>\n\n"
        "Введите количество дней для ключа:\n"
        "• Например: <code>30</code> — ключ на 30 дней\n"
        "• Например: <code>365</code> — ключ на год\n\n"
        f"Ключ будет создан автоматически с названием 🇺🇸 Admin-Key-{user_id}",
        parse_mode="HTML", reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.grant_key_days))
async def receive_grant_key_days(message: Message, bot: Bot, state: FSMContext):
    """Receive days and create key for user."""
    if not _is_admin(message.from_user.id):
        return
    
    try:
        days = int(message.text.strip())
        if days < 1 or days > MAX_GRANT_DAYS:
            raise ValueError
    except ValueError:
        await message.answer(f"❌ Введите число дней от 1 до {MAX_GRANT_DAYS}."); return

    data = await state.get_data()
    await state.clear()
    uid = data.get("grant_key_user_id")
    
    if not uid:
        await message.answer("❌ Ошибка сессии. Начните заново."); return
    
    # Create key for user
    from subscription import deliver_key
    
    config_name = f"🇺🇸 Admin-Key-{uid}"
    success = await deliver_key(
        bot=bot,
        user_id=uid,
        chat_id=uid,
        config_name=config_name,
        days=days,
        limit_ip=2,
        is_paid=False,
        amount=0,
        currency="RUB",
        method="admin_grant",
        payload=f"admin_grant_{uid}_{days}",
        extend_existing=False,  # Always create new key for admin grants
    )
    
    if success:
        await message.answer(
            f"✅ <b>Ключ выдан!</b>\n\n"
            f"👤 Пользователь: <code>{uid}</code>\n"
            f"📅 Срок: {days} дней\n"
            f"📱 Устройств: до 2\n"
            f"🏷️ Название: <b>{config_name}</b>\n\n"
            f"Ключ отправлен пользователю в личные сообщения!",
            parse_mode="HTML", reply_markup=_back_kb()
        )
    else:
        await message.answer(
            f"❌ Не удалось выдать ключ пользователю <code>{uid}</code>.\n"
            "Проверьте подключение к 3x-ui в логах.",
            parse_mode="HTML", reply_markup=_back_kb()
        )


# ── Export Keys CSV ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_export_keys_csv")
async def cb_export_keys_csv(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)
    await callback.message.edit_text("⏳ Генерирую CSV со всеми ключами...", reply_markup=_back_kb())
    
    from database import get_all_keys_csv
    csv_text = await get_all_keys_csv()
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"bymevpn_keys_{date_str}.csv"
    file_bytes = csv_text.encode("utf-8")
    await bot.send_document(
        chat_id=callback.from_user.id,
        document=BufferedInputFile(file_bytes, filename=filename),
        caption=f"🗝 Экспорт ключей ByMeVPN\n{datetime.now().strftime('%d.%m.%Y %H:%M')}",
    )
    await callback.message.edit_text(
        "✅ CSV с ключами отправлен выше.", reply_markup=_back_kb()
    )


# ── Promo Codes ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_promo_codes")
async def cb_promo_codes(callback: CallbackQuery):
    """Show promo codes management."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    promo_codes = await get_all_promo_codes()

    text = "🎁 <b>Управление промокодами</b>\n\n"

    kb_rows = []

    if promo_codes:
        text += "<b>Промокоды:</b>\n"
        for p in promo_codes[:10]:
            status = "✅" if p["is_active"] else "❌"
            expiry_str = fmt_date(p["expires_at"])[:10]
            uses = f"{p['uses_count']}/{p['max_uses']}" if p['max_uses'] < 999999 else f"{p['uses_count']}/∞"

            if p["promo_type"] == "percent":
                value_text = f"{p['discount_value']}%"
            elif p["promo_type"] == "fixed_rub":
                value_text = f"{p['discount_value']} ₽"
            elif p["promo_type"] == "free_days":
                value_text = f"+{p['discount_value']} дней"
            else:
                value_text = str(p['discount_value'])

            tariff_text = f" (Тариф: {p['tariff_binding']} мес.)" if p['tariff_binding'] else ""

            text += f"{status} <code>{p['code']}</code> — {value_text}{tariff_text} (исп. {uses}) до {expiry_str}\n"
            kb_rows.append([InlineKeyboardButton(text=f"{p['code']}", callback_data=f"admin_promo_manage:{p['code']}")])
    else:
        text += "📭 Промокодов пока нет.\n\n"
        text += "➕ Создайте первый промокод для привлечения новых пользователей!"

    kb_rows.append([InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin_promo_create")])
    kb_rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_promo_codes")])
    kb_rows.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="admin_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── Promo Code Management (Individual) ─────────────────────────────────────

@router.callback_query(F.data.startswith("admin_promo_manage:"))
async def cb_promo_manage(callback: CallbackQuery):
    """Show individual promo code management options."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    code = callback.data.split(":")[1]
    promo = await validate_promo_code(code)

    if not promo:
        await callback.message.edit_text("❌ Промокод не найден.", reply_markup=_back_kb())
        return

    status = "✅ Активен" if promo["is_active"] else "❌ Неактивен"
    expiry_str = fmt_date(promo["expires_at"])[:10]
    uses = f"{promo['uses_count']}/{promo['max_uses']}" if promo['max_uses'] < 999999 else f"{promo['uses_count']}/∞"

    if promo["promo_type"] == "percent":
        value_text = f"{promo['discount_value']}%"
    elif promo["promo_type"] == "fixed_rub":
        value_text = f"{promo['discount_value']} ₽"
    elif promo["promo_type"] == "free_days":
        value_text = f"+{promo['discount_value']} дней"
    else:
        value_text = str(promo['discount_value'])

    tariff_text = f" (Тариф: {promo['tariff_binding']} мес.)" if promo['tariff_binding'] else ""

    text = (
        f"🎁 <b>Промокод: {code}</b>\n\n"
        f"Статус: {status}\n"
        f"Тип: {value_text}{tariff_text}\n"
        f"Использований: {uses}\n"
        f"Истекает: {expiry_str}\n\n"
        f"<b>Действия:</b>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_promo_delete:{code}")],
        [InlineKeyboardButton(text="⏳ Продлить на 30 дней", callback_data=f"admin_promo_extend:{code}:30")],
        [InlineKeyboardButton(text="⏳ Продлить на 90 дней", callback_data=f"admin_promo_extend:{code}:90")],
        [InlineKeyboardButton(text="🔄 Изменить лимит", callback_data=f"admin_promo_edit_uses:{code}")],
        [InlineKeyboardButton(text="🔛 Активировать" if not promo["is_active"] else "🔴 Деактивировать",
                             callback_data=f"admin_promo_toggle:{code}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_promo_codes")]
    ])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("admin_promo_delete:"))
async def cb_promo_delete(callback: CallbackQuery):
    """Delete promo code."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    code = callback.data.split(":")[1]
    success = await delete_promo_code(code)

    if success:
        await log_admin_action(
            admin_id=callback.from_user.id,
            action_type="delete_promo_code",
            action_details=f"Deleted promo code {code}"
        )
        await callback.message.edit_text(f"✅ Промокод <code>{code}</code> удалён.", reply_markup=_back_kb())
    else:
        await callback.message.edit_text("❌ Ошибка удаления промокода.", reply_markup=_back_kb())


@router.callback_query(F.data.startswith("admin_promo_extend:"))
async def cb_promo_extend(callback: CallbackQuery):
    """Extend promo code validity."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    parts = callback.data.split(":")
    code = parts[1]
    days = int(parts[2])

    success = await extend_promo_code(code, days)

    if success:
        await log_admin_action(
            admin_id=callback.from_user.id,
            action_type="extend_promo_code",
            action_details=f"Extended promo code {code} by {days} days"
        )
        await callback.message.edit_text(f"✅ Промокод <code>{code}</code> продлён на {days} дней.", reply_markup=_back_kb())
    else:
        await callback.message.edit_text("❌ Ошибка продления промокода.", reply_markup=_back_kb())


@router.callback_query(F.data.startswith("admin_promo_edit_uses:"))
async def cb_promo_edit_uses(callback: CallbackQuery, state: FSMContext):
    """Start editing promo code max uses."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    code = callback.data.split(":")[1]
    await state.update_data(promo_edit_code=code)
    await state.set_state(AdminFlow.promo_edit_uses)
    await callback.message.edit_text(
        f"🎁 <b>Изменение лимита для {code}</b>\n\nВведите новый лимит использований (или 999999 для безлимита):",
        reply_markup=_back_kb()
    )


@router.message(AdminFlow.promo_edit_uses)
async def msg_promo_edit_uses(message: Message, state: FSMContext):
    """Handle promo code max uses edit."""
    if not _is_admin(message.from_user.id):
        return

    try:
        new_max = int(message.text.strip())
        if new_max < 1:
            await message.answer("❌ Минимальное значение: 1.")
            return

        data = await state.get_data()
        code = data["promo_edit_code"]

        success = await update_promo_max_uses(code, new_max)

        if success:
            await log_admin_action(
                admin_id=message.from_user.id,
                action_type="update_promo_uses",
                action_details=f"Updated promo code {code} max uses to {new_max}"
            )
            await message.answer(f"✅ Лимит промокода <code>{code}</code> изменён на {new_max}.", reply_markup=_main_kb())
        else:
            await message.answer("❌ Ошибка изменения лимита.", reply_markup=_back_kb())

        await state.clear()
    except ValueError:
        await message.answer("❌ Введите число.")


@router.callback_query(F.data.startswith("admin_promo_toggle:"))
async def cb_promo_toggle(callback: CallbackQuery):
    """Toggle promo code active status."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True); return
    await safe_answer(callback)

    code = callback.data.split(":")[1]
    promo = await validate_promo_code(code)

    if not promo:
        await callback.message.edit_text("❌ Промокод не найден.", reply_markup=_back_kb())
        return

    new_status = not promo["is_active"]
    success = await toggle_promo_active(code, new_status)

    if success:
        await log_admin_action(
            admin_id=callback.from_user.id,
            action_type="toggle_promo_active",
            action_details=f"{'Activated' if new_status else 'Deactivated'} promo code {code}"
        )
        status_text = "активирован" if new_status else "деактивирован"
        await callback.message.edit_text(f"✅ Промокод <code>{code}</code> {status_text}.", reply_markup=_back_kb())
    else:
        await callback.message.edit_text("❌ Ошибка изменения статуса.", reply_markup=_back_kb())


# ── Create Promo Code ────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_promo_create")
async def cb_promo_create(callback: CallbackQuery, state: FSMContext):
    """Start promo code creation flow."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    await safe_answer(callback)
    await state.set_state(AdminFlow.promo_code)
    await callback.message.edit_text(
        "🎁 <b>Создание промокода</b>\n\nВведите код промокода (минимум 4 символа, только буквы и цифры):",
        reply_markup=_back_kb()
    )


@router.message(AdminFlow.promo_code)
async def receive_promo_code(message: Message, state: FSMContext):
    """Receive promo code string."""
    if not _is_admin(message.from_user.id):
        return

    code = message.text.strip().upper() if message.text else ""
    if len(code) < 4:
        await message.answer("❌ Код должен быть минимум 4 символа.")
        return
    if not code.isalnum():
        await message.answer("❌ Код должен содержать только буквы и цифры.")
        return

    existing = await validate_promo_code(code)
    if existing:
        await message.answer("❌ Такой промокод уже существует.")
        await state.clear()
        return

    await state.update_data(promo_code=code)
    await state.set_state(AdminFlow.promo_type)
    await message.answer(
        f"Код: <code>{code}</code>\n\nВыберите тип промокода:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Процент (%)", callback_data="promo_type_percent")],
            [InlineKeyboardButton(text="Фиксированная сумма (₽)", callback_data="promo_type_fixed")],
            [InlineKeyboardButton(text="Бесплатные дни", callback_data="promo_type_days")],
            [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_menu")]
        ])
    )


@router.callback_query(F.data.startswith("promo_type_"))
async def cb_promo_type(callback: CallbackQuery, state: FSMContext):
    """Handle promo type selection."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    promo_type = callback.data.split("_")[2]
    await state.update_data(promo_type=promo_type)
    await state.set_state(AdminFlow.promo_value)

    if promo_type == "percent":
        prompt = "Введите процент скидки (например: 10 для 10%):"
    elif promo_type == "fixed":
        prompt = "Введите сумму скидки в рублях (например: 50):"
    else:  # days
        prompt = "Введите количество бесплатных дней (например: 7):"

    await callback.message.edit_text(prompt, reply_markup=_back_kb())


@router.message(AdminFlow.promo_value)
async def receive_promo_value(message: Message, state: FSMContext):
    """Receive discount value."""
    if not _is_admin(message.from_user.id):
        return

    try:
        value = float(message.text.strip())
        data = await state.get_data()
        promo_type = data["promo_type"]

        if not math.isfinite(value):
            await message.answer("❌ Введите конечное число.")
            return
        if promo_type in ("fixed", "days") and not value.is_integer():
            await message.answer("❌ Введите целое число рублей или дней.")
            return

        if promo_type == "percent" and (value < 1 or value > 100):
            await message.answer("❌ Процент должен быть от 1 до 100.")
            return
        if promo_type == "fixed" and (value < 1 or value > 10000):
            await message.answer("❌ Сумма должна быть от 1 до 10000 ₽.")
            return
        if promo_type == "days" and (value < 1 or value > 365):
            await message.answer("❌ Количество дней должно быть от 1 до 365.")
            return

        await state.update_data(discount_value=int(value) if promo_type in ("fixed", "days") else value)
        await state.set_state(AdminFlow.promo_uses)
        await message.answer(
            "Введите максимальное количество использований (например: 100 или 999999 для безлимита):",
            reply_markup=_back_kb()
        )
    except ValueError:
        await message.answer("❌ Введите число.")


@router.message(AdminFlow.promo_uses)
async def receive_promo_uses(message: Message, state: FSMContext):
    """Receive max uses count."""
    if not _is_admin(message.from_user.id):
        return

    try:
        max_uses = int(message.text.strip())
        if max_uses < 1:
            await message.answer("❌ Минимальное значение: 1.")
            return

        await state.update_data(max_uses=max_uses)
        await state.set_state(AdminFlow.promo_days)
        await message.answer(
            "Введите срок действия в днях (например: 30):",
            reply_markup=_back_kb()
        )
    except ValueError:
        await message.answer("❌ Введите число.")


@router.message(AdminFlow.promo_days)
async def receive_promo_days(message: Message, state: FSMContext):
    """Receive validity period and create promo code."""
    if not _is_admin(message.from_user.id):
        return

    try:
        days = int(message.text.strip())
        if days < 1 or days > 3650:
            await message.answer("❌ Количество дней должно быть от 1 до 3650.")
            return

        data = await state.get_data()
        code = data["promo_code"]
        promo_type = data["promo_type"]
        discount_value = data["discount_value"]
        max_uses = data["max_uses"]

        success = await create_promo_code(
            code=code,
            promo_type=promo_type,
            discount_value=discount_value,
            max_uses=max_uses,
            valid_days=days
        )

        if success:
            await log_admin_action(
                admin_id=message.from_user.id,
                action_type="create_promo_code",
                action_details=f"Created promo code {code}, type={promo_type}, value={discount_value}, max_uses={max_uses}, days={days}"
            )
            await message.answer(
                f"✅ Промокод <code>{code}</code> успешно создан!",
                reply_markup=_main_kb()
            )
        else:
            await message.answer("❌ Ошибка создания промокода.", reply_markup=_back_kb())

        await state.clear()
    except ValueError:
        await message.answer("❌ Введите число.")


# ── Mass trial distribution ───────────────────────────────────────────────────

@router.callback_query(F.data == "admin_mass_trial")
async def cb_mass_trial(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    
    await safe_answer(callback)
    
    # Show confirmation dialog
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, выдать всем", callback_data="admin_mass_trial_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="admin_menu")
        ]
    ])
    
    await callback.message.edit_text(
        "🎁 <b>Массовая выдача пробников</b>\n\n"
        "Выдать 3-дневный пробный период ВСЕМ пользователям без активных ключей?\n\n"
        "⚠️ Это может занять некоторое время.",
        parse_mode="HTML",
        reply_markup=kb
    )


@router.callback_query(F.data == "admin_mass_trial_confirm")
async def cb_mass_trial_confirm(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    
    await safe_answer(callback)
    
    # Get all users without active keys
    from database import get_db
    db = await get_db()
    cur = await db.execute(
        "SELECT DISTINCT u.user_id "
        "FROM users u "
        "WHERE COALESCE(u.is_banned, 0)=0 "
        "AND NOT EXISTS (SELECT 1 FROM keys k WHERE k.user_id=u.user_id AND k.expiry>?)",
        (int(datetime.now().timestamp()),),
    )
    rows = await cur.fetchall()
    
    if not rows:
        await callback.message.edit_text(
            "✅ Нет пользователей без активных ключей.",
            reply_markup=_back_kb()
        )
        return
    
    # Process trials
    success_count = 0
    failed_count = 0
    
    status_msg = await callback.message.edit_text(
        f"⏳ Выдаю пробники {len(rows)} пользователям...\n"
        f"✅ Успешно: 0\n"
        f"❌ Ошибок: 0",
        reply_markup=_back_kb()
    )
    
    for i, (user_id,) in enumerate(rows):
        try:
            # Check if already used trial
            from database import has_trial_used
            if await has_trial_used(user_id):
                continue
            
            # Deliver trial key
            from subscription import deliver_key
            success = await deliver_key(
                bot=bot,
                user_id=user_id,
                chat_id=user_id,
                config_name="🇷🇺 Россия",
                days=3,
                limit_ip=1,
                is_paid=False,
                method="trial",
                extend_existing=False,  # Always create new key for trials
            )
            
            if success:
                success_count += 1
            else:
                failed_count += 1
                
        except Exception as e:
            logger.error(f"Failed to deliver trial to {user_id}: {e}")
            failed_count += 1
        
        # Update status every 5 users
        if (i + 1) % 5 == 0 or i == len(rows) - 1:
            try:
                await status_msg.edit_text(
                    f"⏳ Выдаю пробники... ({i+1}/{len(rows)})\n"
                    f"✅ Успешно: {success_count}\n"
                    f"❌ Ошибок: {failed_count}",
                    reply_markup=_back_kb()
                )
            except:
                pass
        
        # Small delay to avoid rate limits
        await asyncio.sleep(0.5)
    
    # Final report
    await status_msg.edit_text(
        f"✅ <b>Массовая выдача завершена</b>\n\n"
        f"📊 Всего пользователей: {len(rows)}\n"
        f"✅ Успешно выдано: {success_count}\n"
        f"❌ Ошибок: {failed_count}\n"
        f"⏭️ Пропущено (уже пробовали): {len(rows) - success_count - failed_count}",
        parse_mode="HTML",
        reply_markup=_back_kb()
    )


@router.callback_query(F.data == "admin_mass_trial_5d")
async def cb_mass_trial_5d(callback: CallbackQuery, bot: Bot):
    """Give 5-day trial to ALL users (not just those without keys)."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    
    await safe_answer(callback)
    
    # Show confirmation dialog
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, выдать всем 5 дней", callback_data="admin_mass_trial_5d_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="admin_menu")
        ]
    ])
    
    await callback.message.edit_text(
        "🎁 <b>Массовая выдача пробников — 5 дней</b>\n\n"
        "Выдать 5-дневный пробный период ВСЕМ пользователям?\n\n"
        "⚠️ Это может занять некоторое время.",
        parse_mode="HTML",
        reply_markup=kb
    )


@router.callback_query(F.data == "admin_mass_trial_5d_confirm")
async def cb_mass_trial_5d_confirm(callback: CallbackQuery, bot: Bot):
    """Confirm and execute 5-day mass trial distribution to ALL users."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    
    await safe_answer(callback)
    
    # Get ALL users
    db = await get_db()
    cur = await db.execute("SELECT user_id FROM users")
    rows = await cur.fetchall()
    
    if not rows:
        await callback.message.edit_text(
            "ℹ️ В базе нет пользователей.",
            reply_markup=_back_kb()
        )
        return
    
    # Process trials
    success_count = 0
    failed_count = 0
    skipped_count = 0
    
    status_msg = await callback.message.edit_text(
        f"⏳ Выдаю пробники 5 дней {len(rows)} пользователям...\n"
        f"✅ Успешно: 0\n"
        f"❌ Ошибок: 0",
        reply_markup=_back_kb()
    )
    
    from subscription import deliver_key
    
    for i, (user_id,) in enumerate(rows):
        try:
            # Reset trial flag so user can receive
            await reset_trial_for_user(user_id)
            
            # Deliver 5-day trial key
            success = await deliver_key(
                bot=bot,
                user_id=user_id,
                chat_id=user_id,
                config_name=f"Trial5d-{user_id}",
                days=5,
                limit_ip=2,
                is_paid=False,
                amount=0,
                currency="RUB",
                method="admin_mass_trial_5d",
                payload=f"mass_trial_5d_{user_id}",
                extend_existing=False,  # Always create new key for mass trials
            )
            
            if success:
                success_count += 1
                logger.info(f"Mass trial 5d: delivered to user {user_id}")
            else:
                failed_count += 1
                logger.warning(f"Mass trial 5d: failed to deliver to user {user_id}")
                
        except Exception as e:
            failed_count += 1
            logger.error(f"Mass trial 5d: error delivering to {user_id}: {e}")
        
        # Update status every 5 users
        if (i + 1) % 5 == 0 or i == len(rows) - 1:
            try:
                await status_msg.edit_text(
                    f"⏳ Выдаю пробники 5 дней... ({i+1}/{len(rows)})\n"
                    f"✅ Успешно: {success_count}\n"
                    f"❌ Ошибок: {failed_count}",
                    reply_markup=_back_kb()
                )
            except:
                pass
        
        # Small delay to avoid rate limits
        await asyncio.sleep(0.3)
    
    # Final report
    await status_msg.edit_text(
        f"✅ <b>Массовая выдача завершена</b>\n\n"
        f"📊 <b>Параметры:</b> 5 дней каждому\n"
        f"👥 <b>Всего пользователей:</b> {len(rows)}\n"
        f"✅ <b>Успешно выдано:</b> {success_count}\n"
        f"❌ <b>Ошибок:</b> {failed_count}",
        parse_mode="HTML",
        reply_markup=_back_kb()
    )
    
    logger.info(f"Mass trial 5d completed: {success_count} success, {failed_count} failed out of {len(rows)} users")


# ---------------------------------------------------------------------------
# Key Errors
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_key_errors:"))
async def cb_admin_key_errors(callback: CallbackQuery, bot: Bot):
    """Show key issuance errors with pagination."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    await safe_answer(callback)
    offset = int(callback.data.split(":")[1])

    errors = await get_key_errors(limit=20, offset=offset)
    total_count = await get_key_errors_count()

    if not errors:
        await callback.message.edit_text(
            "✅ Ошибок выдачи ключей нет.",
            reply_markup=_back_kb()
        )
        return

    import json
    text = f"❌ <b>Ошибки выдачи ключей</b>\n\nВсего: {total_count}\n\n"

    for err in errors:
        error_date = datetime.fromtimestamp(err["created"]).strftime("%d.%m.%Y %H:%M")
        context = json.loads(err["context"]) if err["context"] else {}
        text += (
            f"🆔 ID: {err['id']}\n"
            f"👤 User: {err['user_id']}\n"
            f"📅 Дата: {error_date}\n"
            f"🔧 Тип: {err['error_type']}\n"
            f"📝 Ошибка: {err['error_message'][:100] if err['error_message'] else 'N/A'}...\n"
            f"📋 Контекст: {json.dumps(context, ensure_ascii=False)[:100] if context else 'N/A'}...\n"
            f"─────────────\n"
        )

    # Pagination buttons
    kb_rows = []
    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_key_errors:{max(0, offset - 20)}"))
    if len(errors) == 20:  # Assume there might be more
        nav_row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_key_errors:{offset + 20}"))
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


# ---------------------------------------------------------------------------
# Admin Logs
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_logs:"))
async def cb_admin_logs(callback: CallbackQuery, bot: Bot):
    """Show admin action logs."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    await safe_answer(callback)
    offset = int(callback.data.split(":")[1])

    logs = await get_admin_logs(limit=20, offset=offset)

    if not logs:
        await callback.message.edit_text(
            "📋 <b>Логи админа</b>\n\nЗаписей нет.",
            reply_markup=_back_kb()
        )
        return

    text = "📋 <b>Логи действий админа</b>\n\n"
    for log in logs:
        log_date = datetime.fromtimestamp(log["created"]).strftime("%d.%m.%Y %H:%M")
        target = f"User: {log['target_user_id']}" if log['target_user_id'] else ""
        text += (
            f"🆔 Admin: {log['admin_id']}\n"
            f"📅 {log_date}\n"
            f"🔧 {log['action_type']}\n"
            f"📝 {log['action_details'][:80]}...\n"
            f"{target}\n"
            f"─────────────\n"
        )

    # Pagination buttons
    kb_rows = []
    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_logs:{max(0, offset - 20)}"))
    if len(logs) == 20:  # Assume there might be more
        nav_row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_logs:{offset + 20}"))
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


# ---------------------------------------------------------------------------
# User Ban/Unban
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_ban_user:"))
async def cb_admin_ban_user(callback: CallbackQuery, state: FSMContext):
    """Start user ban flow."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    await safe_answer(callback)
    user_id = int(callback.data.split(":")[1])
    await state.update_data(ban_target_user_id=user_id)
    await state.set_state(AdminFlow.ban_reason)

    await callback.message.edit_text(
        f"🚫 <b>Бан пользователя {user_id}</b>\n\n"
        "Введите причину бана (или отправьте /skip для бана без причины):",
        reply_markup=_back_kb()
    )


@router.message(AdminFlow.ban_reason)
async def msg_ban_reason(message: Message, state: FSMContext):
    """Handle ban reason input."""
    if not _is_admin(message.from_user.id):
        return

    reason = message.text.strip() if not message.text.startswith("/skip") else None
    data = await state.get_data()
    user_id = data["ban_target_user_id"]

    success = await ban_user(user_id, reason)

    if success:
        await log_admin_action(
            admin_id=message.from_user.id,
            action_type="ban_user",
            action_details=f"Banned user {user_id}, reason: {reason}",
            target_user_id=user_id
        )
        await message.answer(
            f"✅ Пользователь {user_id} забанен.",
            reply_markup=_main_kb()
        )
    else:
        await message.answer("❌ Ошибка бана пользователя.")

    await state.clear()


@router.callback_query(F.data.startswith("admin_unban_user:"))
async def cb_admin_unban_user(callback: CallbackQuery):
    """Unban user."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    await safe_answer(callback)
    user_id = int(callback.data.split(":")[1])

    success = await unban_user(user_id)

    if success:
        await log_admin_action(
            admin_id=callback.from_user.id,
            action_type="unban_user",
            action_details=f"Unbanned user {user_id}",
            target_user_id=user_id
        )
        await callback.message.edit_text(
            f"✅ Пользователь {user_id} разбанен.",
            reply_markup=_back_kb()
        )
    else:
        await callback.message.edit_text("❌ Ошибка разбана пользователя.")


# ---------------------------------------------------------------------------
# Manual Days Addition
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_add_days:"))
async def cb_admin_add_days(callback: CallbackQuery, state: FSMContext):
    """Start manual days addition flow."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    await safe_answer(callback)
    user_id = int(callback.data.split(":")[1])
    await state.update_data(add_days_target_user_id=user_id)
    await state.set_state(AdminFlow.add_days_value)

    await callback.message.edit_text(
        f"➕ <b>Добавление дней пользователю {user_id}</b>\n\n"
        "Введите количество дней для добавления:",
        reply_markup=_back_kb()
    )


@router.message(AdminFlow.add_days_value)
async def msg_add_days_value(message: Message, state: FSMContext):
    """Handle days value input."""
    if not _is_admin(message.from_user.id):
        return

    try:
        days = int(message.text.strip())
        if days < 1 or days > 3650:
            await message.answer("❌ Количество дней должно быть от 1 до 3650.")
            return

        data = await state.get_data()
        user_id = data["add_days_target_user_id"]

        success = await add_manual_days(user_id, days, message.from_user.id)

        if success:
            await log_admin_action(
                admin_id=message.from_user.id,
                action_type="add_manual_days",
                action_details=f"Added {days} days to user {user_id}",
                target_user_id=user_id
            )
            await message.answer(
                f"✅ Добавлено {days} дней пользователю {user_id}.",
                reply_markup=_main_kb()
            )
        else:
            await message.answer(
                "❌ У пользователя нет ключей. Сначала выдайте ключ.",
                reply_markup=_back_kb()
            )

        await state.clear()
    except ValueError:
        await message.answer("❌ Введите число.")


# ---------------------------------------------------------------------------
# Finance Statistics
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin_finance")
async def cb_admin_finance(callback: CallbackQuery, bot: Bot):
    """Show detailed finance statistics."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    await safe_answer(callback)

    stats = await get_admin_stats()
    payment_stats = await get_payment_stats()
    refund_stats = await get_refund_stats()
    received_by_currency = payment_stats.get("total", {}).get("by_currency", {})

    text = (
        "💰 <b>Финансовая статистика</b>\n\n"
        f"📊 <b>Общая выручка:</b> {stats['total_revenue']} ₽\n"
        f"📅 <b>Выручка за сегодня:</b> {stats['today_revenue']} ₽\n"
        f"⭐ <b>Получено Stars:</b> {received_by_currency.get('XTR', 0)} звёзд\n"
        f"💳 <b>Всего платежей:</b> {stats['total_payments']}\n"
        f"👥 <b>Всего пользователей:</b> {stats['total_users']}\n"
        f"✅ <b>Активных пользователей:</b> {stats['active_users']}\n"
        f"🗝 <b>Активных ключей:</b> {stats['active_keys']}\n\n"
        f"💸 <b>Возвраты в журнале:</b> {_format_refund_breakdown(refund_stats, 'by_currency_total')}\n"
        f"📈 <b>Записей возврата:</b> {refund_stats.get('count_total', 0)}\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# System Settings
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin_settings")
async def cb_admin_settings(callback: CallbackQuery, bot: Bot):
    """Show system settings."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    await safe_answer(callback)

    from constants import (
        PRICE_CONFIG, TRIAL_DAYS, REFERRAL_SIGNUP_BONUS_DAYS,
        REFERRAL_FIRST_PAYMENT_BONUS_DAYS,
    )
    from config import SUPPORT_USERNAME, ADMIN_IDS

    text = (
        "⚙️ <b>Системные настройки</b>\n\n"
        f"📊 <b>Тарифы:</b>\n"
        f"   1 месяц: {PRICE_CONFIG[1][0]} ₽ ({PRICE_CONFIG[1][1]} дней)\n"
        f"   3 месяца: {PRICE_CONFIG[3][0]} ₽ ({PRICE_CONFIG[3][1]} дней)\n"
        f"   6 месяцев: {PRICE_CONFIG[6][0]} ₽ ({PRICE_CONFIG[6][1]} дней)\n"
        f"   12 месяцев: {PRICE_CONFIG[12][0]} ₽ ({PRICE_CONFIG[12][1]} дней)\n\n"
        f"🎁 <b>Бонусы:</b>\n"
        f"   Пробный период: {TRIAL_DAYS} дней\n"
        f"   За регистрацию приглашённого: +{REFERRAL_SIGNUP_BONUS_DAYS} дней\n"
        f"   За его первую платную подписку: +{REFERRAL_FIRST_PAYMENT_BONUS_DAYS} дней (автоматически)\n\n"
        f"👨‍💼 <b>Админы:</b> {', '.join(map(str, ADMIN_IDS))}\n"
        f"📞 <b>Поддержка:</b> {SUPPORT_USERNAME}\n\n"
        f"<i>Для изменения настроек отредактируйте файлы constants.py и config.py на сервере.</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Mass Actions
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin_mass_days")
async def cb_admin_mass_days(callback: CallbackQuery, state: FSMContext):
    """Start mass days extension flow."""
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return

    await safe_answer(callback)
    await state.set_state(AdminFlow.mass_extend_days)
    
    await callback.message.edit_text(
        "➕ <b>Выдать дни всем пользователям</b>\n\n"
        "Введите количество дней для добавления (1-365):",
        reply_markup=_back_kb()
    )


@router.message(AdminFlow.mass_extend_days)
async def msg_mass_extend_days(message: Message, state: FSMContext):
    """Handle mass extend days input."""
    if not _is_admin(message.from_user.id):
        return

    try:
        days = int(message.text.strip())
        if days < 1 or days > 365:
            await message.answer("❌ Количество дней должно быть от 1 до 365.")
            return
    except ValueError:
        await message.answer("❌ Укажите число от 1 до 365.")
        return
    
    db = await get_db()
    cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM keys")
    eligible_count = int((await cur.fetchone())[0] or 0)
    await state.update_data(mass_extend_days=days)
    await state.set_state(AdminFlow.mass_extend_days_confirm)
    await message.answer(
        f"⚠️ Добавить <b>{days} дней</b> пользователям с ключами?\n\n"
        f"Под действие попадут до <b>{eligible_count}</b> пользователей.\n"
        "Подтвердите массовое начисление:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data="admin_mass_days_confirm"),
             InlineKeyboardButton(text="✖️ Отмена", callback_data="admin_mass_days_cancel")],
        ]),
    )


@router.callback_query(F.data == "admin_mass_days_cancel")
async def cb_admin_mass_days_cancel(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback, "Отменено.")
    await state.clear()
    await callback.message.edit_text("Массовое начисление отменено.", reply_markup=_main_kb())


@router.callback_query(F.data == "admin_mass_days_confirm")
async def cb_admin_mass_days_confirm(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    data = await state.get_data()
    days = data.get("mass_extend_days")
    if await state.get_state() != AdminFlow.mass_extend_days_confirm.state or not days:
        await safe_answer(callback, "Подтверждение устарело. Начните заново.", alert=True)
        await state.clear()
        return
    await safe_answer(callback)
    await state.clear()

    db = await get_db()
    cur = await db.execute("SELECT user_id FROM users")
    rows = await cur.fetchall()
    if not rows:
        await callback.message.edit_text("ℹ️ В базе нет пользователей.", reply_markup=_main_kb())
        return

    status_msg = await callback.message.edit_text(
        f"⏳ Добавляю {days} дней пользователям с ключами...\n"
        "✅ Успешно: 0 · ⏭️ Пропущено: 0 · ⚠️ Ошибки: 0"
    )
    success_count = failed_count = skipped_count = 0
    for i, (user_id,) in enumerate(rows):
        try:
            success = await add_manual_days(user_id, days, callback.from_user.id)
            if success:
                success_count += 1
            else:
                skipped_count += 1
        except Exception:
            failed_count += 1
            logger.exception("Mass extend failed for user %s", user_id)

        if (i + 1) % 100 == 0 or i == len(rows) - 1:
            try:
                await status_msg.edit_text(
                    f"⏳ Обработано: {i + 1} из {len(rows)}\n"
                    f"✅ Начислено: {success_count} · ⏭️ Без ключа: {skipped_count} · ⚠️ Ошибки: {failed_count}"
                )
            except Exception:
                pass
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ <b>Массовое начисление завершено</b>\n\n"
        f"Дней каждому: <b>{days}</b>\n"
        f"✅ Начислено: {success_count}\n⏭️ Без ключа: {skipped_count}\n⚠️ Ошибки: {failed_count}",
        parse_mode="HTML", reply_markup=_back_kb(),
    )
    
    await log_admin_action(
        admin_id=callback.from_user.id,
        action_type="mass_extend",
        action_details=f"Mass extend: {success_count} success, {failed_count} failed, {skipped_count} skipped",
    )


# ---------------------------------------------------------------------------
# Giveaways Management
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin_giveaways")
async def cb_admin_giveaways(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)

    giveaways = await get_active_giveaways()
    
    rows = [
        [InlineKeyboardButton(text="➕ Создать розыгрыш", callback_data="admin_new_giveaway")]
    ]

    text = "🎉 <b>Управление розыгрышами</b>\n\n"
    if not giveaways:
        text += "В данный момент активных розыгрышей нет."
    else:
        text += f"Активных розыгрышей: <b>{len(giveaways)}</b>\n\n"
        for gw in giveaways:
            gw_id = gw["id"]
            title = gw["title"]
            prize = gw["prize_days"]
            winners = gw["winner_count"]
            end_t = fmt_date(gw["end_time"])
            text += (
                f"🎁 <b>#{gw_id} {title}</b>\n"
                f"   Приз: +{prize} дней | Победителей: {winners}\n"
                f"   Завершение: {end_t}\n"
                f"   Команда для участников: <code>/start gw_{gw_id}</code>\n\n"
            )
            rows.append([
                InlineKeyboardButton(text=f"❌ Отменить #{gw_id}", callback_data=f"admin_cancel_gw:{gw_id}"),
            ])

    rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="admin_menu")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin_new_giveaway")
async def cb_admin_new_giveaway(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    await state.set_state(AdminFlow.giveaway_title)
    await callback.message.edit_text(
        "🎉 <b>Создание розыгрыша</b>\n\n"
        "Введите <b>название розыгрыша</b>:\n"
        "<i>(например: Весенний розыгрыш 30 дней ByMeVPN)</i>",
        parse_mode="HTML",
        reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.giveaway_title))
async def handle_gw_title(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    title = message.text.strip()
    if not title:
        await message.answer("❌ Название не может быть пустым. Попробуйте еще раз.")
        return
    await state.update_data(gw_title=title)
    await state.set_state(AdminFlow.giveaway_prize)
    await message.answer(
        f"🎁 Название: <b>{title}</b>\n\n"
        "Теперь введите <b>количество призовых дней VPN</b> (например: 30):",
        parse_mode="HTML",
        reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.giveaway_prize))
async def handle_gw_prize(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    try:
        prize_days = int(message.text.strip())
        if prize_days <= 0 or prize_days > MAX_GRANT_DAYS:
            raise ValueError
    except ValueError:
        await message.answer(f"❌ Введите положительное целое число дней от 1 до {MAX_GRANT_DAYS} (например: 30):")
        return
    await state.update_data(gw_prize=prize_days)
    await state.set_state(AdminFlow.giveaway_winners)
    await message.answer(
        f"📅 Приз: <b>+{prize_days} дней</b>\n\n"
        "Введите <b>количество победителей</b> (например: 3):",
        parse_mode="HTML",
        reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.giveaway_winners))
async def handle_gw_winners(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    try:
        winners = int(message.text.strip())
        if winners <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное число победителей (например: 3):")
        return
    await state.update_data(gw_winners=winners)
    await state.set_state(AdminFlow.giveaway_duration)
    await message.answer(
        f"👥 Победителей: <b>{winners}</b>\n\n"
        "Введите <b>длительность розыгрыша в часах</b> (например: 24 или 72):",
        parse_mode="HTML",
        reply_markup=_back_kb()
    )


@router.message(StateFilter(AdminFlow.giveaway_duration))
async def handle_gw_duration(message: Message, bot: Bot, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    try:
        duration_h = int(message.text.strip())
        if duration_h <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное число часов (например: 24):")
        return

    data = await state.get_data()
    title = data.get("gw_title", "Розыгрыш ByMeVPN")
    prize_days = data.get("gw_prize", 30)
    winners = data.get("gw_winners", 1)
    await state.clear()

    import time
    end_time = int(time.time()) + duration_h * 3600
    gw_id = await create_giveaway(
        title=title,
        description="Розыгрыш VPN-дней ByMeVPN",
        prize_days=prize_days,
        winners_count=winners,
        end_date=end_time,
        created_by=message.from_user.id,
    )

    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username or "ByMeVPN_bot"
    except Exception:
        bot_username = "ByMeVPN_bot"

    deep_link = f"https://t.me/{bot_username}?start=gw_{gw_id}"

    text = (
        "✅ <b>Розыгрыш успешно создан!</b>\n\n"
        f"🎁 <b>#{gw_id} {title}</b>\n"
        f"📅 Приз: <b>+{prize_days} дней</b>\n"
        f"👥 Победителей: <b>{winners}</b>\n"
        f"⏳ Завершится: <b>{fmt_date(end_time)}</b>\n\n"
        f"🔗 <b>Ссылка для участия:</b>\n"
        f"<code>{deep_link}</code>\n\n"
        "Опубликуйте эту ссылку в канале или разошлите пользователям."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_main_kb())


@router.callback_query(F.data.startswith("admin_draw_gw:"))
async def cb_admin_draw_gw(callback: CallbackQuery, bot: Bot):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    import time
    gw_id = int(callback.data.split(":")[1])
    
    gw = await get_giveaway_by_id(gw_id)
    if not gw:
        await callback.message.edit_text("❌ Розыгрыш не найден.", reply_markup=_back_kb())
        return
    if gw["end_date"] > int(time.time()):
        await callback.message.edit_text(
            f"🎉 Итоги розыгрыша #{gw_id} бот подведёт автоматически после {fmt_date(gw['end_date'])}.",
            reply_markup=_back_kb(),
        )
        return
    from giveaway_automation import process_due_giveaways
    await process_due_giveaways(bot)
    await callback.message.edit_text(
        f"🏆 Итоги розыгрыша #{gw_id} подведены автоматически. Проверьте отчёт от бота.",
        reply_markup=_main_kb(),
    )


@router.callback_query(F.data.startswith("admin_cancel_gw:"))
async def cb_admin_cancel_gw(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", alert=True)
        return
    await safe_answer(callback)
    gw_id = int(callback.data.split(":")[1])
    await set_giveaway_status(gw_id, "cancelled")
    await callback.message.edit_text(f"❌ Розыгрыш #{gw_id} отменен.", reply_markup=_main_kb())

