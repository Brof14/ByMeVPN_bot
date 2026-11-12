"""Static release gate blocks missing metadata and backup secrets."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.release_gate import verify_release_prerequisites


class ReleaseGateTests(unittest.TestCase):
    def test_release_requires_metadata_and_recoverable_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "staging.sqlite")
            c = sqlite3.connect(db)
            c.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY)")
            c.commit()
            c.close()
            sha = "a" * 40
            with patch.dict(os.environ, {
                "BYMEVPN_RELEASE_SHA": "",
                "BYMEVPN_BACKUP_PASSPHRASE": "",
            }):
                errors = verify_release_prerequisites(db, sha)
                self.assertTrue(any("SHA" in e for e in errors))
                self.assertTrue(any("passphrase" in e for e in errors))
            with patch.dict(os.environ, {
                "BYMEVPN_RELEASE_SHA": sha,
                "BYMEVPN_BACKUP_PASSPHRASE": "x" * 40,
            }):
                self.assertEqual(verify_release_prerequisites(db, sha), [])


if __name__ == "__main__":
    unittest.main()
