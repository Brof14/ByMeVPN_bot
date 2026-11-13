"""Public, read-only endpoints for direct Android update distribution."""
from __future__ import annotations

import json
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse


router = APIRouter()
ROOT = Path(os.getenv("BYMEVPN_UPDATE_ROOT", Path(__file__).parent / "static" / "app")).resolve()
PAGE = Path(__file__).parent / "static" / "app" / "index.html"
ASSETS = Path(__file__).parent / "static" / "app" / "assets"
ICON_ASSETS = frozenset({
    "android.png", "apple.png", "bymevpn-logo.png", "happ.png", "incy.png", "koala.png", "linux.png",
        "v2raytun.png", "v2rayng.png",
    "v2rayn.png", "windows.png",
})
ORIGIN = "https://bymevpn.duckdns.org"
APK_NAME = re.compile(r"^ByMeVPN-v(\d+\.\d+\.\d+)\.apk$")
_verified_digest_cache: dict[tuple[str, int, int], str] = {}


def _load_manifest() -> dict:
    path = ROOT / "update.json"
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 32_768:
            raise ValueError("invalid manifest size")
        value = json.loads(raw)
        version = str(value["version_name"])
        code = value["version_code"]
        url = str(value["download_url"])
        digest = str(value["sha256"])
        notes = str(value["release_notes"])
        published_at = str(value.get("published_at", ""))
        min_android_version = str(value.get("min_android_version", "7.0"))
        signing_certificate_sha256 = str(value.get("signing_certificate_sha256", "")).lower()
        manifest_size = value.get("apk_size_bytes")
        parsed = urlparse(url)
        filename = Path(parsed.path).name
        if (not isinstance(code, int) or isinstance(code, bool) or code <= 0
                or not APK_NAME.fullmatch(filename)
                or APK_NAME.fullmatch(filename).group(1) != version
                or parsed.scheme != "https" or parsed.netloc != "bymevpn.duckdns.org"
                or parsed.path != f"/app/downloads/{filename}"
                or parsed.query or parsed.fragment
                or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
                or (published_at and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_at))
                or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", min_android_version)
                or (signing_certificate_sha256 and not re.fullmatch(r"[a-f0-9]{64}", signing_certificate_sha256))
                or (manifest_size is not None and (not isinstance(manifest_size, int) or isinstance(manifest_size, bool) or manifest_size <= 0))
                or len(notes) > 4000):
            raise ValueError("invalid manifest fields")
        download_root = (ROOT / "downloads").resolve()
        apk_path = (download_root / filename).resolve()
        if apk_path.parent != download_root or not apk_path.is_file():
            raise ValueError("manifest APK is missing")
        stat = apk_path.stat()
        if stat.st_size <= 0 or stat.st_size > 200 * 1024 * 1024:
            raise ValueError("invalid APK size")
        if manifest_size is not None and manifest_size != stat.st_size:
            raise ValueError("manifest APK size does not match file")
        cache_key = (str(apk_path), stat.st_size, stat.st_mtime_ns)
        actual_digest = _verified_digest_cache.get(cache_key)
        if actual_digest is None:
            hasher = hashlib.sha256()
            with apk_path.open("rb") as apk:
                for chunk in iter(lambda: apk.read(1024 * 1024), b""):
                    hasher.update(chunk)
            actual_digest = hasher.hexdigest()
            _verified_digest_cache.clear()
            _verified_digest_cache[cache_key] = actual_digest
        if actual_digest != digest.lower():
            raise ValueError("manifest digest does not match APK")
        return {
            "version_name": version,
            "version_code": code,
            "download_url": url,
            "sha256": digest.lower(),
            "apk_size_bytes": stat.st_size,
            "release_notes": notes,
            "published_at": published_at,
            "min_android_version": min_android_version,
            "signing_certificate_sha256": signing_certificate_sha256,
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="Обновление пока недоступно") from None


def _manifest_response(head: bool = False) -> Response:
    manifest = _load_manifest()
    body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache",
               "X-Content-Type-Options": "nosniff", "Content-Length": str(len(body))}
    return Response(content=b"" if head else body, media_type="application/json", headers=headers)


@router.get("/app", include_in_schema=False)
@router.get("/app/", include_in_schema=False)
async def app_download_page():
    if not PAGE.is_file():
        raise HTTPException(status_code=404, detail="Страница загрузки недоступна")
    return FileResponse(PAGE, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})


@router.head("/app", include_in_schema=False)
@router.head("/app/", include_in_schema=False)
async def app_download_page_head():
    if not PAGE.is_file():
        raise HTTPException(status_code=404, detail="Страница загрузки недоступна")
    return Response(status_code=200, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
                             "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                             "Content-Security-Policy": "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
                             "Content-Length": str(PAGE.stat().st_size)})


@router.get("/app/assets/{asset_name}", include_in_schema=False)
async def app_brand_asset(asset_name: str):
    """Serve only the fixed, public image assets used by the connection page."""
    if asset_name not in ICON_ASSETS:
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = (ASSETS / asset_name).resolve()
    if path.parent != ASSETS.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/app/update.json", include_in_schema=False)
async def update_manifest():
    return _manifest_response()


@router.head("/app/update.json", include_in_schema=False)
async def update_manifest_head():
    return _manifest_response(head=True)


@router.api_route("/app/downloads/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
async def download_apk(filename: str, request: Request):
    match = APK_NAME.fullmatch(filename)
    if not match:
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = (ROOT / "downloads" / filename).resolve()
    downloads = (ROOT / "downloads").resolve()
    if path.parent != downloads or not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    headers = {"Cache-Control": "public, max-age=31536000, immutable",
               "X-Content-Type-Options": "nosniff",
               "Content-Disposition": f'attachment; filename="{filename}"'}
    if request.method == "HEAD":
        headers["Content-Length"] = str(path.stat().st_size)
        return Response(status_code=200, media_type="application/vnd.android.package-archive", headers=headers)
    return FileResponse(path, media_type="application/vnd.android.package-archive", headers=headers)
