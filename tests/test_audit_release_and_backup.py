"""Read-only identity report and temporary SQLite backup recovery."""
import os
import sqlite3
import tempfile
import unittest

from scripts.audit_email_identity_ids import inspect_identity_db
from scripts.verify_sqlite_restore import rehearse_sqlite_restore
from release_info import release_identity
from unittest.mock import patch
import hashlib


class IdentityAuditAndRecoveryTests(unittest.TestCase):
    def test_legacy_hash_id_is_reported_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sample.db")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE app_users(user_id INTEGER PRIMARY KEY,email TEXT)")
            legacy_email = "legacy@example.invalid"
            legacy_id = int(hashlib.md5(legacy_email.encode()).hexdigest()[:8], 16)
            db.execute("INSERT INTO app_users(user_id,email) VALUES(?,?)", (legacy_id,legacy_email))
            db.commit()
            db.close()
            report = inspect_identity_db(path)
            self.assertEqual(report["legacy_email_hash_candidates"], 1)
            self.assertEqual(report["app_identities"], 1)
            # The rehearsal never mutates source.
            self.assertEqual(rehearse_sqlite_restore(path), {})

    def test_release_identity_never_claims_missing_sha(self):
        with patch.dict(os.environ, {
            "BYMEVPN_RELEASE_SHA": "", "BYMEVPN_BUILD_TIMESTAMP": "",
            "BYMEVPN_IMAGE_DIGEST": "",
        }):
            result = release_identity()
        self.assertEqual(result["sha"], "не задан")
        self.assertEqual(result["image_digest"], "не задан")


if __name__ == "__main__":
    unittest.main()
