"""The signer accepts only its own narrowly scoped POSIX ACL."""
import os
import base64
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import entitlements
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


def acl(entries):
    return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)


class SignerServiceAclTests(unittest.TestCase):
    def test_service_owned_key_signs_and_verifies_and_group_access_fails(self):
        with tempfile.TemporaryDirectory(prefix='signer-contract-') as directory:
            path = Path(directory) / 'test-key.pem'
            private_key = ec.generate_private_key(ec.SECP256R1())
            path.write_bytes(private_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            path.chmod(0o400)
            with patch.dict(os.environ, {entitlements.PRIVATE_KEY_FILE_ENV: str(path)}):
                loaded = entitlements.load_private_key()
                header, body, signature = entitlements._jwt_from_claims({'test': True}, loaded).split('.')
                raw = base64.urlsafe_b64decode(signature + '==')
                der = encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big'))
                private_key.public_key().verify(der, (header + '.' + body).encode(), ec.ECDSA(hashes.SHA256()))
                path.chmod(0o440)
                with self.assertRaises(entitlements.EntitlementSigningError):
                    entitlements.load_private_key()

    def test_named_service_reader_and_strict_rejections(self):
        uid = 10001
        undef = 0xffffffff
        good = [(1, 4, undef), (2, 4, uid), (4, 0, undef), (16, 4, undef), (32, 0, undef)]
        metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o440, st_uid=0)
        with patch.object(entitlements.os, 'geteuid', return_value=uid):
            with patch.object(entitlements.os, 'getxattr', return_value=acl(good)):
                entitlements._validate_key_file_metadata(metadata, Path('/test'))
            variants = [
                [(1, 4, undef), (2, 4, 10002), (4, 0, undef), (16, 4, undef), (32, 0, undef)],
                [(1, 4, undef), (2, 4, uid), (4, 4, undef), (16, 4, undef), (32, 0, undef)],
                [(1, 4, undef), (2, 4, uid), (4, 0, undef), (16, 4, undef), (32, 4, undef)],
                good + [(8, 4, 10001)],
            ]
            for entries in variants:
                with self.subTest(entries=entries), patch.object(entitlements.os, 'getxattr', return_value=acl(entries)):
                    with self.assertRaises(entitlements.EntitlementSigningError):
                        entitlements._validate_key_file_metadata(metadata, Path('/test'))
            with patch.object(entitlements.os, 'getxattr', side_effect=OSError('unavailable')):
                with self.assertRaises(entitlements.EntitlementSigningError):
                    entitlements._validate_key_file_metadata(metadata, Path('/test'))
            for owner, mode, allowed in [(uid, 0o400, True), (uid, 0o600, True), (9999, 0o400, False), (uid, 0o444, False)]:
                fake = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=owner)
                if allowed:
                    entitlements._validate_key_file_metadata(fake, Path('/test'))
                else:
                    with self.assertRaises(entitlements.EntitlementSigningError):
                        entitlements._validate_key_file_metadata(fake, Path('/test'))
