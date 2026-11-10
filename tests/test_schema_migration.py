"""Migration helpers must skip only known duplicate-column cases."""
import unittest

import aiosqlite

from database import _ensure_schema_column


class TestSchemaMigration(unittest.IsolatedAsyncioTestCase):
    async def test_adds_missing_column(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
            await _ensure_schema_column(db, "sample", "enabled", "INTEGER NOT NULL DEFAULT 0")
            columns = await (await db.execute("PRAGMA table_info(sample)")).fetchall()
        self.assertIn("enabled", {row[1] for row in columns})

    async def test_existing_column_is_idempotent(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, enabled INTEGER)")
            await _ensure_schema_column(db, "sample", "enabled", "INTEGER NOT NULL DEFAULT 0")
            columns = await (await db.execute("PRAGMA table_info(sample)")).fetchall()
        self.assertEqual([row[1] for row in columns].count("enabled"), 1)

    async def test_invalid_migration_is_not_silenced(self):
        async with aiosqlite.connect(":memory:") as db:
            await db.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
            with self.assertRaises(aiosqlite.OperationalError):
                await _ensure_schema_column(db, "sample", "bad", "NOT A VALID SQL DEFINITION")


if __name__ == "__main__":
    unittest.main()
