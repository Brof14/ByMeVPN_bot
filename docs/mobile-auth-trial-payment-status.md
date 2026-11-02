# Mobile sign-in, trial, and payment readiness

## Telegram sign-in

Telegram confirmation stores only a hash of the short-lived state. The API now
reads the confirmed state, prepares the JWT and subscription snapshot, then
atomically consumes the state immediately before returning a successful login.
A transient subscription, panel, or signer failure no longer burns the state
before an exchange can complete. `POST /api/v1/auth/telegram/cancel-session`
provides an explicit cancellation path; the older GET and POST check routes
remain available for compatibility. The access token is returned in the JSON
body, never in the Telegram deep link.

The current Android `ByMeApiClient.checkTelegramSession` still polls with GET
and places the short-lived `auth_...` state in the query string. That value is
not the issued bearer access token, but proxy access logs could still capture
it. The API's POST check endpoint is already available; the Android client
should move to a JSON body before query-based polling is retired.

For an opted-in client, an unavailable signer or unconfirmed device leaves the
account login successful but returns an `unknown` subscription snapshot without
VPN nodes or an entitlement. Opted-in `/servers` and `/vpn/session` requests
also require the configured signer and a matching device fingerprint from the
owner's 3x-ui record. Legacy clients keep the existing behavior.

## Three-day trial

Trial eligibility is server-owned. Active keys, a previously used trial, a
currently active panel client, or an unavailable eligibility check do not
produce a positive `trial_available` result. An opted-in client can use a
trial against an expired panel client only when its current HWID is already
registered there. Eligibility, signer, and device checks happen before the
one-time trial claim; panel errors do not consume the claim. The provisioned
period remains exactly three days.

There is no safe first-use path for an opted-in app user who has no existing
3x-ui client: this panel integration learns an HWID only after a VPN client
connects, while the current API requires panel confirmation before it will
issue signed access. The server therefore reports the trial unavailable for
that case and does not weaken the entitlement check. A panel-supported
pre-registration/bootstrap handshake, or an explicitly reviewed onboarding
protocol that preserves the same device proof, is required before automatic
trials can work for brand-new app users.

## YooKassa and Google

Mobile payment catalog and checkout remain closed unless
`MOBILE_V2_WEBHOOK_GUARD_ENABLED` is explicitly enabled. This repository
contains no evidence of a configured YooKassa test shop, and no provider calls
were made. Do not set the production guard until provider final-status lookup,
webhook validation, invoice ownership, idempotency, and panel provisioning
have all passed against a test shop and isolated 3x-ui client.

`POST /api/v1/auth/google` remains closed (503) until the backend verifies
Google signatures and claims with the approved audience and a nonce-bound
client flow. A client ID or a decodable JWT alone is insufficient.
