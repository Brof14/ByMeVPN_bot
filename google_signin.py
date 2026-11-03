"""Strict Google OpenID Connect ID-token verification for the mobile API."""

from __future__ import annotations

import time
from typing import Any, Mapping


class GoogleIdTokenError(ValueError):
    """An ID token did not satisfy Google's signature or claim requirements."""

    def __init__(self, code: str):
        # Keep the public/loggable diagnostic to a fixed allowlist. Provider
        # exception messages can contain token or request data and are omitted.
        allowed = {
            "invalid_token_format", "audience_not_configured", "google_keys_unavailable",
            "verifier_unavailable", "invalid_signature", "invalid_signature_or_key",
            "wrong_audience",
            "provider_verification_failed", "wrong_issuer", "invalid_expiration",
            "expired_token", "email_unverified", "invalid_subject", "invalid_email",
            "invalid_nonce",
        }
        self.code = code if code in allowed else "provider_verification_failed"
        super().__init__(self.code)


def verify_google_id_token(token: str, expected_audience: str) -> dict[str, Any]:
    """Verify a Google ID token against Google's published signing keys.

    The verifier performs the cryptographic signature, issuer, audience and
    expiration checks. This wrapper additionally requires the one exact Web
    Client ID, a verified email, a stable subject, and the fields used by the
    one-time nonce flow. No claims are decoded before signature verification.
    """
    if not isinstance(token, str) or not token or len(token) > 16_384:
        raise GoogleIdTokenError("invalid_token_format")
    if not isinstance(expected_audience, str) or not expected_audience.strip():
        raise GoogleIdTokenError("audience_not_configured")

    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token

        class BoundedGoogleRequest(GoogleRequest):
            def __call__(self, url, method="GET", body=None, headers=None, timeout=5, **kwargs):
                return super().__call__(
                    url, method=method, body=body, headers=headers, timeout=5, **kwargs
                )

        # Google now publishes a JWK set. google-auth delegates this format to
        # PyJWT; pass the exact configured audience so PyJWT can validate it
        # during signature verification. The explicit check below keeps the
        # accepted contract narrow and gives a stable, safe failure code.
        claims: Mapping[str, Any] = id_token.verify_token(
            token,
            BoundedGoogleRequest(),
            audience=expected_audience,
            certs_url="https://www.googleapis.com/oauth2/v3/certs",
            clock_skew_in_seconds=0,
        )
    except ImportError:
        raise GoogleIdTokenError("verifier_unavailable") from None
    except Exception as exc:
        # Classify only known exception types. Never copy provider text into
        # logs, the HTTP response, or exception chains.
        error_name = type(exc).__name__
        safe_codes = {
            "InvalidSignatureError": "invalid_signature",
            "InvalidAudienceError": "wrong_audience",
            "InvalidIssuerError": "wrong_issuer",
            "ExpiredSignatureError": "expired_token",
            # google.auth.jwt reports malformed/expired temporal claims as
            # InvalidValue after the signature has been checked.
            "InvalidValue": "invalid_expiration",
            "MalformedError": "invalid_signature_or_key",
            "TransportError": "google_keys_unavailable",
            "TimeoutError": "google_keys_unavailable",
            "ConnectionError": "google_keys_unavailable",
        }
        raise GoogleIdTokenError(
            safe_codes.get(error_name, "provider_verification_failed")
        ) from None

    issuer = claims.get("iss")
    if issuer not in {"https://accounts.google.com", "accounts.google.com"}:
        raise GoogleIdTokenError("wrong_issuer")
    if claims.get("aud") != expected_audience:
        raise GoogleIdTokenError("wrong_audience")

    try:
        expires_at = int(claims.get("exp"))
    except (TypeError, ValueError):
        raise GoogleIdTokenError("invalid_expiration") from None
    if expires_at <= int(time.time()):
        raise GoogleIdTokenError("expired_token")

    verified = claims.get("email_verified")
    if not (verified is True or (isinstance(verified, str) and verified.casefold() == "true")):
        raise GoogleIdTokenError("email_unverified")

    subject = claims.get("sub")
    email = claims.get("email")
    nonce = claims.get("nonce")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise GoogleIdTokenError("invalid_subject")
    if not isinstance(email, str) or not email or len(email) > 320 or "@" not in email:
        raise GoogleIdTokenError("invalid_email")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
        raise GoogleIdTokenError("invalid_nonce")

    return {
        "sub": subject,
        "email": email,
        "name": str(claims.get("name") or "Google user")[:160],
        "nonce": nonce,
        "exp": expires_at,
        "iss": issuer,
        "aud": expected_audience,
    }
