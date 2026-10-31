# Windows/API release hardening requirements

The Windows desktop PR `Brof14/ByMeVPN-app-windows#2` requires this API change.

## Mandatory deployment

1. Set a **random, persistent** `JWT_SECRET` of at least 32 characters on every backend worker. The previous public fallback key has been removed. All existing tokens signed with the old default become invalid; users must sign in again.
2. Make sure the same signed-in user sees the same server-side tariff catalogue in Windows and Android. The API now rejects client price, device or duration mismatches (HTTP 422) rather than charging attacker-controlled amounts.
3. Verify payment webhook reconciliation; a browser redirect alone must never activate an account. For existing clients, check that each `plan_code` uses the specified Android or Windows aliases and the server-advertised days.
4. Free trial is claimed atomically and only provisioned once. If provisioning fails, a retry is permitted; if account synchronization fails, the user must contact support.
5. Google login is unavailable until OIDC ID-token signature, issuer, audience, expiration and nonce are verified on the server.
6. Device enumeration, device revocation and account deletion are **not implemented**. They now return empty/unsupported responses instead of false success. Implement secure transactional workflows before enabling those UI controls.
7. Complete integration tests with real 3x-ui, Windows TUN, IPv4/IPv6/DNS routing, YooKassa webhook, and Telegram auth before public release.

## Supported checkout aliases

Windows: `month`, `quarter`, `half_year`, `year`.

Android: `plan_month1_Ndev`, `plan_month4_Ndev`, `plan_month8_Ndev`, `plan_month15_Ndev`, where N is 2, 5 or 10.

The canonical cost and duration for these plans is controlled by `constants.py` via `get_price_for_months`. Client-provided cost/duration is validated, never treated as authority.
