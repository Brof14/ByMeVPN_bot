"""Safe correlation and panel-stage diagnostics over real HTTP routes."""
import os
import secrets
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('JWT_SECRET', secrets.token_urlsafe(48))
os.environ.setdefault('DB_FILE', '/tmp/test_device_auth_diagnostics.db')
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import api_router
import xui_client


class DeviceAuthDiagnosticsHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = FastAPI()
        app.include_router(api_router.api_router, prefix='/api/v1')
        app.dependency_overrides[api_router.get_current_user_id] = lambda: 123456
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url='https://test.invalid')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_panel_client_not_found_is_correlated_without_leaking_exception(self):
        class Panel:
            def _url(self, path): return path
            async def _get(self, *args, **kwargs):
                raise ValueError('Response status is not successful, message: Obtain (record not found)')
            async def _post(self, *args, **kwargs):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'success': True, 'obj': []})
        panel = SimpleNamespace(client=Panel())
        with patch.object(xui_client, '_get_api', new=AsyncMock(return_value=panel)), \
                self.assertLogs(xui_client.logger, level='WARNING') as log:
            result = await self.client.get('/api/v1/devices', headers={'X-Correlation-ID': 'safe-test-1234'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers['X-Correlation-ID'], 'safe-test-1234')
        self.assertEqual(result.json(), {'devices': [], 'device_limit': 0, 'devices_used': 0})
        content = '\n'.join(log.output)
        self.assertIn('stage=client_read code=record_not_found correlation_id=safe-test-1234', content)
        self.assertNotIn('123456', content)

    async def test_unrecognized_panel_error_remains_503(self):
        class Panel:
            def _url(self, path): return path
            async def _get(self, *args, **kwargs):
                raise ValueError('record not found PRIVATE_TEST_VALUE')
            async def _post(self, *args, **kwargs):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'success': True, 'obj': []})
        with patch.object(xui_client, '_get_api', new=AsyncMock(return_value=SimpleNamespace(client=Panel()))), \
                self.assertLogs(xui_client.logger, level='WARNING') as log:
            result = await self.client.get('/api/v1/devices', headers={'X-Correlation-ID': 'safe-test-1234'})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json(), {'detail': 'Список устройств временно недоступен'})
        content = '\n'.join(log.output)
        self.assertIn('stage=client_read code=invalid_value', content)
        self.assertNotIn('PRIVATE_TEST_VALUE', content)

    async def test_missing_hwids_record_is_not_returned_as_empty_success(self):
        class Panel:
            def _url(self, path): return path
            async def _get(self, *args, **kwargs):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'success': True, 'obj': {'client': {'limitHwid': 2}}})
            async def _post(self, *args, **kwargs):
                raise ValueError('Response status is not successful, message: Obtain (record not found)')
        with patch.object(xui_client, '_get_api', new=AsyncMock(return_value=SimpleNamespace(client=Panel()))):
            result = await self.client.get('/api/v1/devices', headers={'X-Correlation-ID': 'safe-test-1234'})
        self.assertEqual(result.status_code, 503)

    async def test_existing_client_with_empty_device_list_keeps_panel_limit(self):
        class Panel:
            def _url(self, path): return path
            async def _get(self, *args, **kwargs):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'success': True, 'obj': {'client': {'limitHwid': 2}}})
            async def _post(self, *args, **kwargs):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'success': True, 'obj': []})
        with patch.object(xui_client, '_get_api', new=AsyncMock(return_value=SimpleNamespace(client=Panel()))):
            result = await self.client.get('/api/v1/devices', headers={'X-Correlation-ID': 'safe-test-1234'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'devices': [], 'device_limit': 2, 'devices_used': 0})

    async def test_malformed_device_payload_remains_503(self):
        class Panel:
            def _url(self, path): return path
            async def _get(self, *args, **kwargs):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'success': True, 'obj': {'client': {'limitHwid': 2}}})
            async def _post(self, *args, **kwargs):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'success': True, 'obj': 'invalid'})
        with patch.object(xui_client, '_get_api', new=AsyncMock(return_value=SimpleNamespace(client=Panel()))), \
                self.assertLogs(xui_client.logger, level='WARNING') as log:
            result = await self.client.get('/api/v1/devices', headers={'X-Correlation-ID': 'safe-test-1234'})
        self.assertEqual(result.status_code, 503)
        self.assertIn('stage=device_decode code=unavailable', '\n'.join(log.output))

    async def test_servers_401_retains_contract_and_records_safe_reason(self):
        failure = HTTPException(status_code=401, detail='Invalid or expired session token')
        with patch.object(api_router, 'get_current_user_id', new=AsyncMock(side_effect=failure)), \
                self.assertLogs(api_router.logger, level='WARNING') as log:
            result = await self.client.get('/api/v1/servers', headers={'X-Correlation-ID': 'safe-test-1234'})
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.headers['X-Error-Code'], 'authentication_required')
        self.assertEqual(result.headers['X-Correlation-ID'], 'safe-test-1234')
        self.assertIn('reason=invalid_or_expired_token', '\n'.join(log.output))

    async def test_missing_panel_client_status_has_no_entitlement_or_bootstrap(self):
        snapshot = (None, [], {'status': 'active', 'subscription_url': 'private-url', 'seconds_remaining': 100})
        missing = ValueError('Response status is not successful, message: Obtain (record not found)')
        headers = {'X-Entitlement-Version': 'es256-v1', 'X-HWID': 'test-hwid'}
        with patch.object(api_router, 'get_user_real_subscription', new=AsyncMock(return_value=snapshot)), \
                patch.object(xui_client, 'has_xui_client', new=AsyncMock(side_effect=missing)), \
                patch.object(api_router, '_validated_active_device_bootstrap_expiry', new=AsyncMock()) as bootstrap, \
                patch.object(api_router, 'create_entitlement') as signer:
            result = await self.client.get('/api/v1/subscription/status', headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['status'], 'active')
        self.assertTrue(result.json()['device_confirmation_required'])
        self.assertIsNone(result.json().get('entitlement'))
        self.assertIsNone(result.json()['subscription_url'])
        bootstrap.assert_not_awaited()
        signer.assert_not_called()

    async def test_other_value_error_status_remains_503(self):
        snapshot = (None, [], {'status': 'active'})
        headers = {'X-Entitlement-Version': 'es256-v1', 'X-HWID': 'test-hwid'}
        with patch.object(api_router, 'get_user_real_subscription', new=AsyncMock(return_value=snapshot)), \
                patch.object(xui_client, 'has_xui_client', new=AsyncMock(side_effect=ValueError('different failure'))):
            result = await self.client.get('/api/v1/subscription/status', headers=headers)
        self.assertEqual(result.status_code, 503)

    async def test_missing_hwids_record_status_remains_503(self):
        snapshot = (None, [], {'status': 'active'})
        error = ValueError('Response status is not successful, message: Obtain (record not found)')
        headers = {'X-Entitlement-Version': 'es256-v1', 'X-HWID': 'test-hwid'}
        with patch.object(api_router, 'get_user_real_subscription', new=AsyncMock(return_value=snapshot)), \
                patch.object(xui_client, 'has_xui_client', new=AsyncMock(return_value=True)), \
                patch.object(xui_client, 'get_xui_devices', new=AsyncMock(side_effect=error)):
            result = await self.client.get('/api/v1/subscription/status', headers=headers)
        self.assertEqual(result.status_code, 503)

    async def test_client_missing_during_device_lookup_has_no_entitlement(self):
        snapshot = (None, [], {'status': 'active', 'seconds_remaining': 100})
        missing = xui_client.XuiDeviceClientNotFound('Panel client not found')
        headers = {'X-Entitlement-Version': 'es256-v1', 'X-HWID': 'test-hwid'}
        with patch.object(api_router, 'get_user_real_subscription', new=AsyncMock(return_value=snapshot)), \
                patch.object(xui_client, 'has_xui_client', new=AsyncMock(return_value=True)), \
                patch.object(xui_client, 'get_xui_devices', new=AsyncMock(side_effect=missing)), \
                patch.object(api_router, '_validated_active_device_bootstrap_expiry', new=AsyncMock()) as bootstrap, \
                patch.object(api_router, 'create_entitlement') as signer:
            result = await self.client.get('/api/v1/subscription/status', headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['device_confirmation_required'])
        self.assertIsNone(result.json().get('entitlement'))
        bootstrap.assert_not_awaited()
        signer.assert_not_called()

    async def test_subscription_failure_retains_status_and_correlation(self):
        snapshot = (None, [], {'status': 'active'})
        with patch.object(api_router, 'get_user_real_subscription', new=AsyncMock(return_value=snapshot)), \
                patch.object(api_router, '_attach_signed_entitlement', new=AsyncMock(
                    side_effect=HTTPException(status_code=503, detail='panel unavailable'),
                )):
            result = await self.client.get('/api/v1/subscription/status', headers={'X-Correlation-ID': 'safe-test-1234'})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.headers['X-Correlation-ID'], 'safe-test-1234')


class RealJwtDeviceStatusHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        import database
        self.database = database
        self.old_db = database.DB_FILE
        await database.close_db()
        self.temp = tempfile.TemporaryDirectory(prefix='device-auth-regression-')
        database.DB_FILE = os.path.join(self.temp.name, 'isolated.sqlite3')
        database._db_pool = None
        await database.init_db()
        await database.ensure_user(987650)
        app = FastAPI()
        app.include_router(api_router.api_router, prefix='/api/v1')
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url='https://test.invalid')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.database.close_db()
        self.database.DB_FILE = self.old_db
        self.temp.cleanup()

    async def test_real_jwt_distinguishes_active_status_missing_panel_client_and_401(self):
        import time
        issued = await api_router.issue_session_tokens(987650, 'isolated@example.invalid')
        expired = api_router.create_simple_jwt(987650, 'isolated@example.invalid', issued_at=int(time.time())-100, expires_at=int(time.time())-1)
        class Panel:
            def _url(self, path): return path
            async def _get(self, *args, **kwargs):
                raise ValueError('Response status is not successful, message: Obtain (record not found)')
            async def _post(self, *args, **kwargs):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'success': True, 'obj': []})
        mock_snapshot = (None, [{'node_code': 'isolated'}], {'status': 'active'})
        with patch.object(xui_client, '_get_api', new=AsyncMock(return_value=SimpleNamespace(client=Panel()))), \
                patch.object(api_router, 'get_user_real_subscription', new=AsyncMock(return_value=mock_snapshot)):
            auth = {'Authorization': 'Bearer ' + issued['access'], 'X-Correlation-ID': 'isolated-test-id'}
            status = await self.client.get('/api/v1/subscription/status', headers=auth)
            devices = await self.client.get('/api/v1/devices', headers=auth)
            servers = await self.client.get('/api/v1/servers', headers=auth)
            rejected = await self.client.get('/api/v1/servers', headers={'Authorization': 'Bearer ' + expired})
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()['status'], 'active')
        self.assertEqual(status.headers['X-Correlation-ID'], 'isolated-test-id')
        self.assertEqual(devices.status_code, 200)
        self.assertEqual(devices.json(), {'devices': [], 'device_limit': 0, 'devices_used': 0})
        self.assertEqual(servers.status_code, 200)
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(rejected.headers['X-Error-Code'], 'authentication_required')
