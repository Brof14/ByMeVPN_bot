"""Memory-hard hashing for new accounts, legacy PBKDF2 compatibility."""
import secrets
import unittest

import api_router
from fastapi import HTTPException


class EmailPasswordHashingTests(unittest.TestCase):
    def test_scrypt_hash_is_salted_and_verifiable(self):
        salt1, salt2 = secrets.token_hex(16), secrets.token_hex(16)
        a = api_router.hash_password_scrypt("sample-long-password-123", salt1)
        b = api_router.hash_password_scrypt("sample-long-password-123", salt2)
        self.assertTrue(a.startswith("scrypt$v1$"))
        self.assertNotEqual(a, b)
        self.assertTrue(api_router.verify_stored_password("sample-long-password-123", a, salt1))
        self.assertFalse(api_router.verify_stored_password("not-this-password", a, salt1))

    def test_legacy_pbkdf2_credentials_remain_usable(self):
        salt = secrets.token_hex(16)
        legacy = api_router.hash_password("legacy-password", salt)
        self.assertTrue(api_router.verify_stored_password("legacy-password", legacy, salt))
        self.assertFalse(api_router.verify_stored_password("incorrect", legacy, salt))

    def test_new_signup_rejects_invalid_email_and_short_password(self):
        for email in ("not-email", "user@@host.tld", "name..surname@example.com"):
            with self.assertRaises(HTTPException):
                api_router.validate_registration_credentials(email, "strong-password")
        with self.assertRaises(HTTPException):
            api_router.validate_registration_credentials("valid@example.com", "123456")
        api_router.validate_registration_credentials("valid@example.com", "sufficiently-long-password")


if __name__ == "__main__":
    unittest.main()
