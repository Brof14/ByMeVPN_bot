"""Compatibility redirects for the former APK download URLs."""
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()
DOWNLOAD_PAGE_PATH = "/app/"


@router.get("/download")
@router.get("/download/app.apk")
@router.get("/download/debug")
async def legacy_download_redirect():
    """Send old links to the official page, never to stale or debug APKs."""
    return RedirectResponse(
        url=DOWNLOAD_PAGE_PATH,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )
