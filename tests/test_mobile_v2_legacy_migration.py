"""Idempotent additive migration contract against a legacy-shaped SQLite DB."""
import sqlite3
import tempfile
import unittest
from pathlib import Path


class LegacyMigrationTests(unittest.TestCase):
    def test_migration_is_additive_and_repeatable(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as f:
            db = sqlite3.connect(f.name)
            db.executescript("CREATE TABLE users(user_id INTEGER PRIMARY KEY); INSERT INTO users VALUES(7);")
            before = db.execute("SELECT user_id FROM users").fetchall()
            before_columns = db.execute("PRAGMA table_info(users)").fetchall()
            migration = Path(__file__).parents[1] / "migrations/20261101_mobile_v2_legacy.sql"
            sql = migration.read_text(encoding="utf-8")
            db.executescript(sql)
            db.executescript(sql)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT user_id FROM users").fetchall(), before)
            self.assertEqual(db.execute("PRAGMA table_info(users)").fetchall(), before_columns)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mobile_v2_orders").fetchone()[0], 0)
            self.assertEqual(len(db.execute("PRAGMA table_info(mobile_v2_orders)").fetchall()), 13)
            db.close()


if __name__ == "__main__":
    unittest.main()
