# Mobile subscription entitlements

Authenticated subscription snapshots from `/api/v1/subscription/status`,
Telegram/email login, trial activation, and successful payment checks return an
ES256 compact JWT in `subscription.entitlement` for an active, trial, or grace
subscription. The signature uses JOSE format (`base64url(r) || base64url(s)`,
32 bytes each), compatible with Android `SHA256withECDSA` verification.

The signed claims are:

| Claim | Meaning |
| --- | --- |
| `iss` | `bymevpn-api` |
| `aud` | `bymevpn-mobile` |
| `sub` | Authenticated account ID, as a decimal string |
| `device_hash` | SHA-256 digest of a domain-separated account ID and the current HWID; raw HWID is not embedded |
| `status`, `plan_code`, `max_devices` | Current server-side subscription values |
| `iat`, `nbf`, `exp` | Issuance, not-before, and expiry; expiry is capped at the earlier of subscription expiry and 72 hours |
| `jti` | Random token identifier |

An entitlement is issued only when the request has an HWID and the 3x-ui
device list for the authenticated account contains that device fingerprint.
The status and payment-check endpoints refuse active subscription status with
HTTP 503 if the signing key is missing, unsafe, or invalid. Login snapshots
degrade to `unknown` (never unsigned active status), and trial activation checks
signer readiness before consuming the trial. The backend never falls back to an
unsigned entitlement. Non-active responses contain `entitlement: null`.

## Runtime key configuration

Set `MOBILE_ENTITLEMENT_ES256_PRIVATE_KEY_FILE` to an absolute path for a
PKCS#8 P-256 private key. The file must be a regular root-owned file with mode
`0400` or `0600`, mounted read-only into the API container. Keep the private
key only in a VPS root-only secret location or the deployment's secret store;
never commit it, bake it into an image, or print it. Key rotation requires
deploying the corresponding public key to the Android build and then restarting
the API process so its cached signer is replaced.

The Android build variable `SERVER_ES256_PUBLIC_KEY` must contain the PEM
public key derived from exactly this private key. The current Android verifier
checks JWT algorithm, ES256 signature, and `exp`; it does not currently enforce
`iss`, `aud`, `sub`, or `device_hash`, and it does not require a token when the
field is absent. Therefore this backend contract supplies signed claims and
binds issuance to the account/device, but client-side claim enforcement and
fail-closed behavior require a separate Android change before claiming full
offline user/device binding. The backend-only change must not enable Google
login or alter payment flags.

## Safe rollout

1. Create the production P-256 key directly in the VPS root-only secret store;
   do not copy it into Git or chat.
2. Derive the PEM public key and fingerprint on the trusted host. Set the
   Android repository variable `SERVER_ES256_PUBLIC_KEY` and ensure the Android
   workflow passes that variable to Gradle as an environment variable.
3. In staging, verify a registered device receives a signed token, an unknown
   device is refused, missing signer configuration returns 503, and Telegram
   billing/subscription flows remain unchanged.
4. Back up production configuration, make the key available read-only to the
   API, deploy the backend only after staging passes, and verify an authenticated
   status response without logging the token. Keep the prior backend release and
   configuration for rollback.

The key is intentionally not generated or configured by this PR. Until the
runtime key and matching Android public-key variable are configured, active
mobile subscription status will fail closed if this backend version is
deployed; existing Telegram bot operations do not depend on the signer.
