"""Focused compatibility checks for the bind-mounted legacy trial adapter."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch
from pydantic import BaseModel
from fastapi import APIRouter, FastAPI

from legacy_trial_bootstrap import (
    _create_grant,
    _fingerprint_matches,
    _grant_expiry,
    _panel_client_not_found,
    _trial_panel_limits_match,
    _valid_panel_device_state,
    install_legacy_trial_bootstrap,
    _select_session_node,
)
from ops.patch_legacy_trial_bootstrap import MARKER, patched_text


class Route:
    def __init__(self, path: str, methods: set[str], endpoint=None):
        self.path = path
        self.methods = methods
        self.endpoint = endpoint


class FakeRouter:
    def __init__(self):
        self.routes = [
            Route("/subscription/trial", {"POST"}),
            Route("/servers", {"GET"}),
            Route("/vpn/session", {"POST"}),
            Route("/subscription/status", {"GET"}),
        ]

    def _register(self, method: str, path: str):
        def decorate(endpoint):
            self.routes.append(Route(path, {method}, endpoint))
            return endpoint
        return decorate

    def post(self, path: str):
        return self._register("POST", path)

    def get(self, path: str):
        return self._register("GET", path)


class TrialRequest(BaseModel):
    install_id: str | None = None
    device_model: str | None = None


class SessionRequest(BaseModel):
    node_code: str


class LegacyTrialBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_adapter_honors_windows_transport_capabilities(self):
        configs = [
            {"node_code": "grpc", "config": {
                "protocol": "vless", "security": "tls", "network": "grpc",
                "uuid": "uuid-from-upstream", "server": "vpn.example.com",
                "server_name": "cdn.example.com"}},
            {"node_code": "reality", "config": {
                "protocol": "vless", "security": "reality", "network": "tcp",
                "uuid": "uuid-from-upstream", "server": "vpn.example.com",
                "server_name": "cdn.example.com", "public_key": "server-owned-public-key"}},
        ]
        legacy = _select_session_node(configs, "auto", True, "")
        self.assertEqual(legacy["node_code"], "reality")
        modern = _select_session_node(configs, "auto", True, "vless-tls,vless-grpc")
        self.assertEqual(modern["node_code"], "grpc")
        with self.assertRaises(Exception) as raised:
            _select_session_node(configs, "grpc", True, "")
        self.assertEqual(getattr(raised.exception, "status_code", None), 422)
        # Never silently switch a manual selection to another server.
        self.assertEqual(_select_session_node(configs, "grpc", False, "")["node_code"], "grpc")

    async def test_bootstrap_grant_is_single_and_does_not_extend_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "trial.sqlite3"
            with sqlite3.connect(db_path) as db:
                db.executescript(
                    "CREATE TABLE users(user_id INTEGER PRIMARY KEY, trial_used INTEGER NOT NULL);"
                    "INSERT INTO users VALUES(41, 1);"
                )
            database = SimpleNamespace(DB_FILE=str(db_path))
            first_expiry = int(time.time()) + 300
            self.assertTrue(await _create_grant(database, 41, "device-digest", first_expiry))
            self.assertFalse(await _create_grant(database, 41, "another-digest", first_expiry + 600))
            self.assertEqual(await _grant_expiry(database, 41, "device-digest"), first_expiry)
            self.assertIsNone(await _grant_expiry(database, 41, "another-digest"))
            with sqlite3.connect(db_path) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("SELECT count(*) FROM mobile_trial_bootstrap").fetchone()[0], 1)

    async def test_installer_replaces_only_the_trial_and_configuration_routes(self):
        router = FakeRouter()
        database = SimpleNamespace(DB_FILE=":memory:")
        namespace = {
            "database": database,
            "TrialRequest": TrialRequest,
            "SessionRequest": SessionRequest,
            "get_current_user_id": object(),
            "_subscription_headers_from_request": lambda request: {},
            "_entitlement_requested": lambda headers: False,
            "logger": SimpleNamespace(warning=lambda *a: None, error=lambda *a: None),
            "parse_single_node_line": lambda *_args: None,
        }
        with patch("legacy_trial_bootstrap._install_fingerprint_readback"):
            install_legacy_trial_bootstrap(router, namespace)
        endpoints = {(route.path, method) for route in router.routes for method in route.methods}
        self.assertEqual(sum(route.path == "/subscription/trial" and "POST" in route.methods
                             for route in router.routes), 1)
        self.assertEqual(sum(route.path == "/servers" and "GET" in route.methods
                             for route in router.routes), 1)
        self.assertEqual(sum(route.path == "/vpn/session" and "POST" in route.methods
                             for route in router.routes), 1)
        self.assertIn(("/subscription/status", "GET"), endpoints)

    async def test_replacement_routes_register_json_models_with_fastapi(self):
        async def current_user_id():
            return 41

        router = APIRouter()
        namespace = {
            "database": SimpleNamespace(DB_FILE=":memory:"),
            "TrialRequest": TrialRequest,
            "SessionRequest": SessionRequest,
            "get_current_user_id": current_user_id,
            "_subscription_headers_from_request": lambda request: {},
            "_entitlement_requested": lambda values: False,
            "logger": SimpleNamespace(warning=lambda *a: None, error=lambda *a: None),
            "parse_single_node_line": lambda *_args: None,
        }
        with patch("legacy_trial_bootstrap._install_fingerprint_readback"):
            install_legacy_trial_bootstrap(router, namespace)
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        schema = app.openapi()
        trial = schema["paths"]["/api/v1/subscription/trial"]["post"]
        session = schema["paths"]["/api/v1/vpn/session"]["post"]
        self.assertIn("requestBody", trial)
        self.assertIn("requestBody", session)

    def test_panel_fingerprint_must_match_the_full_server_hwid_hash_prefix(self):
        hwid = "install-id-for-this-test-device"
        fingerprint = hashlib.sha256(hwid.encode()).hexdigest()
        self.assertTrue(_fingerprint_matches(41, hwid, {"devices": [{"fingerprint": fingerprint[:12]}]}))
        self.assertTrue(_fingerprint_matches(41, hwid, {"devices": [{"fingerprint": fingerprint[:16]}]}))
        self.assertFalse(_fingerprint_matches(41, hwid, {"devices": [{"fingerprint": "0" * 16}]}))
        self.assertFalse(_fingerprint_matches(41, hwid, {"devices": [{"fingerprint": fingerprint[:8]}]}))

    def test_paid_device_limits_remain_usable_but_trial_stays_at_two(self):
        self.assertTrue(_valid_panel_device_state({"devices": [], "limit_hwid": 5, "limit_ip": 0}))
        self.assertTrue(_valid_panel_device_state({"devices": [], "limit_hwid": 10, "limit_ip": 0}))
        self.assertFalse(_trial_panel_limits_match({"devices": [], "limit_hwid": 5, "limit_ip": 0}))
        self.assertTrue(_trial_panel_limits_match({"devices": [], "limit_hwid": 2, "limit_ip": 0}))
        self.assertFalse(_valid_panel_device_state({"devices": [], "limit_hwid": 2, "limit_ip": 1}))

    def test_panel_record_not_found_is_distinguished_from_panel_failure(self):
        self.assertTrue(_panel_client_not_found(ValueError("Obtain (record not found)")))
        self.assertFalse(_panel_client_not_found(TimeoutError("panel request timed out")))

    def test_api_overlay_patch_is_additive_and_idempotent(self):
        source = '''api_router = APIRouter()\nclass TrialRequest(BaseModel):\n    pass\nclass SessionRequest(BaseModel):\n    pass\n@api_router.post("/subscription/trial")\nasync def activate_trial():\n    pass\n@api_router.get("/servers")\nasync def get_servers():\n    pass\n@api_router.post("/vpn/session")\nasync def create_vpn_session():\n    pass\ndef _subscription_headers_from_request(request):\n    pass\ndef _entitlement_requested(headers):\n    pass\ndef parse_single_node_line(line, index, user):\n    pass\n'''
        patched = patched_text(source)
        self.assertIn(MARKER, patched)
        self.assertEqual(patched_text(patched), patched)
        self.assertTrue(patched.startswith(source))

    async def test_first_trial_creates_two_device_client_and_retries_same_short_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "trial.sqlite3"
            with sqlite3.connect(db_path) as db:
                db.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY, trial_used INTEGER NOT NULL)")
                db.execute("INSERT INTO users VALUES(41, 0)")

            class TestDatabase:
                DB_FILE = str(db_path)
                keys = []
                create_count = 0

                async def get_user_active_keys(self, user_id):
                    return list(self.keys)

                async def try_claim_trial(self, user_id):
                    with sqlite3.connect(self.DB_FILE) as db:
                        cursor = db.execute("UPDATE users SET trial_used=1 WHERE user_id=? AND trial_used=0", (user_id,))
                        return cursor.rowcount == 1

                async def reset_trial_for_user(self, user_id):
                    with sqlite3.connect(self.DB_FILE) as db:
                        db.execute("UPDATE users SET trial_used=0 WHERE user_id=?", (user_id,))

                async def add_key(self, **row):
                    self.keys.append({"key": row["key"], "remark": row["remark"], "expiry": row["expiry"]})

            database = TestDatabase()
            panel_client = {"client": None, "state": {"devices": [], "limit_hwid": 2, "limit_ip": 0}}
            panel_expiry = int(time.time() + 3 * 86400) * 1000
            client_uuid = "test-panel-client-uuid"

            async def get_by_email(_email):
                if panel_client["client"] is None:
                    raise ValueError("Obtain (record not found)")
                return panel_client["client"]

            async def panel_create(*, user_id, days, limit_ip):
                self.assertEqual((days, limit_ip), (3, 2))
                database.create_count += 1
                panel_client["client"] = SimpleNamespace(uuid=client_uuid, expiry_time=panel_expiry)
                return {"subscription_url": "https://subscription.invalid/test", "uuid": client_uuid,
                        "created_new_client": True}

            async def panel_devices(_user_id):
                return dict(panel_client["state"])

            async def get_api():
                return SimpleNamespace(client=SimpleNamespace(get_by_email=get_by_email))

            async def retry(call, *args):
                return await call(*args)

            xui_module = ModuleType("xui_client")
            xui_module._get_api = get_api
            xui_module._api_call_with_retry = retry
            xui_module.create_xui_user = panel_create
            xui_module.get_xui_devices = panel_devices

            class TestEntitlementSigningError(Exception):
                pass

            signed_claims = []
            entitlements_module = ModuleType("entitlements")
            entitlements_module.EntitlementSigningError = TestEntitlementSigningError
            entitlements_module.load_private_key = lambda: object()
            entitlements_module.device_binding = lambda user_id, hwid: hashlib.sha256(
                f"{user_id}:{hwid}".encode()
            ).hexdigest()
            entitlements_module.create_entitlement = lambda user_id, hwid, payload: signed_claims.append(
                dict(payload)
            ) or "test-es256-entitlement"

            router = FakeRouter()
            headers = {"X-HWID": "installation-hwid-0001", "X-Entitlement-Version": "es256-v1"}
            namespace = {
                "database": database,
                "TrialRequest": TrialRequest,
                "SessionRequest": SessionRequest,
                "get_current_user_id": object(),
                "_subscription_headers_from_request": lambda request: request.headers,
                "_entitlement_requested": lambda values: any(
                    key.casefold() == "x-entitlement-version" and value == "es256-v1"
                    for key, value in values.items()
                ),
                "logger": SimpleNamespace(warning=lambda *a: None, error=lambda *a: None),
                "parse_single_node_line": lambda *_args: None,
            }
            request = SimpleNamespace(headers=headers)
            request_body = SimpleNamespace(install_id=headers["X-HWID"])
            with patch.dict(sys.modules, {"xui_client": xui_module, "entitlements": entitlements_module}), \
                 patch("legacy_trial_bootstrap._install_fingerprint_readback"), \
                 patch("legacy_trial_bootstrap._fetch_nodes", new=AsyncMock(return_value=[{"node_code": "test"}])):
                install_legacy_trial_bootstrap(router, namespace)
                activate = next(r.endpoint for r in router.routes
                                if r.path == "/subscription/trial" and "POST" in r.methods)
                first = await activate(request_body, request, user_id=41)
                grant_expiry = await _grant_expiry(database, 41, entitlements_module.device_binding(41, headers["X-HWID"]))
                second = await activate(request_body, request, user_id=41)

            self.assertEqual(database.create_count, 1)
            self.assertEqual(len(database.keys), 1)
            self.assertEqual(first["status"], "trial")
            self.assertEqual(first["max_devices"], 2)
            self.assertEqual(first["expires_at"], second["expires_at"])
            self.assertTrue(first["entitlement_bootstrap"])
            self.assertEqual(first["entitlement"], "test-es256-entitlement")
            self.assertEqual(second["entitlement"], "test-es256-entitlement")
            self.assertEqual(await _grant_expiry(database, 41, entitlements_module.device_binding(41, headers["X-HWID"])), grant_expiry)
            self.assertLessEqual(signed_claims[0]["seconds_remaining"], 600)
            self.assertGreater(signed_claims[0]["seconds_remaining"], 0)
            with sqlite3.connect(db_path) as db:
                self.assertEqual(db.execute("SELECT trial_used FROM users WHERE user_id=41").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
