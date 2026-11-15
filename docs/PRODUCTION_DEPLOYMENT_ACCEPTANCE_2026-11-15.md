# ByMeVPN — production deployment acceptance and rollback

This is an operator runbook, **not** a claim that production was deployed or
live payments were tested. Applies to PR #111 and later revisions of
`Brof14/ByMeVPN_bot`.

## 1. Inventory and source freeze

1. Record all currently running containers, compose project names, immutable
   image digests, the exact deployed Git commit, active polling modes and
   `/readyz` results. The repository's default `docker-compose.yml` defines
   `vpnbot`; the secondary container may use a separate production Compose
   override. Confirm how **both** are actually deployed before changing either.
2. Confirm that `.env`, signing keys, external TLS keys, persistent SQLite DB,
   Redis data and 3x-ui are located on persistent mounts, **not** overlaid by
   repository updates or copied into a new image.
3. Use the exact merged commit (not an unverified branch tip). Ensure Bot
   quality, Python tests, signer contract and Chromium UX checks succeeded for
   that commit. All previous GitHub commits are build-time evidence only.

## 2. Durable SQLite backup and rehearsal (required)

Use the checked-out source only after ensuring that no bot process can run its
startup migrations until the dry run is complete. Identify the real persistent
DB path from the **production container's configuration**, not from a guess.

```bash
python scripts/predeploy_db_guard.py \
  --db /absolute/path/to/prod.sqlite \
  --backup-dir /secure/bymevpn-backups

python scripts/audit_duplicate_subscriptions.py \
  --db /secure/bymevpn-backups/<actual-file-from-output>.sqlite
```

- The backup uses SQLite online backup (including WAL state) and stores a
  mode-0600 file; the script runs new migrations only on a throwaway clone and
  validates the single-active-key triggers. It never migrates the live DB.
- Copy the backup **off the VPS** into an access-controlled location before
  replacing production containers.
- Review all preexisting accounts with more than one active key against the
  payment ledger, pending upgrades, saved recurring methods and 3x-ui.
  **Never delete/shorten paid keys or unlink a card without verifying source
  invoices and getting an explicit owner-approved reconciliation plan.**
- If schema rehearsal fails, abort. If there are unidentified duplicate-paid
  accounts, do not enable those users to make a second checkout until fixed.

## 3. Release with rollback checkpoint

1. Save old Docker image IDs before any rebuild. Keep those images available
   until acceptance completes. Do not use `docker compose down --volumes`.
2. Build the target image from the audited commit with a source-revision
   label. Use the real deployment's Compose files/overrides to recreate both
   bot instances, **one primary scheduler** and one secondary polling instance.
   Do not accidentally run two independent payment monitors.
3. Restart only services that should change. Persistent DB migrations run once
   through the bot's startup, with no credentials or subscription links printed
   to operator logs. Check SQLite `PRAGMA integrity_check` after startup.
4. Validate `/healthz`, `/readyz`, container health, 3x-ui availability,
   inbound IDs, Redis and both Telegram polling consumers.

## 4. Controlled acceptance (required before enabling new payments broadly)

- Open **current subscriber main menu** on Telegram iOS/Android/Desktop; first
  tap on “Подключить VPN” must open the correct signed HTTPS page with no extra
  Telegram message. Repeat with a panel-only imported entitlement and an
  expired key; never expose a secret URL in logs.
- Test link reissue invalidates the prior epoch and works for the replacement.
  Test manual third-party import and actual tunnel success on physical clients.
- Use a **controlled provider test account**: each Stars, YooKassa and CryptoBot
  device upgrade (2→5, 2→10, 5→10) must bind the invoice, preserve expiry,
  update panel HWID, verify DB limit and update an existing auto-renew amount
  only **after** successful fulfillment.
- Repeat provider callbacks, duplicate polling, network timeout and interrupted
  finalization. Neither panel access nor amount/expiry may be incremented twice.
  An ambiguous paid outcome must be tagged recovery_required and surfaced to
  operators, with customer instructed not to pay again.
- Check real device delete/reconnect, support reply/reaction fallback, cancelled
  invoice re-checkout, refunds and provider reversals.
- Examine the stored Chromium screenshot artifacts, then inspect **real**
  client screenshots with narrow screens, large text and keyboard navigation.

## 5. Safe rollback

If code, UI or VPN connectivity fails, restore the **previous immutable
application image** and Compose configuration while retaining persistent
data. Never blindly restore the old SQLite snapshot after new real payments
or upgrades have settled: doing so would erase receipts, renewals and days.
First reconcile post-release payments and pending orders, then plan a
transaction-safe data recovery if needed.

Release outcome is **not accepted** until a real tester records the results,
dates, exact deployed commit, payment references in a secure audit store and
a clearly named rollback owner. The bot's GitHub CI cannot verify VPN routing
or production payment settlement.
