"""The signer accepts only its own narrowly scoped POSIX ACL."""
import os
import stat
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import entitlements


def acl(entries):
    return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)


class SignerServiceAclTests(unittest.TestCase):
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
