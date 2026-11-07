# Online SQLite backup fix — 2026-11-07

## Finding

The former 03:00 scheduler used `ZipFile.write(Path(DB_FILE))` while SQLite
remained live. In WAL mode, committed transactions can reside in the
`-wal` file. Backing up only the main `.db` can silently omit recent
payments, keys, users and referral events.

## Change

- Use the official SQLite online backup API against a read-only source
  connection, keeping all committed WAL transactions.
- Verify `PRAGMA integrity_check = ok` on the snapshot before archiving.
- Run backup and compression via `asyncio.to_thread` to avoid blocking
  Telegram handlers.
- Keep a private temporary snapshot only for archive construction; do not
  leave ZIPs or snapshots in the bot working directory.
- Report backup errors without echoing internal exception details in Telegram.
- Test restore from a database with committed WAL transactions; test corrupt
  source rejection and the administrator delivery envelope.

## Remaining requirements

A tested archive is **not** a complete disaster-recovery plan:
restore drills on a staging VPS, encryption of off-site backups, retention,
deletion access controls, restoration of associated 3x-ui state and RPO/RTO
objectives remain operator responsibilities. No production snapshot or
restoration was performed in this PR. Tariffs/payments were not modified.
