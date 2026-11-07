# ByMeVPN app handover: HTTPS security gate (2026-11-07)

## Finding

The bot sends `/app/login?sub=<subscription bearer URL>` to the customer's
Telegram client. The default public handover origin was plain HTTP on port
8080. Anyone able to intercept a network request could extract and reuse the
VPN subscription credential. URL query values also appear in browser history
and may be retained in reverse-proxy access logs even over HTTPS.

## Change

- Default the public app handover origin to the existing public webhook's
  scheme + authority (which is configured for HTTPS), not raw :8080.
- Refuse to create credential-bearing app links when the configured handover
  origin is not HTTPS or contains URL credentials/query/fragment.
- If a deployment explicitly configures an insecure HTTP origin, keep
  app sign-in usable through manual copy and connection guide **without**
  emitting an HTTP redirect carrying the VPN secret.
- Update `.env.example` and add regression tests for both modes.

## Release and migration requirements

- Check that the TLS reverse proxy routes `/app/login` and `/app/logo`
  at the public HTTPS webhook origin; a successful TCP handshake is not a
  successful app-opening test.
- Ensure reverse-proxy and application request logs redact query
  parameters on `/app/login`.
- Complete a proper short-lived one-time handover token exchange to
  eliminate long-lived bearer subscription URLs from browser history.
- Test the actual ByMeVPN Android app's deep-link handler on device.
- Do not force-update existing VPS `.env` blindly; the new default only
  applies where APP_PUBLIC_URL is unset.

No tariffs, paid durations, referrer bonuses or payment contracts changed.
