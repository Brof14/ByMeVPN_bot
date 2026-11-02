"""Strict Google OpenID Connect ID-token verification for the mobile API."""

from __future__ import annotations

import time
from typing import Any, Mapping


class GoogleIdTokenError(ValueError):
    """An ID token did not satisfy Google's signature or claim requirements."""


def verify_google_id_token(token: str, expected_audience: str) -> dict[str, Any]:
    """Verify a Google ID token against Google's published signing keys.

    The verifier performs the cryptographic signature, issuer, audience and
    expiration checks. This wrapper additionally requires the one exact Web
    Client ID, a verified email, a stable subject, and the fields used by the
    one-time nonce flow. No claims are decoded before signature verification.
    """
    if not isinstance(token, str) or not token or len(token) > 16_384:
        raise GoogleIdTokenError("Invalid ID token")
    if not isinstance(expected_audience, str) or not expected_audience.strip():
        raise GoogleIdTokenError("Google audience is not configured")

    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token

        class BoundedGoogleRequest(GoogleRequest):
            def __call__(self, url, method="GET", body=None, headers=None, timeout=5, **kwargs):
                return super().__call__(
                    url, method=method, body=body, headers=headers, timeout=5, **kwargs
                )

        claims: Mapping[str, Any] = id_token.verify_oauth2_token(
            token,
            BoundedGoogleRequest(),
            audience=expected_audience,
            clock_skew_in_seconds=0,
        )
    except Exception as exc:
        # Provider/network/library details can contain request data; keep them
        # out of API errors and routine logs.
        raise GoogleIdTokenError("Google ID token verification failed") from None

    issuer = claims.get("iss")
    if issuer not in {"https://accounts.google.com", "accounts.google.com"}:
        raise GoogleIdTokenError("Invalid Google issuer")
    if claims.get("aud") != expected_audience:
        raise GoogleIdTokenError("Invalid Google audience")

    try:
        expires_at = int(claims.get("exp"))
    except (TypeError, ValueError):
        raise GoogleIdTokenError("Invalid token expiration") from None
    if expires_at <= int(time.time()):
        raise GoogleIdTokenError("Expired Google ID token")

    verified = claims.get("email_verified")
    if not (verified is True or (isinstance(verified, str) and verified.casefold() == "true")):
        raise GoogleIdTokenError("Google email is not verified")

    subject = claims.get("sub")
    email = claims.get("email")
    nonce = claims.get("nonce")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise GoogleIdTokenError("Invalid Google subject")
    if not isinstance(email, str) or not email or len(email) > 320 or "@" not in email:
        raise GoogleIdTokenError("Invalid Google email")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
        raise GoogleIdTokenError("Invalid Google nonce")

    return {
        "sub": subject,
        "email": email,
        "name": str(claims.get("name") or "Google user")[:160],
        "nonce": nonce,
        "exp": expires_at,
        "iss": issuer,
        "aud": expected_audience,
    }
