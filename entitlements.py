"""ES256 signed, short-lived mobile subscription entitlements.

The signing key is read only from a root-owned, owner-only file supplied by the
runtime. This module never provides an unsigned fallback.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)


ENTITLEMENT_ISSUER = "bymevpn-api"
ENTITLEMENT_AUDIENCE = "bymevpn-mobile"
ENTITLEMENT_MAX_TTL_SECONDS = 72 * 60 * 60
PRIVATE_KEY_FILE_ENV = "MOBILE_ENTITLEMENT_ES256_PRIVATE_KEY_FILE"
_HWID_RE = re.compile(r"^[^\x00-\x20\x7f]{8,256}$")


class EntitlementSigningError(RuntimeError):
    """The signer is unavailable or the requested entitlement is invalid."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


@lru_cache(maxsize=8)
def _load_private_key_cached(path_text: str, mtime_ns: int, size: int):
    del mtime_ns, size  # These values make cache invalidation follow file rotation.
    path = Path(path_text)
    try:
        metadata = path.lstat()
        _validate_key_file_metadata(metadata)
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except EntitlementSigningError:
        raise
    except Exception as exc:
        raise EntitlementSigningError("Entitlement signing key could not be loaded") from exc

    if (not isinstance(key, ec.EllipticCurvePrivateKey)
            or not isinstance(key.curve, ec.SECP256R1)):
        raise EntitlementSigningError("Entitlement signing key must use P-256")
    return key


def _validate_key_file_metadata(metadata: os.stat_result) -> None:
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
            or metadata.st_mode & 0o077):
        raise EntitlementSigningError(
            "Entitlement key file must be a root-owned regular file with mode 0400 or 0600"
        )


def load_private_key():
    path_text = os.getenv(PRIVATE_KEY_FILE_ENV, "").strip()
    if not path_text:
        raise EntitlementSigningError(f"{PRIVATE_KEY_FILE_ENV} is not configured")
    path = Path(path_text)
    if not path.is_absolute():
        raise EntitlementSigningError(f"{PRIVATE_KEY_FILE_ENV} must be an absolute path")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EntitlementSigningError("Entitlement signing key file is unavailable") from exc
    _validate_key_file_metadata(metadata)
    return _load_private_key_cached(str(path), metadata.st_mtime_ns, metadata.st_size)


def device_binding(user_id: int, hwid: str) -> str:
    """Return a pseudonymous, account-bound digest without embedding the HWID."""
    if type(user_id) is not int or user_id <= 0 or not isinstance(hwid, str):
        raise EntitlementSigningError("A valid user and device identifier are required")
    normalized_hwid = hwid.strip()
    if not _HWID_RE.fullmatch(normalized_hwid):
        raise EntitlementSigningError("A valid device identifier is required")
    material = f"bymevpn-entitlement-v1:{user_id}:{normalized_hwid}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _jwt_from_claims(claims: Mapping[str, Any], private_key: ec.EllipticCurvePrivateKey) -> str:
    header = {"alg": "ES256", "typ": "JWT"}
    head = _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    body = _b64url(json.dumps(dict(claims), separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{head}.{body}".encode("ascii")
    der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    if r.bit_length() > 256 or s.bit_length() > 256:
        raise EntitlementSigningError("ES256 signature has an invalid size")
    raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{head}.{body}.{_b64url(raw_signature)}"


def create_entitlement(
    user_id: int,
    hwid: str,
    subscription: Mapping[str, Any],
    *,
    now: int | None = None,
    private_key: ec.EllipticCurvePrivateKey | None = None,
) -> str:
    """Sign a current account/device subscription state for at most 72 hours."""
    current_time = int(time.time()) if now is None else int(now)
    status = subscription.get("status")
    remaining = subscription.get("seconds_remaining")
    if status not in {"active", "trial", "grace"}:
        raise EntitlementSigningError("Subscription is not eligible for an entitlement")
    if type(remaining) is not int or remaining <= 0:
        raise EntitlementSigningError("Subscription expiry is unavailable")
    try:
        max_devices = int(subscription.get("max_devices"))
    except (TypeError, ValueError) as exc:
        raise EntitlementSigningError("Subscription device limit is unavailable") from exc
    if max_devices not in (2, 5, 10):
        raise EntitlementSigningError("Subscription device limit is unsupported")

    expiry = min(current_time + remaining, current_time + ENTITLEMENT_MAX_TTL_SECONDS)
    if expiry <= current_time:
        raise EntitlementSigningError("Subscription is expired")
    claims: dict[str, Any] = {
        "iss": ENTITLEMENT_ISSUER,
        "aud": ENTITLEMENT_AUDIENCE,
        "sub": str(user_id),
        "device_hash": device_binding(user_id, hwid),
        "status": status,
        "plan_code": subscription.get("plan_code"),
        "max_devices": max_devices,
        "iat": current_time,
        "nbf": current_time,
        "exp": expiry,
        "jti": secrets.token_urlsafe(18),
    }
    return _jwt_from_claims(claims, private_key or load_private_key())


def verify_entitlement(
    token: str,
    public_key: ec.EllipticCurvePublicKey,
    *,
    expected_user_id: int,
    expected_hwid: str,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Strict contract verifier used by tests and backend-side diagnostics."""
    current_time = int(time.time()) if now is None else int(now)
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
        if header != {"alg": "ES256", "typ": "JWT"} or not isinstance(claims, dict):
            return None
        raw_signature = _b64url_decode(parts[2])
        if len(raw_signature) != 64:
            return None
        der = encode_dss_signature(
            int.from_bytes(raw_signature[:32], "big"),
            int.from_bytes(raw_signature[32:], "big"),
        )
        public_key.verify(
            der, f"{parts[0]}.{parts[1]}".encode("ascii"), ec.ECDSA(hashes.SHA256())
        )
        expiry, issued, not_before = claims.get("exp"), claims.get("iat"), claims.get("nbf")
        if (claims.get("iss") != ENTITLEMENT_ISSUER
                or claims.get("aud") != ENTITLEMENT_AUDIENCE
                or claims.get("sub") != str(expected_user_id)
                or claims.get("device_hash") != device_binding(expected_user_id, expected_hwid)
                or type(expiry) is not int or type(issued) is not int or type(not_before) is not int
                or expiry <= current_time or not_before > current_time or issued > current_time + 60
                or expiry <= issued or expiry - issued > ENTITLEMENT_MAX_TTL_SECONDS
                or claims.get("status") not in {"active", "trial", "grace"}
                or claims.get("max_devices") not in (2, 5, 10)):
            return None
        return claims
    except Exception:
        return None
