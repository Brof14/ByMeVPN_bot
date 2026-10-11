# ByMeVPN Bot — pre-deployment checklist

This checklist is intentionally separate from a deployment script. It does not change the VPS or restart any service.

## Before touching production

- [ ] Confirm the exact target VPS and the intended 3x-ui panel. Do not infer live settings from `.env.example` or `PRODUCTION_AUDIT.md`.
- [ ] Record the current Git commit and keep a tested rollback revision.
- [ ] Stop and back up the bot database using a SQLite-consistent backup method. Preserve the original file, WAL/SHM files if the process is live, and file permissions.
- [ ] Back up `.env` outside Git with restrictive permissions. Never paste credentials into issues, chat, or logs.
- [ ] Back up the 3x-ui database using the database engine actually used by that installation. Do not run a PostgreSQL-only command against a SQLite panel, or vice versa.
- [ ] Compare `XUI_INBOUND_IDS` with the real panel. Include only inbound IDs that exist and that should appear in customer subscriptions. Do not copy example IDs.
- [ ] Verify `XUI_URL` is the public subscription base URL and `XUI_API_URL` is the private API URL used by the bot.
- [ ] Verify TLS validation is enabled unless the panel deliberately uses a private/self-signed certificate and that exception is understood.
- [ ] Confirm that the webhook endpoint is reachable through the intended HTTPS path and that firewall rules do not expose unrelated services.

## Repository checks

Run in a clean checkout of the exact candidate commit:

```bash
python -m pip install -r requirements.txt
python -m compileall -q .
python -m unittest discover -s tests -v
```

Do not deploy unless all checks pass on the exact candidate commit.

## Staged production verification

1. Deploy only after reviewing the change set and the live configuration.
2. Confirm startup logs report that every configured 3x-ui inbound responds. If startup refuses to continue, fix the configuration/connectivity issue; do not bypass the check just to make the process run.
3. Confirm the bot is polling Telegram and the YooKassa callback is routed to `/webhook/yookassa`. Telegram updates are not accepted over HTTP.
4. Make a low-risk test purchase only when ready to test the real payment path. Verify one payment ledger row, one expected expiry change, the correct device limit, and a working subscription on every intended server.
5. Replay the same provider notification and confirm it does not extend access a second time.
6. Verify the payment-monitor recovery path and admin alerts for an intentionally simulated provisioning failure in a non-production environment first.
7. Check logs for leaked UUIDs, subscription URLs, payment-method IDs, or credentials.
8. Keep the prior code and database backups until the new version has operated normally through payment, renewal, and restart scenarios.

## Rollback conditions

Rollback or disable checkout if any of the following occurs:

- a payment is captured but access cannot be verified;
- a customer UUID or subscription ID changes unexpectedly;
- one or more intended inbounds are missing from the issued subscription;
- payment state becomes ambiguous or duplicate extensions are observed;
- database migration or restart behavior differs from the reviewed plan.

A passing CI run does not verify the live VPS, live payment credentials, panel certificate, firewall, or real client connectivity. Those require a controlled production check.
