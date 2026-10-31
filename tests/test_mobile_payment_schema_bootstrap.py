"""Ensure an older database missing mobile checkout tables is upgraded safely."""
import os
import secrets
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestMobilePaymentSchemaBootstrap(unittest.TestCase):
    def test_missing_mobile_payment_table_is_recreated_and_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="bymevpn-schema-test-") as temp_dir:
            env = dict(os.environ)
            env["DB_FILE"] = os.path.join(temp_dir, "legacy.sqlite3")
            env["JWT_SECRET"] = secrets.token_urlsafe(48)
            env["PYTHONPATH"] = os.pathsep.join(
                value for value in (PROJECT_ROOT, env.get("PYTHONPATH", "")) if value
            )
            script = r"""
import asyncio
import database

async def main():
    await database.init_db()
    db = await database.get_db()
    await db.execute("DROP TABLE mobile_payment_requests")
    await db.commit()
    await db.close()
    database._db_pool = None

    await database.init_db()
    db = await database.get_db()
    columns = {
        row[1] for row in await (await db.execute(
            "PRAGMA table_info(mobile_payment_requests)"
        )).fetchall()
    }
    required = {
        "user_id", "request_id", "plan_code", "provider", "payment_id",
        "confirmation_url", "devices", "generation", "creation_state", "created_at",
    }
    assert required <= columns

    first = await database.ensure_mobile_payment_request(
        987654321, "schema-bootstrap-test", "1m", "yookassa", 5
    )
    second = await database.ensure_mobile_payment_request(
        987654321, "schema-bootstrap-test", "1m", "yookassa", 5
    )
    assert first["devices"] == second["devices"] == 5
    assert await database.claim_mobile_payment_creation(
        987654321, "schema-bootstrap-test", "1m", "yookassa"
    )
    assert not await database.claim_mobile_payment_creation(
        987654321, "schema-bootstrap-test", "1m", "yookassa"
    )
    await db.close()

asyncio.run(main())
"""
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
