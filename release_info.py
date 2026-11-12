"""Report only verified deployment metadata in the admin system page."""
import os
import re


def release_identity() -> dict[str, str]:
    sha = os.getenv("BYMEVPN_RELEASE_SHA", "").strip()
    image_digest = os.getenv("BYMEVPN_IMAGE_DIGEST", "").strip()
    built_at = os.getenv("BYMEVPN_BUILD_TIMESTAMP", "").strip()
    return {
        "sha": sha if re.fullmatch(r"[a-fA-F0-9]{7,40}", sha) else "не задан",
        "built_at": built_at[:64] if re.fullmatch(r"[A-Za-z0-9T:.+\-Z ]{10,64}", built_at) else "не задана",
        "image_digest": image_digest[:80] if re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) else "не задан",
    }
