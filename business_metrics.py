"""Read-only acquisition and operating economics; no changes to payment flows.

Do not mix RUB and XTR. Expenses come from operator-supplied *30-day actual*
numbers. Missing cost inputs intentionally prevent a fabricated profit figure.
"""
from __future__ import annotations

import os
import time

from database import get_checkout_conversion_snapshot, get_db

COST_FIELDS = {
    "vps": "BYMEVPN_COSTS_30D_VPS_RUB",
    "marketing": "BYMEVPN_COSTS_30D_MARKETING_RUB",
    "staff": "BYMEVPN_COSTS_30D_STAFF_RUB",
    "processing": "BYMEVPN_COSTS_30D_PROCESSING_RUB",
    "other": "BYMEVPN_COSTS_30D_OTHER_RUB",
}


def reported_costs() -> dict[str, int | None]:
    """Only parse positive, explicit amounts; never treat unknown as zero."""
    result = {}
    for name, key in COST_FIELDS.items():
        raw = os.getenv(key, "").strip()
        if not raw:
            result[name] = None
            continue
        if not raw.isdecimal() or len(raw) > 12:
            raise ValueError(f"Invalid non-negative RUB amount in {key}")
        result[name] = int(raw)
    return result


async def economic_overview(days: int = 30) -> dict:
    """Historical revenue and refund ledger with independent funnel milestones.

    The 30-day manually supplied expenses are only comparable to a 30-day
    report. Known RUB balance is *partial* whenever a currency is unconverted.
    """
    days = int(days)
    if days != 30:
        raise ValueError("Expense reporting currently supports exactly 30 days")
    cutoff = int(time.time()) - days * 86400
    db = await get_db()

    cur = await db.execute(
        """SELECT currency, COUNT(*), COALESCE(SUM(amount),0)
           FROM payments WHERE status='success' AND amount>0 AND created>=?
           GROUP BY currency ORDER BY currency""",
        (cutoff,),
    )
    payments = {
        str(currency): {"count": int(count), "gross": int(total)}
        for currency, count, total in await cur.fetchall()
    }
    cur = await db.execute(
        """SELECT currency, COUNT(*), COALESCE(SUM(amount),0)
           FROM refunds WHERE created>=?
           GROUP BY currency ORDER BY currency""",
        (cutoff,),
    )
    refunds = {
        str(currency): {"count": int(count), "recorded_amount": int(total)}
        for currency, count, total in await cur.fetchall()
    }
    funnel = await get_checkout_conversion_snapshot(days)
    costs = reported_costs()
    known_costs = all(value is not None for value in costs.values())
    recorded_expense_total = sum(costs.values()) if known_costs else None
    rub_balance = (
        payments.get("RUB", {}).get("gross", 0)
        - refunds.get("RUB", {}).get("recorded_amount", 0)
        - recorded_expense_total
    ) if known_costs else None

    steps = (
        ("registered", "Регистрации"),
        ("views", "Просмотр тарифов"),
        ("selections", "Выбор тарифа"),
        ("started", "Начатая оплата"),
        ("paid", "Первая оплата"),
    )
    # Counts are *independent* registration-cohort milestones: customers can
    # buy directly without a trial, and some paths skip the Telegram plan view.
    milestones = [
        {"key": key, "label": label, "users": int(funnel.get(key, 0))}
        for key, label in steps
    ]
    return {
        "days": days,
        "payments": payments,
        "recorded_refunds": refunds,
        "costs_rub": costs,
        "costs_complete": known_costs,
        "known_rub_balance": rub_balance,
        "milestones": milestones,
        "paid_from_views": int(funnel.get("paid_from_views", 0)),
        "paid_from_views_pct": funnel.get("purchase_pct"),
        "cac_marketing_rub": (
            costs["marketing"] / funnel["paid"]
            if costs["marketing"] is not None and funnel.get("paid") else None
        ),
        "ltv_rub": None,  # Requires actual time-based customer cohorts.
        "warnings": (
            "RUB balance is not company net profit: XTR is not converted; "
            "refunds are only those recorded locally; other real-world costs "
            "and unrecorded provider fees may exist."
        ),
    }
