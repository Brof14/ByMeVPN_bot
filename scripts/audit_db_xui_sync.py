"""
READ-ONLY Diagnostic Script: Compare SQLite Database keys vs 3x-ui Clients.
DOES NOT MODIFY ANY DATA.
Run via: docker run --rm -v /opt/ByMeVPN_bot:/app -w /app bymevpn_bot-vpnbot python scripts/audit_db_xui_sync.py
"""
import os
import sys
import json
import time
import asyncio
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import database
import xui_client
from constants import format_timestamp


async def run_audit():
    print("=" * 80)
    print("READ-ONLY AUDIT: SQLite DB vs 3x-ui Clients Synchronization")
    print(f"Timestamp: {datetime.now().isoformat()}")
    print("=" * 80)

    # 1. Fetch DB keys
    db = await database.get_db()
    cur = await db.execute("""
        SELECT k.user_id, k.id as key_id, k.uuid, k.expiry, k.limit_ip, k.key as sub_url, k.remark
        FROM keys k
        ORDER BY k.user_id, k.expiry DESC
    """)
    db_rows = await cur.fetchall()

    db_keys_by_user = {}
    for row in db_rows:
        uid = row[0]
        if uid not in db_keys_by_user:
            db_keys_by_user[uid] = []
        db_keys_by_user[uid].append({
            "key_id": row[1],
            "uuid": row[2],
            "expiry": row[3],
            "limit_ip": row[4],
            "sub_url": row[5],
            "remark": row[6],
        })

    # 2. Fetch 3x-ui clients via API
    try:
        api = await xui_client._get_api()
        inbounds = await xui_client._api_call_with_retry(api.inbound.get_list)
    except Exception as e:
        print(f"ERROR connecting to 3x-ui: {e}")
        return

    xui_clients_by_email = {}
    for ib in inbounds:
        client_stats = ib.client_stats or []
        # Parse settings
        try:
            settings = json.loads(ib.settings)
            clients = settings.get("clients", [])
            for c in clients:
                email = str(c.get("email", "")).strip()
                if not email:
                    continue
                if email not in xui_clients_by_email:
                    xui_clients_by_email[email] = {
                        "inbounds": [],
                        "uuid": c.get("id"),
                        "sub_id": c.get("subId", ""),
                        "expiry_time": c.get("expiryTime", 0),
                        "limit_ip": c.get("limitIp", 0),
                        "enable": c.get("enable", True),
                    }
                xui_clients_by_email[email]["inbounds"].append(ib.id)
        except Exception as e:
            print(f"Warning: could not parse settings for inbound {ib.id}: {e}")

    print(f"\nTotal users in SQLite keys: {len(db_keys_by_user)}")
    print(f"Total client accounts in 3x-ui: {len(xui_clients_by_email)}")

    discrepancies = []
    in_sync_count = 0

    now_ts = int(time.time())

    # Check each 3x-ui client against DB
    for email, xclient in xui_clients_by_email.items():
        is_numeric_tg = email.isdigit()
        user_id = int(email) if is_numeric_tg else None

        if not is_numeric_tg:
            discrepancies.append({
                "account": email,
                "category": "NAMED_ACCOUNT_IN_3XUI",
                "db_state": "Not present in SQLite keys (admin/manual account)",
                "xui_state": f"UUID: {xclient['uuid']}, inbounds: {xclient['inbounds']}",
                "recommended_action": "DO NOT DELETE. Keep as administrative/special client.",
            })
            continue

        db_keys = db_keys_by_user.get(user_id)
        if not db_keys:
            discrepancies.append({
                "account": str(user_id),
                "category": "IN_3XUI_MISSING_IN_DB",
                "db_state": "Missing from SQLite keys table",
                "xui_state": f"UUID: {xclient['uuid']}, expiry: {format_timestamp(xclient['expiry_time']//1000 if xclient['expiry_time'] else 0)}, inbounds: {xclient['inbounds']}",
                "recommended_action": "Safe import into SQLite keys on next renewal or user interaction (zero destruction).",
            })
            continue

        # Client exists in both DB and 3x-ui!
        active_db_key = db_keys[0]  # most recent key
        xui_uuid = xclient.get("uuid")
        db_uuid = active_db_key.get("uuid")
        xui_expiry = (xclient.get("expiry_time", 0) or 0) // 1000
        db_expiry = active_db_key.get("expiry", 0)

        # Check UUID match
        uuid_match = (xui_uuid and db_uuid and xui_uuid.lower() == db_uuid.lower())
        # Check expiry diff (allow up to 60 seconds clock skew)
        expiry_diff = abs(xui_expiry - db_expiry)
        expiry_match = expiry_diff < 120 or (xui_expiry == 0 and db_expiry == 0)

        if uuid_match and expiry_match:
            in_sync_count += 1
        else:
            issues = []
            if not uuid_match:
                issues.append(f"UUID mismatch (DB: {db_uuid}, 3x-ui: {xui_uuid})")
            if not expiry_match:
                issues.append(f"Expiry mismatch (DB: {format_timestamp(db_expiry)}, 3x-ui: {format_timestamp(xui_expiry)})")

            discrepancies.append({
                "account": str(user_id),
                "category": "ATTRIBUTE_MISMATCH",
                "db_state": f"UUID: {db_uuid}, expiry: {format_timestamp(db_expiry)}, limit_ip: {active_db_key.get('limit_ip')}",
                "xui_state": f"UUID: {xui_uuid}, expiry: {format_timestamp(xui_expiry)}, limit_ip: {xclient.get('limit_ip')}",
                "recommended_action": f"Align attributes safely: {'; '.join(issues)}. Preserve 3x-ui UUID.",
            })

    # Check DB keys missing in 3x-ui
    for uid, keys in db_keys_by_user.items():
        if str(uid) not in xui_clients_by_email:
            active_key = keys[0]
            is_active = active_key.get("expiry", 0) > now_ts
            discrepancies.append({
                "account": str(uid),
                "category": "IN_DB_MISSING_IN_3XUI",
                "db_state": f"Expiry: {format_timestamp(active_key.get('expiry'))} (active: {is_active})",
                "xui_state": "Client not found in 3x-ui inbounds",
                "recommended_action": "Provision in 3x-ui upon next user request/renewal." if is_active else "Expired historical record. No action needed.",
            })

    print(f"\nPerfect Sync: {in_sync_count} clients")
    print(f"Total Discrepancies/Notices: {len(discrepancies)}")
    print("-" * 80)

    for item in discrepancies:
        print(f"[{item['category']}] Account: {item['account']}")
        print(f"  DB State:  {item['db_state']}")
        print(f"  3x-ui:     {item['xui_state']}")
        print(f"  Recommend: {item['recommended_action']}")
        print()

    print("=" * 80)
    print("AUDIT COMPLETE. NO CHANGES WERE MADE.")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_audit())
