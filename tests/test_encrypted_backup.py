"""Encrypted database exports must fail closed on missing or wrong keys."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from cryptography.exceptions import InvalidTag
from encrypted_backup import (
    MAGIC, encrypt_backup, decrypt_backup, require_backup_passphrase,
)


class TestEncryptedBackup(unittest.TestCase):
    def test_encrypt_round_trip_and_wrong_passphrase(self):
        secret = "x" * 36
        raw = b"sqlite snapshot with secret subscription tokens"
        payload = encrypt_backup(raw, secret)
        self.assertTrue(payload.startswith(MAGIC))
        self.assertNotIn(b"secret subscription", payload)
        self.assertEqual(decrypt_backup(payload, secret), raw)
        with self.assertRaises(InvalidTag):
            decrypt_backup(payload, "y" * 36)

    def test_modified_payload_is_rejected(self):
        encrypted = bytearray(encrypt_backup(b"payload", "x" * 36))
        encrypted[-1] ^= 1
        with self.assertRaises(InvalidTag):
            decrypt_backup(bytes(encrypted), "x" * 36)

    def test_missing_env_rejects_unencrypted_telegram_delivery(self):
        with patch.dict(os.environ, {"BYMEVPN_BACKUP_PASSPHRASE": ""}):
            with self.assertRaises(RuntimeError):
                require_backup_passphrase()


if __name__ == "__main__":
    unittest.main()
