"""Online SQLite backup regression: WAL transactions must survive archive/restore."""
import io
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from encrypted_backup import decrypt_backup
from notifications import _backup_database, _verified_sqlite_backup_zip


class SqliteOnlineBackupTests(unittest.IsolatedAsyncioTestCase):
    def test_wal_snapshot_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "live.sqlite3"
            with sqlite3.connect(source) as live:
                self.assertEqual(live.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                live.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, value TEXT)")
                live.execute("INSERT INTO payments(value) VALUES('committed-wal')")
                live.commit()
                self.assertTrue(Path(str(source) + "-wal").exists())

                archive_bytes = _verified_sqlite_backup_zip(source)
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                    self.assertEqual(archive.namelist(), ["live.sqlite3"])
                    backup_path = Path(tmp) / "restored.sqlite3"
                    backup_path.write_bytes(archive.read("live.sqlite3"))
                with sqlite3.connect(backup_path) as restored:
                    self.assertEqual(
                        restored.execute("SELECT value FROM payments").fetchone()[0],
                        "committed-wal",
                    )
                    self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def test_corrupted_source_is_never_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad.sqlite3"
            source.write_bytes(b"this is not a SQLite database")
            with self.assertRaises((sqlite3.DatabaseError, RuntimeError)):
                _verified_sqlite_backup_zip(source)

    async def test_daily_backup_delivers_verified_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "online.db"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE events (name TEXT)")
                db.execute("INSERT INTO events VALUES ('safe')")
                db.commit()
            bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
            password = "correct-test-backup-passphrase-value-12345"
            with patch("notifications.DB_FILE", str(source)), \
                 patch("notifications.ADMIN_ID", 11), \
                 patch.dict("os.environ", {"BYMEVPN_BACKUP_PASSPHRASE": password}):
                await _backup_database(bot)
            bot.send_document.assert_awaited_once()
            self.assertEqual(bot.send_document.await_args.args[0], 11)
            sent_file = bot.send_document.await_args.args[1]
            self.assertTrue(sent_file.filename.endswith(".bmbackup"))
            decrypted = decrypt_backup(sent_file.data, password)
            with zipfile.ZipFile(io.BytesIO(decrypted)) as archive:
                self.assertIn("online.db", archive.namelist())
            bot.send_message.assert_not_awaited()

    async def test_missing_backup_secret_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "online.db"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE events (name TEXT)")
                db.commit()
            bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
            with patch("notifications.DB_FILE", str(source)), \
                 patch("notifications.ADMIN_ID", 11), \
                 patch.dict("os.environ", {"BYMEVPN_BACKUP_PASSPHRASE": ""}):
                await _backup_database(bot)
            bot.send_document.assert_not_awaited()
            bot.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
