"""Authenticated encryption for Telegram-transmitted SQLite backup archives.

The plaintext ZIP exists only in memory and its temporary SQLite snapshot is
deleted before transmission. Keep the passphrase OFF the same chat and server
backup destination, and separately maintain encrypted off-site backup copies.
"""
import hashlib
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"BYMEVPN-BACKUP-V1\x00"
SALT_LEN = 16
NONCE_LEN = 12
AAD = b"ByMeVPN SQLite backup v1"


def require_backup_passphrase() -> str:
    secret = os.environ.get("BYMEVPN_BACKUP_PASSPHRASE", "")
    if len(secret) < 32:
        raise RuntimeError("BYMEVPN_BACKUP_PASSPHRASE must be configured as a strong secret")
    return secret


def _key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 32:
        raise ValueError("backup passphrase is too short")
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32,
    )


def encrypt_backup(data: bytes, passphrase: str) -> bytes:
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    return MAGIC + salt + nonce + AESGCM(_key(passphrase, salt)).encrypt(
        nonce, data, AAD
    )


def decrypt_backup(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("Unsupported or unencrypted backup format")
    offset = len(MAGIC)
    if len(blob) <= offset + SALT_LEN + NONCE_LEN + 16:
        raise ValueError("Incomplete encrypted backup")
    salt = blob[offset: offset + SALT_LEN]
    nonce = blob[offset + SALT_LEN: offset + SALT_LEN + NONCE_LEN]
    ciphertext = blob[offset + SALT_LEN + NONCE_LEN:]
    return AESGCM(_key(passphrase, salt)).decrypt(nonce, ciphertext, AAD)
