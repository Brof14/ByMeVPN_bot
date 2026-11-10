"""Production FastAPI surface must not publish interactive or schema docs."""
import os
import secrets
import unittest

os.environ.setdefault("DB_FILE", "/tmp/test_fastapi_surface.db")
os.environ.setdefault("JWT_SECRET", secrets.token_urlsafe(48))

from webhook import app
from api_router import api_router


def _paths(router, prefix=""):
    paths = set()
    for route in router.routes:
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(f"{prefix}{path}")
            continue
        included = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        if included is not None:
            paths.update(_paths(included, f"{prefix}{getattr(context, 'prefix', '')}"))
    return paths


class FastApiSurfaceTests(unittest.TestCase):
    def test_production_app_disables_swagger_redoc_and_openapi_schema(self):
        self.assertIsNone(app.docs_url)
        self.assertIsNone(app.redoc_url)
        self.assertIsNone(app.openapi_url)
        routes = {getattr(route, "path", None) for route in app.routes}
        self.assertNotIn("/docs", routes)
        self.assertNotIn("/redoc", routes)
        self.assertNotIn("/openapi.json", routes)

    def test_api_v1_keeps_legacy_route_paths_after_router_grouping(self):
        paths = _paths(api_router)
        self.assertTrue({
            "/auth/login", "/auth/telegram/check-session", "/subscription/status",
            "/subscription/trial", "/servers", "/payment/create", "/mobile/v2/plans",
        }.issubset(paths))


if __name__ == "__main__":
    unittest.main()
