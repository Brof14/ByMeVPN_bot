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

Android PR #25 polls with `POST /api/v1/auth/telegram/check-session` and JSON
`{"token":"auth_..."}`. The backend branch for this PR implements that
contract through an additive adapter which delegates to the existing GET
handler. HTTP tests verify the method, path, JSON field, state lookup, and
response. The GET route remains for older clients. A public
production probe on 2026-11-02 returned `405 Method Not Allowed` with
`Allow: GET` for POST, while the legacy GET probe returned `200`; therefore the
currently observed production runtime does not yet support the Android PR #25
contract and must not be described as deployed-compatible.

For a GET-only runtime with the same handler/response names, the isolated patch
is: add `telegram_post_compat.py`, import
`register_telegram_check_post`, then call
`register_telegram_check_post(api_router, check_telegram_session,
TelegramCheckResponse)` immediately after the existing GET handler. This adds
only POST and leaves GET behavior untouched. Rollback is to remove the helper,
its import, and that registration call; there is no database migration. The
server's observed ED25519 host-key fingerprint matched the previously trusted
key for the old address when checked with strict verification, but SSH
authentication was denied (`Permission denied (publickey,password)`). The
actual VPS source therefore could not be read; this is a tested additive patch
for the observed GET-only contract, not a verified diff against the mounted
production file. It has not been applied.

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
3x-ui client: the current panel integration learns an HWID only after a VPN
client connects, and its API adapter can list or delete HWIDs but has no
pre-registration operation. The API therefore reports the trial unavailable
for that case and refuses activation before claiming the one-time trial or
provisioning a client. Client-supplied `X-HWID` alone is not panel proof.

The safe bootstrap contract is deliberately conditional on panel support:
authenticate the account; bind the requested HWID to that account's own panel
client using a panel-native registration operation; read the panel record back
and verify its owner and HWID fingerprint; then atomically reserve the trial,
provision a client with `limitHwid=2`, `limitIp=0` and a hard expiry exactly
259,200 seconds from issuance, and return the signed entitlement only after
the provisioned client and device record are confirmed. Repeated requests must
return the same expiry and never extend it. A panel refusal or failed readback
must issue no entitlement/configuration and must not consume the trial. An
ambiguous provisioning outcome must be reconciled against the panel before any
retry; it must never blindly extend the expiry. The current 3x-ui integration
does not expose the required native registration call, so this PR keeps the
flow closed and tests the fail-closed behavior until an isolated panel confirms
the API and its owner/limit semantics.

Tests cover a successful first trial for an already panel-confirmed device
(exactly three days), a repeated request (409, no reprovision/extension), an
unconfirmed HWID (403, no claim/provision), and panel read failure (503, no
claim/provision). These are mocked panel checks, not end-to-end panel proof.

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
