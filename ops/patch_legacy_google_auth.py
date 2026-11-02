"""Create an additive overlay copy of the legacy API router for Google auth.

The source is never edited in place. The output can be mounted read-only at
the same API path and rolled back by removing that overlay mount.
"""
from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "# BYMEVPN_LEGACY_GOOGLE_AUTH_OVERLAY_V1"
HOOK = f"""\n\n{MARKER}\nfrom legacy_google_auth import install_legacy_google_auth\ninstall_legacy_google_auth(api_router, globals())\n"""


def patched_text(source: str) -> str:
    if MARKER in source:
        return source
    if "api_router = APIRouter()" not in source:
        raise ValueError("Source is not the expected legacy API router")
    if '@api_router.post("/auth/google")' not in source:
        raise ValueError("Legacy Google stub was not found")
    return source.rstrip() + HOOK


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    original = args.source.read_text(encoding="utf-8")
    rendered = patched_text(original)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError("Refusing to overwrite an existing overlay")
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
