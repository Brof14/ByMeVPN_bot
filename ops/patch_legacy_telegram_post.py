"""Add the POST body alias to a legacy GET-only mobile API module.

This narrow transformer is intended for a verified copy of the old mounted
runtime. It leaves the existing GET handler and all surrounding code intact.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path


GET_ROUTE = '@api_router.get("/auth/telegram/check-session", response_model=TelegramCheckResponse)'
POST_ROUTE = '@api_router.post("/auth/telegram/check-session", response_model=TelegramCheckResponse)'
SECTION_MARKER = "\n# ---------------------------------------------------------------------------\n# 2. SECURE EMAIL"
POST_ADAPTER = '''

class TelegramCheckSessionBody(BaseModel):
    token: str


@api_router.post("/auth/telegram/check-session", response_model=TelegramCheckResponse)
async def check_telegram_session_body(body: TelegramCheckSessionBody, request: Request):
    """Accept Android's JSON token while preserving the legacy GET handler."""
    if not body.token or len(body.token) > 256:
        return TelegramCheckResponse(status="expired")
    return await check_telegram_session(body.token, request)
'''


def patch_source(source: str) -> str:
    if POST_ROUTE in source and "async def check_telegram_session_body" in source:
        return source
    if POST_ROUTE in source:
        raise ValueError("Unexpected existing Telegram POST route; inspect the runtime before patching")
    if GET_ROUTE not in source:
        raise ValueError("Expected legacy Telegram GET route was not found")
    if "from pydantic import BaseModel" not in source or "Request" not in source:
        raise ValueError("Expected BaseModel and Request imports were not found")
    if "class TelegramCheckResponse(BaseModel):" not in source:
        raise ValueError("Expected Telegram response model was not found")
    if SECTION_MARKER not in source:
        raise ValueError("Expected insertion point after Telegram route was not found")
    return source.replace(SECTION_MARKER, POST_ADAPTER + SECTION_MARKER, 1)


def patch_file(path: Path) -> bool:
    original = path.read_text(encoding="utf-8")
    updated = patch_source(original)
    if updated == original:
        return False
    current_mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, current_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) == 2 else None
    if target is None:
        raise SystemExit("usage: patch_legacy_telegram_post.py /path/to/api_router.py")
    try:
        changed = patch_file(target)
    except Exception as exc:
        raise SystemExit(f"patch refused: {type(exc).__name__}") from None
    print("patched" if changed else "already patched")
