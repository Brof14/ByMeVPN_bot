#!/usr/bin/env python3
"""Decrypt a ByMeVPN .bmbackup archive to an offline local ZIP.

Usage: BYMEVPN_BACKUP_PASSPHRASE=<secret> python scripts/decrypt_backup.py backup.bmbackup archive.zip
Do not run this on a public server. The output contains subscription secrets.
"""
import argparse
import os
from pathlib import Path

from encrypted_backup import decrypt_backup, require_backup_passphrase


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("encrypted_file")
    parser.add_argument("output_zip")
    args = parser.parse_args()
    dest = Path(args.output_zip)
    if dest.exists():
        raise FileExistsError("Refusing to overwrite existing archive")
    contents = decrypt_backup(Path(args.encrypted_file).read_bytes(), require_backup_passphrase())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(dest, flags, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(contents)
    print("Decrypted ZIP saved locally; treat it as sensitive customer data.")


if __name__ == "__main__":
    main()
