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
`{"token":"auth_..."}`. The backend preserves the legacy GET route and adds
the POST form through an additive adapter. HTTP tests cover the request
contract. A production request with a synthetic nonexistent token returned
HTTP 200 and the expected `expired` state on 2026-11-02; no real session was
used.

For an opted-in client, an unavailable signer or unconfirmed device leaves the
account login successful but returns an `unknown` subscription snapshot without
VPN nodes or an entitlement. Opted-in `/servers` and `/vpn/session` requests
also require the configured signer and a matching device fingerprint from the
owner's 3x-ui record. Legacy clients keep the existing behavior.

## Three-day trial

Trial eligibility is server-owned and shared with Telegram through the
atomic `users.trial_used` claim. Active keys, a previously used trial, a
currently active panel client, or an unavailable eligibility check do not
produce a positive `trial_available` result. The provisioned period remains
exactly three days.

The legacy panel cannot pre-register an HWID. For an opted-in client with no
panel device yet, the runtime uses a bounded two-step bootstrap: it atomically
claims the shared `users.trial_used` flag, provisions the owner's panel client
for three days with `limitHwid=2` and `limitIp=0`, reads the client and limits
back, and requests the subscription once with the install HWID. A matching
panel fingerprint gets a normal entitlement immediately. If the panel learns
the HWID only after the first VPN connection, the API issues a user-and-device
bound ES256 bootstrap entitlement lasting at most ten minutes. `/servers` and
`/vpn/session` accept only that same bound device during this window. Once
3x-ui reports its fingerprint, normal entitlement replaces the bootstrap and
the grant is removed. A client-supplied header alone never proves registration.

Repeated requests return the existing trial and cannot extend the panel expiry
or bootstrap window. Panel readback must confirm ownership, expiry and exact
2/0 limits before any entitlement is issued. Errors fail closed; an ambiguous
provisioning result keeps the shared claim reserved rather than risking a
second trial. Paid clients with 5/10 devices remain accepted by the shared
device gate. These behaviors have isolated HTTP/unit coverage; the first
real-device handshake and subsequent panel fingerprint refresh still require
a release app/test account and are not established by mocks.

## YooKassa and Google

The deployed mobile checkout guard is enabled. The runtime uses server-side
plan prices, owner-scoped invoices, provider final-status lookup and guarded,
idempotent webhook fulfillment. The merchant callback was confirmed by the
owner. No invoice or payment was created during this work, so a successful
provider transaction was not exercised end-to-end.

Google nonce and signature verification are deployed for the owner-provided
Web Client ID. The server checks Google signature, issuer, audience, expiry,
verified email and one-time nonce, and links accounts only by `sub`. The
public nonce route and rejection of a fake token were checked; a successful
release-app login still requires the Android nonce-flow change to ship and be
tested on a device.
