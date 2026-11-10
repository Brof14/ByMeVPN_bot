# Telegram webhook authentication

`/webhook/telegram` is disabled with HTTP 503 until `TELEGRAM_WEBHOOK_SECRET`
is set. When enabled, every request must contain Telegram's
`X-Telegram-Bot-Api-Secret-Token` header with the configured secret. Invalid
secrets receive HTTP 401; malformed and oversized updates are rejected before
dispatch.

For a webhook deployment, generate a unique secret that meets Telegram's
`secret_token` format requirements, set it in the bot's runtime environment,
and configure Telegram's `setWebhook` with the same value. Keep it out of source
control, URLs, logs, and support messages. If the bot uses long polling, leave
the webhook secret unset and do not configure a Telegram webhook.
