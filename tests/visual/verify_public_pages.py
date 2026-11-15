"""Browser smoke tests and screenshot artifacts for public connection and Android pages.

All URLs are synthetic. No real subscription, user or payment data is used.
Run: python -m playwright install chromium && python tests/visual/verify_public_pages.py
"""
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from app_links import render_connection_choice_page

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts" / "public-ui"
ARTIFACTS.mkdir(parents=True, exist_ok=True)
CONNECT_HTML = render_connection_choice_page("https://sub.example.invalid/test-account")
LANDING_HTML = (ROOT / "static" / "app" / "index.html").read_text("utf-8")

ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)
IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def dispatch(route):
    path = urlsplit(route.request.url).path
    if path == "/connect":
        return route.fulfill(status=200, content_type="text/html", body=CONNECT_HTML)
    if path == "/app":
        return route.fulfill(status=200, content_type="text/html", body=LANDING_HTML)
    if path == "/app/logo":
        path = "/app/assets/bymevpn-logo.png"
    if path.startswith("/app/assets/"):
        filename = path.removeprefix("/app/assets/")
        if not filename or "/" in filename or ".." in filename:
            return route.abort()
        src = ROOT / "static" / "app" / "assets" / filename
        if src.is_file():
            kind = "image/svg+xml" if src.suffix == ".svg" else "image/png"
            return route.fulfill(status=200, content_type=kind, body=src.read_bytes())
    return route.abort()


def no_horizontal_overflow(page, label):
    metrics = page.evaluate("""() => ({
      width: window.innerWidth,
      scroll: document.documentElement.scrollWidth,
    })""")
    assert metrics["scroll"] <= metrics["width"] + 1, (label, metrics)


def test_connection(browser, *, name, width, height, ua, selected):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        user_agent=ua, is_mobile=width <= 430, device_scale_factor=1,
    )
    page = context.new_page()
    page.route("http://bymevpn.local/**", dispatch)
    page.goto("http://bymevpn.local/connect", wait_until="load")
    page.wait_for_selector("#app:not([hidden])")
    assert page.locator("h1").inner_text() == "Подключение VPN"
    if selected:
        assert page.locator(f'[data-platform="{selected}"]').get_attribute("aria-pressed") == "true"
        assert page.locator("#recommendation-panel").is_visible()
        assert page.locator("#copy-subscription").is_visible()
    else:
        assert not page.locator("#recommendation-panel").is_visible()
        assert "Выберите платформу" in page.locator("#status").inner_text()
        page.locator('[data-platform="android"]').click()
        assert page.locator("#recommendation-panel").is_visible()
    min_height = page.locator(".platform").first.bounding_box()["height"]
    assert min_height >= 44, (name, min_height)
    no_horizontal_overflow(page, name)
    page.screenshot(path=str(ARTIFACTS / f"connect-{name}-{width}.png"), full_page=True)
    context.close()


def test_landing(browser, *, width, height):
    context = browser.new_context(
        viewport={"width": width, "height": height}, device_scale_factor=1,
    )
    page = context.new_page()
    page.route("http://bymevpn.local/**", dispatch)
    page.goto("http://bymevpn.local/app", wait_until="load")
    assert page.get_by_role("link", name="Скачать ByMeVPN").is_visible()
    assert page.get_by_role("link", name="Скачать Happ вместо ByMeVPN").is_visible()
    no_horizontal_overflow(page, f"landing-{width}")
    page.screenshot(path=str(ARTIFACTS / f"android-landing-{width}.png"), full_page=True)
    context.close()


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for name, width, height, ua, selected in (
                ("android", 320, 720, ANDROID_UA, "android"),
                ("android", 390, 844, ANDROID_UA, "android"),
                ("ios", 390, 844, IOS_UA, "ios"),
                ("windows", 1366, 768, WINDOWS_UA, "windows"),
                ("unknown", 375, 812, "ByMeVPN-VisualTests/1.0", None),
            ):
                test_connection(
                    browser, name=name, width=width, height=height,
                    ua=ua, selected=selected,
                )
            for width, height in ((320, 720), (390, 844), (1366, 768)):
                test_landing(browser, width=width, height=height)
        finally:
            browser.close()
    print(f"Visual layout checks passed; screenshots: {ARTIFACTS}")


if __name__ == "__main__":
    main()
